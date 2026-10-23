"""Known issues: a bug found between releases goes on one list instead of becoming a hotfix on the spot.

The list is event-sourced. `issue_add` and `issue_close` system events in the hash-chained log are the record, and
queues/known-issues.md is the view, rewritten from them on every change. An issue is triaged when the next release is
cut: it becomes a tooling task, or it is closed. `--urgent` files that task at once, at the top of the queue (the fast
track for a real outage: the fix still goes through a PR and CI).
"""
from __future__ import annotations

from pathlib import Path

from .events import EventLog

VIEW = Path("queues") / "known-issues.md"


def _log(root: Path) -> EventLog:
    return EventLog(root / "events" / "events.jsonl")


def issues(root: Path) -> list[dict]:
    """Every issue, oldest first: {id, text, urgent, task, opened, actor, status, closed, note}."""
    out: dict[str, dict] = {}
    for e in _log(root).read():
        if e.get("kind") != "system":
            continue
        d = e.get("data") or {}
        if e.get("name") == "issue_add" and d.get("id"):
            out[d["id"]] = {"id": d["id"], "text": d.get("text", ""), "urgent": bool(d.get("urgent")), "task": d.get("task"),
                            "opened": e.get("ts"), "actor": e.get("actor"), "status": "open", "closed": None, "note": None}
        elif e.get("name") == "issue_close" and d.get("id") in out:
            out[d["id"]].update(status="closed", closed=e.get("ts"), note=d.get("note"))
    return list(out.values())


def next_id(root: Path) -> str:
    nums = [int(i["id"].split("-")[1]) for i in issues(root) if i["id"].startswith("I-") and i["id"][2:].isdigit()]
    return f"I-{(max(nums) + 1) if nums else 1:04d}"


def render(root: Path) -> str:
    items = issues(root)
    open_, closed = [i for i in items if i["status"] == "open"], [i for i in items if i["status"] == "closed"]
    lines = ["# Known issues", "",
             "Bugs found between releases (`scr issue add`). Triaged when the next release is cut: each becomes a tooling",
             "task or is closed (`scr issue close <id> --note ...`). Generated from the event log; do not edit.", "",
             f"## Open ({len(open_)})", ""]
    lines += [f"- **{i['id']}**{' (urgent)' if i['urgent'] else ''} {i['text']}"
              + (f" → {i['task']}" if i["task"] else "") + f"  _(opened {i['opened']}, {i['actor']})_" for i in open_] or ["_none_"]
    lines += ["", f"## Closed ({len(closed)})", ""]
    lines += [f"- {i['id']} {i['text']} — {i['note'] or 'closed'} _({i['closed']})_" for i in closed] or ["_none_"]
    return "\n".join(lines) + "\n"


def write_view(root: Path) -> Path:
    p = root / VIEW
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(root), encoding="utf-8")
    return p


def add(root: Path, text: str, actor: str, urgent: bool = False, task: str | None = None) -> dict:
    text = " ".join(text.split())
    if not text:
        raise ValueError("an issue needs a description")
    iid = next_id(root)
    _log(root).append("system", name="issue_add", actor=actor, note=f"{iid}: {text}"[:500],
                      data={"id": iid, "text": text, "urgent": urgent, "task": task})
    write_view(root)
    return {"id": iid, "text": text, "urgent": urgent, "task": task}


def close(root: Path, iid: str, actor: str, note: str) -> dict:
    found = next((i for i in issues(root) if i["id"] == iid), None)
    if found is None:
        raise KeyError(f"no issue {iid}")
    if found["status"] == "closed":
        raise ValueError(f"{iid} is already closed")
    _log(root).append("system", name="issue_close", actor=actor, note=f"{iid}: {note}"[:500], data={"id": iid, "note": note})
    write_view(root)
    return {**found, "status": "closed", "note": note}


def tooling_workload(workloads: dict) -> str | None:
    """Where an urgent issue's task goes: the `scr` workload (the code repo) once configured, else the old
    `scriptorium` one, else none (the task is filed without a workload and Intake places it)."""
    for name in ("scr", "scriptorium"):
        if name in (workloads or {}):
            return name
    return None
