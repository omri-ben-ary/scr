"""Discuss a ticket (or plan new work): open an interactive Claude Code session in its own terminal
window, with the RIGHT role, in the RIGHT folder, preloaded with everything about the item, that
(a) helps Omri decide and (b) carries the decision out with scr once he says so.

Items and their sessions (role / working directory):
  T-xxxx with an open HELP:     Steward, in the task's worktree (the code the scriber is stuck on)
  T-xxxx escalated / gate       Steward, in the task's worktree (review.md + the diff are there)
  T-xxxx decision (omri queue)  Concierge, in the workload repo (notes/, missions/, the Lean)
  T-xxxx other                  Steward, in its worktree if it exists, else the workload repo
  chore:<id>                    Operator, in the scriptorium root
  event:<hash>                  Steward, in the task's worktree if any, else the scriptorium root
  plan                          Intake + Prioritizer, in the workload repo (what to work on next)
  general                       Operator, in the scriptorium root

The brief is a FILE and the first message tells the session to read it: a Windows command line is
capped at 32k characters, so passing the brief inline silently truncated it (seen 2026-09-16).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .events import EventLog
from .ops import task_worktree, workload
from .run import claude_bin, scr_exe
from .store import Store

CONCLUDE = """
## When Omri concludes
When he says the conversation is concluded (any of "done", "go ahead", "apply", "that's the decision",
"create them"), restate each decision in one line, then CARRY IT OUT with scr and print a two-line summary.
Do nothing he has not decided. Commands (run from anywhere; `--root` is set):
  answers        {scr} --actor omri answer <task> --kind decision|help|gate --text "<answer>"
  chores         {scr} --actor omri chore run <id>
  status         {scr} --actor omri task set <id> <status> --note "<why>"
  new task       {scr} --actor omri task new "<title>" --kind prove|audit|survey|decide|refactor --files <paths...> --decls <names...> [--blocked-by T-...] [--origin T-...|omri] [--may-touch-statements]
                 then fill tasks/<id>-*/spec.md from templates/spec.md (Goal, Scope, Definition of done with mechanical lines, Context),
                 then {scr} --actor omri task set <id> triaged and ready (or blocked)
  edges          {scr} --actor omri edge add blocks|informs|supersedes <from> <to> --reason "<why>" [--weight w]
  inbox line     {scr} --actor omri inbox "<one line>"      (when a thought is not ready to be a task)
  dismiss alarm  {scr} --actor omri ack <hash>
  finally        {scr} verify
The scriptorium tree journals itself after every scr call: never run git in {root}. Never edit Lean,
mission files or notes in this session; that is scriber work and becomes a task.
"""

ROLES = {
    "steward": ("Steward", "You are the Steward of the Scriptorium research operation (read {root}/agents/steward.md and "
                "{root}/agents/_common.md). You judge submissions and stuck tasks against their spec and the mechanical "
                "checks, and you answer scribers' HELP: questions with precise, honest guidance. You are in the task's "
                "worktree: read the actual code, run `lake build` or the workload's checkers when it helps."),
    "concierge": ("Concierge", "You are the Concierge of the Scriptorium research operation (read {root}/agents/concierge.md and "
                  "{root}/agents/_common.md). Omri's queue holds decisions about research direction; you make each one a "
                  "one-screen choice: the question, the options, what each unlocks, your recommendation, and you apply the answer."),
    "intake": ("Intake + Prioritizer", "You are Intake and the Prioritizer of the Scriptorium research operation (read {root}/agents/intake.md, "
               "{root}/agents/prioritizer.md and {root}/agents/_common.md). You turn a conversation about what to work on next "
               "into well-formed task records with real scope, mechanical done-conditions and edges, deduplicated against "
               "the open graph, and you say what you would dispatch first and why."),
    "operator": ("Operator", "You are the operator's assistant for the Scriptorium research operation (read {root}/agents/_common.md). "
                 "You know the records, the event log and the chores; you explain and you execute exactly what Omri decides."),
}


@dataclass
class Session:
    item: str
    role: str
    cwd: Path
    brief: str
    title: str


def _read(p: Path, limit: int = 60_000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _events_for(root: Path, tid: str, n: int = 40) -> str:
    evs = [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("task") == tid][-n:]
    return "\n".join(json.dumps({k: v for k, v in e.items() if k not in ("h", "prev")}) for e in evs)


def _task_context(root: Path, tid: str) -> str:
    t = Store(root).get(tid)
    d = t.dir
    parts = [f"# Ticket {tid}: {t.rec.get('title', '')}",
             f"status: {t.status}   kind: {t.rec.get('kind')}   queue: {t.rec.get('queue')}   record folder: {d}",
             "```yaml\n" + _read(d / "task.yaml", 8000) + "\n```"]
    for name in ("spec.md", "review.md", "report.md", "log.md"):
        txt = _read(d / name, 40_000)
        if txt.strip():
            parts.append(f"## {name} ({d / name})\n" + txt)
    ev = _events_for(root, tid)
    if ev:
        parts.append("## Recent events\n" + ev)
    return "\n\n".join(parts)


def _open_help(root: Path, t) -> bool:
    log = _read(t.dir / "log.md", 200_000)
    from .ui import _help_lines
    return bool(_help_lines(log))


def _graph_context(root: Path, repo: Path, wl) -> str:
    store = Store(root)
    tasks = store.load_all()
    rows = []
    for t in sorted(tasks.values(), key=lambda t: t.id):
        if t.is_open:
            sc = (t.rec.get("scope") or {})
            rows.append(f"- {t.id} `{t.status}` [{t.rec.get('kind')}] {t.rec.get('title', '')}"
                        + (f" — files: {', '.join(sc.get('files') or [])[:160]}" if sc.get("files") else "")
                        + (f" — blocked_by {', '.join(t.rec.get('blocked_by'))}" if t.rec.get("blocked_by") else ""))
    parts = ["# Open tasks (the graph you must not duplicate)\n" + "\n".join(rows)]
    q = _read(root / "queues" / "scribers.md", 20_000)
    if q:
        parts.append("## Scriber queue (ranked)\n" + q)
    inbox = _read(root / "queues" / "omri.md", 20_000)
    if "## Inbox" in inbox:
        parts.append("## Inbox (ideas not yet tasks)\n" + inbox[inbox.index("## Inbox"):])
    rm = wl.cfg.get("roadmap")
    if rm and (repo / rm).exists():
        parts.append(f"## Roadmap ({repo / rm})\n" + _read(repo / rm, 30_000))
    closed = [t for t in tasks.values() if t.status in ("merged", "closed")]
    if closed:
        parts.append("## Recently finished (their reports' `unlocks` blocks are proposals)\n" + "\n".join(
            f"- {t.id} `{t.status}` {t.rec.get('title', '')} — report: {t.dir / 'report.md'}" for t in sorted(closed, key=lambda t: t.rec.get('updated') or '', reverse=True)[:12]))
    lim = _read(root / "config" / "limits.yaml", 4000)
    parts.append("## limits.yaml\n```yaml\n" + lim + "\n```")
    return "\n\n".join(parts)


def build(root: Path, item: str) -> Session:
    store = Store(root)
    wl = workload(root, store)
    repo = wl.repo
    scr = " ".join(f'"{a}"' if " " in a else a for a in scr_exe(root))
    conclude = CONCLUDE.format(scr=scr, root=root)
    if item.startswith("chore:"):
        from .chores import load
        c = next((x for x in load(root) if x["id"] == item[6:]), None)
        body = f"# Chore {item[6:]}\n```json\n" + (json.dumps(c, indent=1, default=str) if c else "(unknown chore)") + "\n```"
        return Session(item, "operator", root, body + conclude, f"chore {item[6:]}")
    if item.startswith("event:"):
        h = item[6:]
        e = next((e for e in EventLog(root / "events" / "events.jsonl").read() if (e.get("h") or "").startswith(h)), None)
        body = "# System alarm\n```json\n" + (json.dumps(e, indent=1) if e else "(unknown event)") + "\n```"
        cwd = root
        if e and e.get("task"):
            body += "\n\n" + _task_context(root, e["task"])
            t = store.get(e["task"])
            wt = task_worktree(wl, t)
            cwd = wt if wt.exists() else repo
        return Session(item, "steward", cwd, body + conclude, f"alarm {h[:8]}")
    if item == "plan":
        body = ("# Planning session: what should the operation work on next?\n"
                "Omri will describe directions, results he wants, or things that bother him. Your job: turn the conversation into a "
                "LIST of candidate tasks (title, kind, scope files/decls, mechanical done-conditions, blockers, origin), check each "
                "against the open graph below (dedup!), rank them the way the Prioritizer would, and when he concludes, create the "
                "ones he approves as records — each with a filled spec.md — set them triaged/ready, and add the edges.\n\n"
                + _graph_context(root, repo, wl))
        return Session(item, "intake", repo, body + conclude, "plan new tasks")
    if item == "general":
        body = "# General discussion\nNo single ticket. Use `scr graph analyze`, `scr score`, queues/*.md and the records.\n\n" + _graph_context(root, repo, wl)
        return Session(item, "operator", root, body + conclude, "general")
    # a task id
    t = store.get(item)
    body = _task_context(root, item)
    wt = task_worktree(wl, t)
    if t.rec.get("queue") == "omri":
        role, cwd = "concierge", repo
    elif t.status in ("escalated", "omri_queue") or _open_help(root, t):
        role, cwd = "steward", (wt if wt.exists() else repo)
    else:
        role, cwd = "steward", (wt if wt.exists() else repo)
    if cwd == wt:
        body += f"\n\n## Where you are\nThis session runs inside the task's worktree `{wt}` (branch task/{item}); the Lean and the mission files are here."
    return Session(item, role, cwd, body + conclude, f"{item} {t.rec.get('title', '')[:40]}")


def brief(root: Path, item: str) -> str:
    return build(root, item).brief


def _system_prompt(root: Path, s: Session) -> str:
    name, text = ROLES[s.role]
    return (text.format(root=root) + f" Scriptorium root: {root}. Workload repo: {workload(root).repo}. "
            "Start by reading the brief file you are pointed at, IN FULL, then open with a five-line summary of the ticket, "
            "the options, and your recommendation. Quote paths and line numbers; say when something is not in the records.")


def discuss(root: Path, item: str, open_terminal: bool = True) -> list[str]:
    s = build(root, item)
    bdir = root / ".scr" / "discuss"
    bdir.mkdir(parents=True, exist_ok=True)
    bf = bdir / f"{item.replace(':', '-')}-{int(time.time())}.md"
    bf.write_text(s.brief, encoding="utf-8")
    sp = bf.with_suffix(".system.txt")
    sp.write_text(_system_prompt(root, s), encoding="utf-8")
    EventLog(root / "events" / "events.jsonl").append("system", name="discuss", actor="omri", task=item if item.startswith("T-") else None,
                                                     note=f"{item}: {ROLES[s.role][0]} in {s.cwd.name}; brief {bf.name}")
    out = [f"role: {ROLES[s.role][0]}", f"folder: {s.cwd}", f"brief: {bf}"]
    if not open_terminal:
        return out + [s.brief]
    exe = claude_bin()
    if not exe:
        return out + ["claude (Claude Code CLI) not on PATH — open a terminal in that folder and paste the brief"]
    first = f"Read {bf} in full now (it is your brief for this session), then begin as it says."
    add_dirs = [str(root), str(workload(root).repo)]
    if os.name == "nt":
        # A launcher SCRIPT, not an inline command: Windows Terminal splits its command line on ';'
        # (seen 2026-09-16: it tried to run `" claude ...` as a program). The system prompt is read
        # from a file inside the script, so nothing long ever sits on a command line.
        ps1 = bf.with_suffix(".launch.ps1")
        ps1.write_text("\ufeff" + f"Set-Location -LiteralPath '{s.cwd}'\n"
                       f"$sys = Get-Content -Raw -Encoding UTF8 '{sp}'\n"
                       f"claude --permission-mode acceptEdits --add-dir '{add_dirs[0]}' --add-dir '{add_dirs[1]}' --append-system-prompt $sys '{first}'\n",
                       encoding="utf-8")
        title = s.title.replace(";", ",").replace('"', "'")
        if shutil.which("wt"):
            subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", title, "-d", str(s.cwd), "powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(ps1)])
        else:
            subprocess.Popen(["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], cwd=s.cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]
        out.append(f"terminal opened: {title}")
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("xterm")
        line = f"cd \"{s.cwd}\" && {exe} --permission-mode acceptEdits --add-dir \"{add_dirs[0]}\" --add-dir \"{add_dirs[1]}\" --append-system-prompt \"$(cat \"{sp}\")\" \"{first}\""
        if term:
            subprocess.Popen([term, "-e", f"bash -c '{line}'"])
            out.append("terminal opened")
        else:
            out.append("no terminal emulator found; run: " + line)
    return out
