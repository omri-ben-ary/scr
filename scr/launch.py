"""Launching scriber sessions for dispatched tasks, and the Intake run (Python ports of
hooks/launch_dispatched.ps1 and hooks/intake_run.ps1)."""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import bgwait
from .brief import brief_for
from .assets import asset_path
from .events import EventLog
from .ops import spawn, task_worktree, workload_for
from .run import (claude_bin, scr_display, new_session_id, permission_mode_for, process_created, record_launch, scr_cmd, session_argv,
                  start_claude)
from .store import Store


def launch(root: Path, tids: list[str] | None = None, mode: str = "headless", permission_mode: str | None = None,
           model: str | None = None, relaunch: bool = False, dry_run: bool = False) -> list[str]:
    """`permission_mode=None` (the normal case) is the config default resolved by scr/run.py (T-0077)."""
    permission_mode = permission_mode or permission_mode_for(root)
    store = Store(root)
    tasks = store.load_all()
    want = {"dispatched", "in_progress", "checkpoint"} if relaunch else {"dispatched"}
    tids = list(tids or [t.id for t in tasks.values() if t.status in want])
    out: list[str] = []
    if not tids:
        return ["nothing in status 'dispatched'"]
    out.append(f"launching: {', '.join(tids)} (mode {mode})")
    if not claude_bin() and not dry_run:
        return out + ["claude (Claude Code CLI) not on PATH — nothing launched"]
    # Background-wait ceiling per task (scr/bgwait.py): > 12 h only when Omri set it; otherwise park, never launch.
    events = EventLog(root / "events" / "events.jsonl").read()
    ceiling: dict[str, int | None] = {}
    for tid in list(tids):
        try:
            m = bgwait.requested(tasks[tid].rec)
            problem = None if bgwait.authorized(events, tid, m) else (
                f"{bgwait.FIELD} = {m} min is over {bgwait.AUTH_LIMIT_MINUTES // 60} h and was not set by Omri")
        except ValueError as e:
            m, problem = None, str(e)
        if problem:
            out.append(f"=== {tid} NOT launched: {problem}")
            if not dry_run:
                out += park_unauthorized(root, tasks[tid], problem)
            tids.remove(tid)
        else:
            ceiling[tid] = m
    if not tids:
        return out
    if not dry_run:
        out += spawn(root, tids)
    for tid in tids:
        t = tasks[tid]
        wt = task_worktree(workload_for(root, store, t), t)
        brief = brief_for(root, store, t)
        bf = t.dir / "brief.txt"
        bf.write_text(brief, encoding="utf-8")
        out.append(f"=== {tid} [{t.status}] {wt}")
        if dry_run:
            out.append(brief); continue
        if not wt.exists():
            out.append("  worktree missing — skipped"); continue
        if t.status == "dispatched":
            scr_cmd(root, f"steward:{tid}", "task", "set", tid, "in_progress", "--quiet")
        log_md = t.dir / "log.md"
        stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if mode == "headless":
            tr = t.dir / f"session-{datetime.now():%Y%m%d-%H%M%S}.txt"
            with log_md.open("a", encoding="utf-8") as f:
                f.write(f"\n- {stamp} [launcher] headless scriber session started (permission-mode {permission_mode}); "
                        f"background-wait ceiling {bgwait.describe(ceiling.get(tid))}\n")
            sid = new_session_id()                                 # resumable by `scr takeover` (T-0247)
            argv = session_argv(wt, root=root, permission_mode=permission_mode, model=model, session_id=sid) or []
            pid = start_claude(wt, bf, tr, permission_mode=permission_mode, model=model, root=root,
                               env_extra=bgwait.env_for(ceiling.get(tid)) or None, session_id=sid)
            created = process_created(pid) if pid else None      # the pid's identity: None if it already died (T-0147)
            with log_md.open("a", encoding="utf-8") as f:
                f.write(f"- {stamp} [launcher] pid {pid}, transcript {tr}\n")
                f.write(f"- {stamp} [launcher] session {sid} (cwd {Path(wt).resolve()}; take over: scr takeover {tid})\n")
            # the watcher re-checks this pid and this transcript's .err every tick (T-0063): both go in `data`,
            # the note stays human-readable (and is the fallback for events recorded before `data` existed);
            # `created` lets it tell this session from a later process that reused the pid (T-0147)
            record_launch(root, "scriber", tid, pid, sid, wt, argv, note=f"headless pid {pid}",
                          data={"transcript": tr.name, "created": created, "bg_wait_minutes": ceiling.get(tid)})
            out.append(f"  headless pid {pid}; session {sid}; transcript {tr}")
        else:
            ps1 = t.dir / "launch.ps1"     # a script, not an inline command: wt splits command lines on ';'
            from .discuss import ps_quote
            ps1.write_text("\ufeff" + f"Set-Location -LiteralPath {ps_quote(str(wt))}\nclaude {('--model ' + model) if model else ''} (Get-Content -Raw -Encoding UTF8 {ps_quote(str(bf))})\n", encoding="utf-8")
            if os.name == "nt" and shutil.which("wt"):
                subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", tid, "-d", str(wt), "powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(ps1)])
            elif os.name == "nt":
                subprocess.Popen(["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], cwd=wt, creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]
            else:
                out.append(f"  interactive tabs need Windows Terminal; run in {wt}: claude \"$(cat {bf})\""); continue
            scr_cmd(root, f"steward:{tid}", "event", "system", "--name", "launch", "--task", tid, "--note", "interactive tab", "--quiet")
            out.append("  tab opened")
    return out


def park_unauthorized(root: Path, t, problem: str) -> list[str]:
    """A task whose background-wait request is not allowed goes to Omri's queue instead of running:
    dispatched -> in_progress -> escalated -> omri_queue (the sanctioned transitions), one event each."""
    path = {"dispatched": ["in_progress", "escalated", "omri_queue"], "in_progress": ["escalated", "omri_queue"],
            "checkpoint": ["escalated", "omri_queue"]}.get(t.status, [])
    note = (f"Gate: {problem}. To authorize: scr --actor omri task field {t.id} {bgwait.FIELD} <minutes>; "
            f"then set it triaged -> ready")[:300]
    out = []
    for st in path:
        rc, text = scr_cmd(root, f"steward:{t.id}", "task", "set", t.id, st, "--note", note, "--quiet")
        if rc != 0:
            out.append(f"  park failed at {st}: {text}")
            break
    else:
        out.append(f"  parked in Omri's queue: {note}")
    scr_cmd(root, f"steward:{t.id}", "event", "system", "--name", "bg_wait_unauthorized", "--task", t.id,
            "--note", problem[:300], "--quiet")
    return out


INTAKE_BRIEF = """You are Intake. Read {agents}/intake.md and {agents}/_common.md and follow them exactly.
Scriptorium root: {root} (run scr from there: `{scr} --actor intake ...`; the tree journals itself — never run git here).
Sources this run: (1) every line under '## Inbox' in queues/omri.md — one task each, then remove the line;
(2) the unlocks block of every report.md whose task reached merged or rejected since the last intake run
(find them with scr replay and the 'intake_ran' system events); (3) system events blocker_rejected / intake_starved.
For every task you create: fill spec.md from the template with a real Goal, Scope (files!), Definition of done
(mechanical lines wherever possible) and Context; set done_when in task.yaml; add informs/blocks edges with
--reason; then task set <id> triaged and ready (or blocked). Respect limits.yaml (wip_ready_scribers, max_depth).
Never create a task that names a declaration an open task already names (dedup by scope; add an informs edge instead).
End with: scr event system --name intake_ran --note '<n created>'; scr verify.
{dry}"""


def intake(root: Path, dry_run: bool = False, timeout: int = 3600) -> list[str]:
    sid = new_session_id()
    argv = session_argv(root, root=root, session_id=sid)   # the config permission mode (scr/run.py), cwd = the root
    if not argv:
        return ["claude not on PATH — intake skipped"]
    scr = scr_display(root)
    brief = INTAKE_BRIEF.format(root=root, agents=asset_path("agents"), scr=scr, dry="DRY RUN: do not create anything; print what you would create." if dry_run else "")
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    out_file = root / "reports" / f"intake-{stamp}.txt"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    # T-0285: the brief goes in as a FILE on stdin, exactly like start_claude (scr/run.py). Piping it through
    # communicate() lost it: `claude -p` stops waiting for stdin after 3 s ("no stdin data received in 3s"), and
    # every Intake run from 2026-09-30 to 2026-10-09 ended with "Input must be provided" and created nothing.
    brief_file = root / "reports" / f"intake-{stamp}.brief.txt"
    brief_file.write_text(brief, encoding="utf-8")
    # Popen, not run: the pid and the session id go on record BEFORE the wait, so an Intake that runs for 17
    # minutes can be taken over while it runs (`scr takeover --session <id>`, T-0247)
    p = subprocess.Popen(argv, cwd=root, stdin=brief_file.open("rb"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    record_launch(root, "intake", None, p.pid, sid, root, argv, actor="steward-watch", data={"transcript": out_file.name})
    try:
        so, se = p.communicate(timeout=timeout)
        text = (so or "") + (se or "")
    except subprocess.TimeoutExpired:
        p.kill()
        text = "intake timed out"
    out_file.write_text(text, encoding="utf-8")
    EventLog(root / "events" / "events.jsonl").append("system", name="intake_run", actor="steward-watch", note=str(out_file.name))
    return text.strip().splitlines()[-15:]
