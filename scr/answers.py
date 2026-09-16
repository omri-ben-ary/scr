"""Recording Omri's answers (decisions, HELP replies, gate decisions) where the agents look for
them, and kicking off the Concierge to apply consequences. Used by the UI, by `scr answer`, and
by the Discuss sessions, so every path writes the same records."""
from __future__ import annotations

import re
import time
from pathlib import Path

from .events import now_iso
from .run import claude_bin, scr_cmd, start_claude
from .store import RepoError, Store

GATE_TARGETS = {"accept": "accepted", "return": "triaged", "reject": "rejected"}


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def apply(root: Path, tid: str, answer: str, kind: str, actor: str = "omri", concierge: bool = True) -> dict:
    store = Store(root)
    t = store.get(tid)
    d = t.dir
    ts = now_iso()
    msgs: list[str] = []
    if kind == "help":
        with open(d / "log.md", "a", encoding="utf-8") as f:
            f.write(f"\n- {ts} [steward] ({actor}, via UI) {answer}\n")
        msgs.append(scr_cmd(root, actor, "event", "system", "--name", "help_answered", "--task", tid, "--note", answer[:500], "--quiet")[1])
    elif kind == "decision":
        q = root / "queues" / "omri.md"
        txt = _read(q)
        pat = re.compile(rf"(### {re.escape(tid)} ·.*?\nAnswer:)\n", re.S)
        if pat.search(txt):
            q.write_text(pat.sub(lambda m: m.group(1) + f" {answer}\n", txt, count=1), encoding="utf-8")
        with open(d / "log.md", "a", encoding="utf-8") as f:
            f.write(f"\n- {ts} [{actor}] answer: {answer}\n")
        msgs.append(scr_cmd(root, actor, "event", "system", "--name", "answer", "--task", tid, "--note", answer[:500], "--quiet")[1])
        msgs.append(scr_cmd(root, "concierge", "task", "set", tid, "closed", "--note", f"answered via UI: {answer[:200]}", "--quiet")[1])
    elif kind == "gate":
        target = GATE_TARGETS.get(answer.split()[0].lower()) if answer.strip() else None
        if target:
            if t.status == "escalated":
                scr_cmd(root, actor, "task", "set", tid, "omri_queue", "--quiet")
            msgs.append(scr_cmd(root, actor, "task", "set", tid, target, "--note", f"Gate decision via UI: {answer[:200]}", "--quiet")[1])
            if target == "triaged":
                scr_cmd(root, actor, "task", "set", tid, "ready", "--note", f"returned by Omri: {answer[:200]}", "--quiet")
        else:
            with open(d / "log.md", "a", encoding="utf-8") as f:
                f.write(f"\n- {ts} [{actor}] gate note: {answer}\n")
            msgs.append(scr_cmd(root, actor, "event", "system", "--name", "gate_note", "--task", tid, "--note", answer[:500], "--quiet")[1])
    else:
        raise RepoError(f"unknown answer kind {kind}")
    if concierge and kind != "help":
        start_concierge(root, tid, answer)
    return {"ok": True, "log": [m for m in msgs if m]}


def start_concierge(root: Path, tid: str, answer: str) -> int | None:
    """Detached headless Concierge run: apply the consequences of an answer (new tasks, edges)."""
    if not claude_bin():
        return None
    brief = (f"You are the Concierge (read {root / 'agents' / 'concierge.md'} and agents/_common.md and follow them). "
             f"Omri answered {tid} with: {answer!r}. The decision brief is tasks/{Store(root).get(tid).dir.name}/spec.md. "
             f"Apply the answer with scr calls (--actor concierge, run `{root / '.venv' / 'Scripts' / 'scr.exe'}` from {root}): unblock or create the tasks "
             f"the answer implies (scr task new ... --origin {tid}), add edges, log a system event 'concierge_applied' with --task {tid}. "
             f"The record itself is already closed. Never edit Lean. The scriptorium tree journals itself: never run git here. Finish with `scr verify`.")
    bf = root / ".scr" / f"concierge-{tid}-{int(time.time())}.txt"
    bf.parent.mkdir(exist_ok=True)
    bf.write_text(brief, encoding="utf-8")
    return start_claude(root, bf, root / "reports" / f"concierge-{tid}-{int(time.time())}.txt", permission_mode="acceptEdits")


def add_inbox(root: Path, text: str, actor: str = "omri") -> dict:
    q = root / "queues" / "omri.md"
    txt = _read(q)
    if "## Inbox" not in txt:
        txt += "\n## Inbox\n\n"
    q.write_text(txt.rstrip("\n") + f"\n- {text.strip()}\n", encoding="utf-8")
    scr_cmd(root, actor, "event", "system", "--name", "inbox", "--note", text[:500], "--quiet")
    return {"ok": True}
