"""Launching scriber sessions for dispatched tasks, and the Intake run (Python ports of
hooks/launch_dispatched.ps1 and hooks/intake_run.ps1)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .brief import brief_for
from .events import EventLog
from .ops import spawn, task_worktree, workload_for
from .run import claude_bin, permission_mode_for, scr_cmd, session_argv, start_claude
from .store import Store


def launch(root: Path, tids: list[str] | None = None, mode: str = "headless", permission_mode: str | None = None,
           model: str | None = None, relaunch: bool = False, dry_run: bool = False) -> list[str]:
    """`permission_mode=None` (the normal case) is the config default resolved by scr/run.py (T-0077)."""
    permission_mode = permission_mode or permission_mode_for(root)
    store = Store(root)
    tasks = store.load_all()
    want = {"dispatched", "in_progress", "checkpoint"} if relaunch else {"dispatched"}
    tids = tids or [t.id for t in tasks.values() if t.status in want]
    out: list[str] = []
    if not tids:
        return ["nothing in status 'dispatched'"]
    out.append(f"launching: {', '.join(tids)} (mode {mode})")
    if not claude_bin() and not dry_run:
        return out + ["claude (Claude Code CLI) not on PATH — nothing launched"]
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
                f.write(f"\n- {stamp} [launcher] headless scriber session started (permission-mode {permission_mode})\n")
            pid = start_claude(wt, bf, tr, permission_mode=permission_mode, model=model, root=root)
            with log_md.open("a", encoding="utf-8") as f:
                f.write(f"- {stamp} [launcher] pid {pid}, transcript {tr}\n")
            # the watcher re-checks this pid and this transcript's .err every tick (T-0063): both go in `data`,
            # the note stays human-readable (and is the fallback for events recorded before `data` existed)
            scr_cmd(root, f"steward:{tid}", "event", "system", "--name", "launch", "--task", tid, "--note", f"headless pid {pid}",
                    "--data", json.dumps({"pid": pid, "transcript": tr.name}), "--quiet")
            out.append(f"  headless pid {pid}; transcript {tr}")
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


INTAKE_BRIEF = """You are Intake. Read {root}/agents/intake.md and {root}/agents/_common.md and follow them exactly.
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
    argv = session_argv(root, root=root)          # the config permission mode (scr/run.py), cwd = the root
    if not argv:
        return ["claude not on PATH — intake skipped"]
    scr = root / ".venv" / "Scripts" / "scr.exe"
    brief = INTAKE_BRIEF.format(root=root, scr=scr if scr.exists() else "scr", dry="DRY RUN: do not create anything; print what you would create." if dry_run else "")
    out_file = root / "reports" / f"intake-{datetime.now():%Y%m%d-%H%M%S}.txt"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(argv, input=brief, cwd=root,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        text = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        text = "intake timed out"
    out_file.write_text(text, encoding="utf-8")
    EventLog(root / "events" / "events.jsonl").append("system", name="intake_run", actor="steward-watch", note=str(out_file.name))
    return text.strip().splitlines()[-15:]
