"""Discuss a ticket: open an interactive Claude Code session in its own terminal window, preloaded
with everything about that ticket, that (a) helps Omri decide and (b) carries out the decision
with scr once he says the conversation is concluded.

Items: a task id (T-0031), `chore:<id>`, `event:<hash>` (a system alarm), or `general`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .events import EventLog
from .run import claude_bin, scr_exe
from .store import Store

PROTOCOL = """
## How this session works
You are Omri's discussion partner for ONE ticket of his Scriptorium research operation (root: {root};
run scr as `{scr} --actor omri ...` from that folder). Everything you need is quoted below; read the
files it names for more (task records under {root}/tasks, the Lean repo at {repo}, read-only).
1. Start by summarising the ticket in five lines: what is being asked, what the options are, and what
   you would recommend and why. Then discuss. Be concrete, quote paths and line numbers, say when
   something is not in the records. Never edit Lean or mission files; never run git in the
   scriptorium folder (it journals itself).
2. When Omri says the conversation is concluded (any of: "done", "go ahead", "apply", "that's the
   decision"), restate the decision(s) in one line each, and CARRY THEM OUT with scr:
     answers:   `{scr} --actor omri answer <task> --kind decision|help|gate --text "<answer>"`
     chores:    `{scr} --actor omri chore run <id>`
     new ideas: `{scr} --actor omri inbox "<one line>"`
     status:    `{scr} --actor omri task set <id> <status> --note "<why>"`
     dismiss an alarm: `{scr} --actor omri ack <hash>`
   then run `{scr} verify` and print a two-line summary. Do not carry out anything he has not decided.
"""


def _read(p: Path, limit: int = 60_000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _task_context(root: Path, tid: str) -> str:
    t = Store(root).get(tid)
    d = t.dir
    parts = [f"# Ticket {tid}: {t.rec.get('title', '')}", f"status: {t.status}   kind: {t.rec.get('kind')}   queue: {t.rec.get('queue')}",
             "```yaml\n" + _read(d / "task.yaml", 8000) + "\n```"]
    for name in ("spec.md", "report.md", "review.md", "log.md"):
        txt = _read(d / name, 40_000)
        if txt.strip():
            parts.append(f"## {name} ({d / name})\n" + txt)
    evs = [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("task") == tid][-40:]
    if evs:
        parts.append("## Recent events\n" + "\n".join(json.dumps({k: v for k, v in e.items() if k not in ('h', 'prev')}) for e in evs))
    return "\n\n".join(parts)


def brief(root: Path, item: str) -> str:
    store = Store(root)
    wls = store.config("workloads") or {}
    name = next(iter(wls), "")
    repo = (root / (wls.get(name) or {}).get("repo", "../" + name)).resolve()
    scr = " ".join(scr_exe(root))
    head = PROTOCOL.format(root=root, scr=scr, repo=repo)
    if item.startswith("chore:"):
        from .chores import load
        c = next((x for x in load(root) if x["id"] == item[6:]), None)
        body = f"# Chore {item[6:]}\n" + (json.dumps(c, indent=1, default=str) if c else "(unknown chore)")
    elif item.startswith("event:"):
        h = item[6:]
        e = next((e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("h", "").startswith(h)), None)
        body = "# System alarm\n" + json.dumps(e, indent=1) if e else "(unknown event)"
        if e and e.get("task"):
            body += "\n\n" + _task_context(root, e["task"])
    elif item == "general":
        body = "# General discussion\nNo single ticket; use `scr graph analyze`, `scr score`, queues/*.md and the records."
    else:
        body = _task_context(root, item)
    return head + "\n\n" + body


def discuss(root: Path, item: str, open_terminal: bool = True) -> list[str]:
    text = brief(root, item)
    bdir = root / ".scr" / "discuss"
    bdir.mkdir(parents=True, exist_ok=True)
    bf = bdir / f"{item.replace(':', '-')}-{int(time.time())}.md"
    bf.write_text(text, encoding="utf-8")
    EventLog(root / "events" / "events.jsonl").append("system", name="discuss", actor="omri", task=item if item.startswith("T-") else None, note=f"{item}: {bf.name}")
    out = [f"brief: {bf}"]
    if not open_terminal:
        return out + [text]
    exe = claude_bin()
    if not exe:
        return out + ["claude (Claude Code CLI) not on PATH — open a terminal in the scriptorium folder and paste the brief"]
    store = Store(root)
    wls = store.config("workloads") or {}
    name = next(iter(wls), "")
    repo = (root / (wls.get(name) or {}).get("repo", "../" + name)).resolve()
    if os.name == "nt":
        cmd = f"claude --permission-mode acceptEdits --add-dir '{repo}' (Get-Content -Raw -Encoding UTF8 '{bf}')"
        if shutil.which("wt"):
            subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", f"discuss {item}", "-d", str(root), "powershell", "-NoExit", "-Command", cmd])
        else:
            subprocess.Popen(["powershell", "-NoExit", "-Command", cmd], cwd=root, creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]
        out.append(f"terminal opened: discuss {item}")
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("xterm")
        if term:
            subprocess.Popen([term, "-e", f"bash -c 'cd \"{root}\" && {exe} --permission-mode acceptEdits --add-dir \"{repo}\" \"$(cat \"{bf}\")\"'"])
            out.append("terminal opened")
        else:
            out.append(f"no terminal emulator found; run: cd {root} && claude --add-dir {repo} \"$(cat {bf})\"")
    return out
