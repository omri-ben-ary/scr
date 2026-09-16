"""Discuss a ticket (or plan new work): open an interactive Claude Code session in its own terminal
window, with the RIGHT role, in the RIGHT folder, that (a) helps Omri decide and (b) carries the
decision out with scr once he says so.

The flow (2026-09-16, after Omri's review of the first version):
  1. The session runs AS a named agent (`claude --agent scriptorium-<role>`), so the terminal shows
     which role is speaking. The agent files are (re)written into ~/.claude/agents/ at launch time
     from ROLES below, with the scriptorium and workload paths filled in.
  2. The session WAITS FOR OMRI FIRST. Its opening turn is two lines (who it is, what its brief
     covers); it reads nothing else until Omri says what he wants, and then reads only what that
     needs — the brief is an index that points at the records, not a dump of them.
  3. The brief is a FILE: a Windows command line is capped at 32k characters and an inline brief
     silently truncated (seen 2026-09-16). It is compact by design: record paths, one-line rows,
     open questions, the exact scr commands — never whole reports or the whole roadmap.

Items and their sessions (role / working directory):
  T-xxxx with an open HELP:     Steward, in the task's worktree (the code the scriber is stuck on)
  T-xxxx escalated / gate       Steward, in the task's worktree (review.md + the diff are there)
  T-xxxx decision (omri queue)  Concierge, in the workload repo (notes/, missions/, the Lean)
  T-xxxx other                  Steward, in its worktree if it exists, else the workload repo
  chore:<id>                    Operator, in the scriptorium root
  event:<hash> / events:<grp>   Steward, in the task's worktree if any, else the scriptorium root
  plan                          Intake + Prioritizer, in the workload repo (what to work on next)
  general                       Operator, in the scriptorium root
"""
from __future__ import annotations

import json
import os
import re
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
  dismiss alarm  {scr} --actor omri ack <key>              (a card's key: `events:<task>:<kind>:<ts>` dismisses the whole group)
  finally        {scr} verify
The scriptorium tree journals itself after every scr call: never run git in {root}. Never edit Lean,
mission files, notes, tooling or config in this session — you never implement a fix here; that is a task
(workload `scriptorium` for the operation's own tooling), and the commands above are the only changes you make.
"""

ROLES = {
    "steward": ("Steward", "You are the Steward of the Scriptorium research operation (read {root}/agents/steward.md and "
                "{root}/agents/_common.md when Omri's request calls for it). You judge submissions and stuck tasks against "
                "their spec and the mechanical checks, and you answer scribers' HELP: questions with precise, honest "
                "guidance. You are in the task's worktree: read the actual code, run `lake build` or the workload's "
                "checkers when it helps."),
    "concierge": ("Concierge", "You are the Concierge of the Scriptorium research operation (read {root}/agents/concierge.md and "
                  "{root}/agents/_common.md when Omri's request calls for it). Omri's queue holds decisions about research "
                  "direction; you make each one a one-screen choice: the question, the options, what each unlocks, your "
                  "recommendation, and you apply the answer."),
    "intake": ("Intake + Prioritizer", "You are Intake and the Prioritizer of the Scriptorium research operation (read {root}/agents/intake.md, "
               "{root}/agents/prioritizer.md and {root}/agents/_common.md when Omri's request calls for it). You turn a "
               "conversation about what to work on next into well-formed task records with real scope, mechanical "
               "done-conditions and edges, deduplicated against the open graph, and you say what you would dispatch first and why."),
    "operator": ("Operator", "You are the operator's assistant for the Scriptorium research operation (read {root}/agents/_common.md "
                 "when Omri's request calls for it). You know the records, the event log and the chores; you explain and "
                 "you execute exactly what Omri decides."),
}

FLOW = """Scriptorium root: {root}. Workload repo: {repo}. Your brief (an INDEX of this ticket, not the records
themselves): {brief}

How this session runs (Omri's rule, 2026-09-16):
- Your ONLY output is a task list Omri approves, which you then record with scr. You never implement a fix in
  this session — not in tooling, Lean, notes, mission files or config. A fix that is needed is a task; you
  write its spec, you do not make the change.
- Omri speaks first. Your opening turn is two lines: who you are (role) and what the brief covers — read only
  the brief's first section for that — then stop and wait. Do not read records, reports, the roadmap or code
  before he has said what he wants.
- When he has, read the brief's relevant section and follow its paths to the records you actually need
  (spec.md, report.md, review.md, log.md, the events, the code). Ask a short question if the request is
  ambiguous; otherwise answer with an assessment, options and a recommendation. Quote paths and line numbers;
  say when something is not in the records.
- You act only on what he decides, with scr, as the brief's last section says. Nothing is created, changed or
  dismissed before he concludes, and nothing but records is ever changed."""


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


def _section(text: str, heading: str, limit: int = 3000) -> str:
    """The body of a `# heading` / `## heading` markdown section, or ''."""
    m = re.search(rf"(?ms)^#{{1,3}}\s*{re.escape(heading)}\s*$\n(.*?)(?=^#{{1,3}}\s|\Z)", text)
    return m.group(1).strip()[:limit] if m else ""


def _events_for(root: Path, tid: str, n: int = 12) -> str:
    evs = [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("task") == tid][-n:]
    rows = []
    for e in evs:
        what = e.get("name") or (f"{e.get('from')} -> {e.get('to')}" if e.get("kind") == "state" else e.get("kind"))
        rows.append(f"- {e.get('ts')} [{e.get('actor')}] {what}" + (f": {e.get('note')}" if e.get("note") else ""))
    return "\n".join(rows)


def _help_lines(text: str) -> list[str]:
    from .ui import _help_lines as hl
    return [h["text"] for h in hl(text)]


def _task_context(root: Path, tid: str) -> str:
    t = Store(root).get(tid)
    d, rec = t.dir, t.rec
    sc = rec.get("scope") or {}
    b = rec.get("budget") or {}
    head = [f"# Ticket {tid}: {rec.get('title', '')}",
            f"status: {t.status}   kind: {rec.get('kind')}   queue: {rec.get('queue')}   attempts: {rec.get('attempts', 0)}/{b.get('max_attempts', '?')}"
            f"   origin: {rec.get('origin')}   may_touch_statements: {sc.get('may_touch_statements', False)}",
            f"scope files: {', '.join(sc.get('files') or []) or '(none set)'}",
            f"scope decls: {', '.join(sc.get('decls') or []) or '(none set)'}",
            f"blocked_by: {', '.join(rec.get('blocked_by') or []) or 'none'}   branch: {rec.get('branch') or '-'}   worktree: {rec.get('worktree') or '-'}",
            f"record folder: {d}  (task.yaml, spec.md, report.md, review.md, log.md)"]
    parts = ["\n".join(head)]
    spec = _read(d / "spec.md", 12_000)
    if spec.strip():
        parts.append(f"## spec.md — the binding contract ({d / 'spec.md'})\n" + spec)
    log = _read(d / "log.md", 200_000)
    helps = _help_lines(log)
    if helps:
        parts.append("## Open HELP: lines in log.md (unanswered)\n" + "\n".join(f"- {h}" for h in helps))
    review = _read(d / "review.md", 40_000)
    dec = re.search(r"(?m)^##\s*Decision\s*$\n(.*?)(?=^##|\Z)", review, re.S)
    if dec and dec.group(1).strip() and "accepted | returned" not in dec.group(1):
        parts.append(f"## review.md — Decision ({d / 'review.md'})\n" + dec.group(1).strip()[:1500])
    report = _read(d / "report.md", 60_000)
    res = _section(report, "Result", 1200)
    if res and not res.startswith("What was proved"):
        parts.append(f"## report.md — Result, first lines ({d / 'report.md'}; its `unlocks` block is at the end)\n" + res)
    ev = _events_for(root, tid)
    if ev:
        parts.append("## Last events\n" + ev)
    return "\n\n".join(parts)


def _open_help(root: Path, t) -> bool:
    return bool(_help_lines(_read(t.dir / "log.md", 200_000)))


def _unlock_count(report: str) -> int:
    blk = report[report.find("## unlocks"):] if "## unlocks" in report else ""
    return len(re.findall(r"(?m)^\s*-\s*title:", blk))


def _roadmap_open(path: Path) -> str:
    """Only the open `- [ ]` boxes of the roadmap, each under its nearest heading, with line numbers."""
    rows, heading = [], ""
    for i, line in enumerate(_read(path, 400_000).splitlines(), 1):
        if line.startswith("## "):
            heading = line[3:].strip()
        elif line.lstrip().startswith("- [ ]"):
            rows.append(f"- L{i} [{heading[:60]}] {line.strip()[5:].strip()[:160]}")
    return "\n".join(rows)


def _graph_context(root: Path, repo: Path, wl) -> str:
    store = Store(root)
    tasks = store.load_all()
    rows = []
    for t in sorted(tasks.values(), key=lambda t: t.id):
        if t.is_open:
            sc = (t.rec.get("scope") or {})
            rows.append(f"- {t.id} `{t.status}` [{t.rec.get('kind')}] {t.rec.get('title', '')}"
                        + (f" — files: {', '.join(sc.get('files') or [])[:160]}" if sc.get("files") else "")
                        + (f" — blocked_by {', '.join(t.rec.get('blocked_by'))}" if t.rec.get("blocked_by") else "")
                        + f" — record: {t.dir.name}")
    parts = ["# Open tasks (the graph you must not duplicate; records under " + str(root / "tasks") + ")\n" + "\n".join(rows)]
    q = _read(root / "queues" / "scribers.md", 20_000)
    if q:
        parts.append("## Scriber queue (ranked by `scr score`; view only)\n" + q)
    omri = _read(root / "queues" / "omri.md", 20_000)
    if omri:
        parts.append("## Omri's queue and inbox (queues/omri.md)\n" + omri)
    closed = sorted((t for t in tasks.values() if t.status in ("merged", "closed", "rejected")),
                    key=lambda t: t.rec.get("updated") or "", reverse=True)[:12]
    if closed:
        parts.append("## Recently finished — their reports' `unlocks` blocks are PROPOSALS (read on demand)\n" + "\n".join(
            f"- {t.id} `{t.status}` {t.rec.get('title', '')[:110]} — {_unlock_count(_read(t.dir / 'report.md'))} proposal(s) — {t.dir / 'report.md'}"
            for t in closed))
    rm = wl.cfg.get("roadmap")
    if rm and (repo / rm).exists():
        parts.append(f"## Roadmap open boxes ({repo / rm}; read the file for context)\n" + (_roadmap_open(repo / rm) or "(none open)"))
    parts.append("## Where things are\n"
                 f"- agents: {root / 'agents'}  (intake.md, prioritizer.md, steward.md, concierge.md, _common.md)\n"
                 f"- limits and weights: {root / 'config' / 'limits.yaml'}, {root / 'config' / 'weights.yaml'}\n"
                 f"- workload config: {root / 'config' / 'workloads.yaml'}  (main_branch, worktrees, checkers)\n"
                 f"- spec template: {root / 'templates' / 'spec.md'}\n"
                 f"- events: {root / 'events' / 'events.jsonl'}  (`scr replay`, `scr graph analyze`, `scr score`)\n"
                 f"- workload notes and missions: {repo / 'notes'}, {repo / 'missions'}, {repo / 'docs'}")
    return "\n\n".join(parts)


def _event_context(root: Path, store: Store, wl, item: str) -> Session:
    """`event:<hash>` (one event) or `events:<task>:<name>:<ts>` (a UI card grouping every event of that
    task and kind up to <ts>)."""
    evs = EventLog(root / "events" / "events.jsonl").read()
    if item.startswith("events:"):
        _, task, name, ts = (item.split(":", 3) + ["", "", ""])[:4]
        sel = [e for e in evs if e.get("kind") == "system" and e.get("name") == name and (e.get("task") or "-") == task and (e.get("ts") or "") <= ts]
        body = f"# System alarm group: {name} ×{len(sel)} on {task} (card key `{item}`; dismiss with `scr ack {item}`)\n"
        body += "\n".join(f"- {e.get('ts')} {e.get('note') or json.dumps(e.get('data') or {})}" for e in sel[-40:])
        if len(sel) > 40:
            body += f"\n… {len(sel) - 40} earlier"
        tid = task if task.startswith("T-") else None
        title = f"alarm {name} {task}"
    else:
        h = item[6:]
        e = next((e for e in evs if (e.get("h") or "").startswith(h)), None)
        body = "# System alarm\n```json\n" + (json.dumps(e, indent=1) if e else "(unknown event)") + "\n```"
        tid = e.get("task") if e else None
        title = f"alarm {h[:8]}"
    cwd = root
    if tid and tid in store.load_all():
        body += "\n\n" + _task_context(root, tid)
        wt = task_worktree(wl, store.get(tid))
        cwd = wt if wt.exists() else wl.repo
    return Session(item, "steward", cwd, body, title)


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
    if item.startswith("event:events:"):
        item = item[6:]
    if item.startswith(("event:", "events:")):
        s = _event_context(root, store, wl, item)
        return Session(s.item, s.role, s.cwd, s.brief + conclude, s.title)
    if item == "plan":
        body = ("# Planning session: what should the operation work on next?\n"
                "Omri will say what he wants — directions, results, things that bother him — and you turn that into a LIST of "
                "candidate tasks (title, kind, scope files/decls, mechanical done-conditions, blockers, origin), each checked "
                "against the open graph below (dedup!) and ranked the way the Prioritizer would. When he concludes, create the "
                "ones he approves as records — each with a filled spec.md — set them triaged/ready, and add the edges. "
                "Below is the index of the operation's current state; open the records it points at as his requests need them.\n\n"
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


def agent_name(role: str) -> str:
    return f"scriptorium-{role}"


def _system_prompt(root: Path, s: Session, brief_file: Path) -> str:
    name, text = ROLES[s.role]
    return text.format(root=root) + "\n\n" + FLOW.format(root=root, repo=workload(root).repo, brief=brief_file)


def install_agents(root: Path, agents_dir: Path | None = None) -> list[Path]:
    """(Re)write ~/.claude/agents/scriptorium-<role>.md for every role, so `claude --agent` can name the
    session's role. Returns the files written."""
    agents_dir = agents_dir or Path.home() / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    repo = workload(root).repo
    out = []
    for role, (name, text) in ROLES.items():
        p = agents_dir / f"{agent_name(role)}.md"
        body = (f"---\nname: {agent_name(role)}\ndescription: Scriptorium {name} — interactive session opened by the Scriptorium UI's Discuss button; "
                f"waits for Omri, then works from the ticket's brief. Written by scr discuss; do not hand-edit.\n---\n"
                f"{text.format(root=root)}\n\nScriptorium root: {root}. Workload repo: {repo}. The session's brief and flow rules arrive "
                f"in the appended system prompt; follow them. You never implement a fix in a Discuss session: your only "
                f"output is a task list Omri approves, recorded with scr.\n")
        if _read(p) != body:
            p.write_text(body, encoding="utf-8")
        out.append(p)
    return out


def discuss(root: Path, item: str, open_terminal: bool = True) -> list[str]:
    s = build(root, item)
    bdir = root / ".scr" / "discuss"
    bdir.mkdir(parents=True, exist_ok=True)
    bf = bdir / f"{item.replace(':', '-')}-{int(time.time())}.md"
    bf.write_text(s.brief, encoding="utf-8")
    sp = bf.with_suffix(".system.txt")
    sp.write_text(_system_prompt(root, s, bf), encoding="utf-8")
    agent = None
    try:
        install_agents(root)
        agent = agent_name(s.role)
    except OSError as e:  # the session still works without a named agent
        agent_err = f"agent files not written ({e}); launching without --agent"
    EventLog(root / "events" / "events.jsonl").append("system", name="discuss", actor="omri", task=item if item.startswith("T-") else None,
                                                     note=f"{item}: {ROLES[s.role][0]} in {s.cwd.name}; brief {bf.name}")
    out = [f"role: {ROLES[s.role][0]}" + (f" (agent {agent})" if agent else ""), f"folder: {s.cwd}", f"brief: {bf}"]
    if not agent:
        out.append(agent_err)
    if not open_terminal:
        return out + [s.brief]
    exe = claude_bin()
    if not exe:
        return out + ["claude (Claude Code CLI) not on PATH — open a terminal in that folder and paste the brief"]
    first = (f"Say in two lines who you are and what your brief covers (read only the first section of {bf} for that), "
             "then wait for Omri's instruction.")
    add_dirs = [str(root), str(workload(root).repo)]
    agent_flag = f"--agent {agent} " if agent else ""
    if os.name == "nt":
        # A launcher SCRIPT, not an inline command: Windows Terminal splits its command line on ';'
        # (seen 2026-09-16: it tried to run `" claude ...` as a program). The system prompt is read
        # from a file inside the script, so nothing long ever sits on a command line.
        ps1 = bf.with_suffix(".launch.ps1")
        ps1.write_text("﻿" + f"Set-Location -LiteralPath '{s.cwd}'\n"
                       f"$sys = Get-Content -Raw -Encoding UTF8 '{sp}'\n"
                       f"claude {agent_flag}--permission-mode acceptEdits --add-dir '{add_dirs[0]}' --add-dir '{add_dirs[1]}' --append-system-prompt $sys '{first}'\n",
                       encoding="utf-8")
        title = (f"{ROLES[s.role][0]}: {s.title}").replace(";", ",").replace('"', "'")
        if shutil.which("wt"):
            subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", title, "-d", str(s.cwd), "powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(ps1)])
        else:
            subprocess.Popen(["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], cwd=s.cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]
        out.append(f"terminal opened: {title}")
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("xterm")
        line = (f"cd \"{s.cwd}\" && {exe} {agent_flag}--permission-mode acceptEdits --add-dir \"{add_dirs[0]}\" --add-dir \"{add_dirs[1]}\" "
                f"--append-system-prompt \"$(cat \"{sp}\")\" \"{first}\"")
        if term:
            subprocess.Popen([term, "-e", f"bash -c '{line}'"])
            out.append("terminal opened")
        else:
            out.append("no terminal emulator found; run: " + line)
    return out
