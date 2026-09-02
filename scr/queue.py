"""Queue views. queues/*.md are regenerated from records + graph; never a source of truth."""
from __future__ import annotations

from pathlib import Path

from .events import now_iso
from .graph import Graph
from .score import rank_ready

INBOX_MARK = "## Inbox"


def render_scribers(g: Graph, weights: dict, limits: dict) -> str:
    rows = rank_ready(g, weights, "scribers")
    cap = int(limits.get("concurrency", 3))
    chosen = g.independent_set([r["task"] for r in rows], cap)
    lines = [
        "# Scriber queue", "",
        f"Regenerated {now_iso()} — view only; edit task records, not this file.", "",
        f"Next dispatch (cap {cap}, conflict-free): " + (", ".join(chosen) if chosen else "—"), "",
        "| rank | task | score | unlock | path | breadth | verif | risk | att | title |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, r in enumerate(rows, 1):
        t = g.tasks[r["task"]]
        tm = r["terms"]
        flag = " ⚑stmt" if ((t.rec.get("scope") or {}).get("may_touch_statements")) else ""
        lines.append(f"| {i} | {r['task']} | {r['score']:.2f} | {tm['unlock_value']:.1f} | {tm['critical_path_share']:.2f} | "
                     f"{int(tm['blocking_breadth'])} | {tm['verifiability']:.1f} | {tm['risk']:.1f} | {int(tm['attempts'])} | {t.rec.get('title','')}{flag} |")
    others = [t for t in g.tasks.values() if t.rec.get("queue") == "scribers" and t.is_open and t.id not in {r["task"] for r in rows} and t.rec.get("kind") != "milestone"]
    if others:
        lines += ["", "## Not ready", ""]
        for t in sorted(others, key=lambda t: t.id):
            bl = [a for a in t.rec.get("blocked_by") or [] if a in g.tasks and not g.tasks[a].is_done]
            lines.append(f"- {t.id} `{t.status}` {t.rec.get('title','')}" + (f" — waits on {', '.join(bl)}" if bl else ""))
    return "\n".join(lines) + "\n"


def render_omri(g: Graph, existing: str | None) -> str:
    items = [t for t in g.tasks.values() if t.rec.get("queue") == "omri" and t.is_open]
    items.sort(key=lambda t: -len([d for d in g.descendants(t.id) if g.tasks[d].is_open]))
    lines = ["# Omri's queue", "", f"Regenerated {now_iso()} — answer inline under each item; the Concierge applies answers.", ""]
    for t in items:
        waiting = [d for d in g.descendants(t.id) if g.tasks[d].is_open]
        ms = [d for d in waiting if g.tasks[d].rec.get("kind") == "milestone"]
        lines.append(f"### {t.id} · {t.rec.get('kind')} · {t.rec.get('title','')}")
        lines.append(f"Waiting on this: {len(waiting) - len(ms)} tasks, {len(ms)} milestones. Status `{t.status}`.")
        lines.append(f"Brief: `{t.dir.name}/spec.md`")
        lines.append("")
        lines.append("Answer:")
        lines.append("")
    # preserve the human inbox verbatim
    inbox = ""
    if existing and INBOX_MARK in existing:
        inbox = existing[existing.index(INBOX_MARK):]
    if not inbox.strip():
        inbox = INBOX_MARK + "\n\nOne line per idea. Intake turns these into task records and removes them.\n"
    return "\n".join(lines) + "\n" + inbox


def write_queues(root: Path, g: Graph, weights: dict, limits: dict) -> None:
    qd = root / "queues"
    qd.mkdir(exist_ok=True)
    (qd / "scribers.md").write_text(render_scribers(g, weights, limits), encoding="utf-8")
    op = qd / "omri.md"
    (op).write_text(render_omri(g, op.read_text(encoding="utf-8") if op.exists() else None), encoding="utf-8")
