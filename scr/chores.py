"""Chores: one-off things that show up in the UI under "Needs you" until an OUTCOME check passes.

ui/chores.yaml entries:
  id, title, why                      shown on the card
  steps: [[scr args...], ...]         run in order by `scr chore run <id>` (each step is an scr
                                      command, so each is journaled and testable); a step may also
                                      be {"py": "module:function"} for an operation in this package
  manual: true                        no steps; Omri does it and presses "Mark done"
  link: url                           optional
  done: {kind: ...}                   declarative outcome check (see CHECKS); never "is git clean"

Outcome kinds:
  task_status: {T-0025: [omri_queue, closed]}     every listed task is in one of the statuses
  no_task_status: [submitted, accepted]            no task is in any of these statuses
  config_match: {file: limits, regex: '(?m)^concurrency:\\s*2\\b'}
  event: {name: system_change, note_regex: 'concurrency -> 2'}   such an event exists
  inbox_or_task: 'printed strength'                 the phrase is in queues/omri.md or a task title
  remote_branch: trusted                            origin has that branch in the workload repo
  no_dirty_worktrees: true                          `scr sweep --dry-run` finds nothing
  watcher_fresh_minutes: 40                         the watcher log was written recently
  all: [check, check]                               every sub-check passes
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .assets import asset_path
from .events import EventLog
from .gitops import Git
from .run import scr_cmd
from .store import Store

UI_DIR = asset_path("ui")


def load(root: Path) -> list[dict]:
    p = root / "ui" / "chores.yaml"
    if not p.exists():
        p = UI_DIR / "chores.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or []) if p.exists() else []


def check(root: Path, spec) -> bool:
    if not spec:
        return False
    if isinstance(spec, list):
        return all(check(root, s) for s in spec)
    if not isinstance(spec, dict) or len(spec) != 1:
        raise ValueError(f"bad done check: {spec!r}")
    kind, arg = next(iter(spec.items()))
    store = Store(root)
    if kind == "all":
        return all(check(root, s) for s in arg)
    if kind == "task_status":
        tasks = store.load_all()
        return all(tid in tasks and tasks[tid].status in set(sts) for tid, sts in arg.items())
    if kind == "no_task_status":
        return not any(t.status in set(arg) for t in store.load_all().values())
    if kind == "config_match":
        p = root / "config" / f"{arg['file']}.yaml"
        return p.exists() and re.search(arg["regex"], p.read_text(encoding="utf-8")) is not None
    if kind == "event":
        rx = re.compile(arg.get("note_regex", ""))
        for e in EventLog(root / "events" / "events.jsonl").read():
            if e.get("kind") == "system" and e.get("name") == arg["name"] and rx.search(e.get("note") or ""):
                return True
        return False
    if kind == "inbox_or_task":
        q = root / "queues" / "omri.md"
        if q.exists() and arg.lower() in q.read_text(encoding="utf-8").lower():
            return True
        return any(arg.lower() in (t.rec.get("title") or "").lower() for t in store.load_all().values())
    if kind == "remote_branch":
        from .ops import workload
        wl = workload(root, store)
        r = Git(wl.repo).run("ls-remote", "--heads", "origin", arg)
        return r.ok and bool(r.out.strip())
    if kind == "no_dirty_worktrees":
        from .ops import sweep, workload
        if not Path(workload(root).repo).is_dir():     # a repo it cannot see is not a clean one
            return False
        return not sweep(root, dry_run=True)
    if kind == "watcher_fresh_minutes":
        p = root / "reports" / "steward_watch.log"
        if not p.exists():
            return False
        age = (datetime.now(timezone.utc) - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)).total_seconds() / 60
        return age < float(arg)
    raise ValueError(f"unknown done check kind {kind}")


def status(root: Path, acked: set[str]) -> list[dict]:
    """Chores still open (not done, not acked), with their card fields."""
    out = []
    for c in load(root):
        if f"chore:{c['id']}" in acked:
            continue
        try:
            done = check(root, c.get("done"))
        except Exception as e:  # noqa: BLE001 — a broken check shows up as an open card with the error
            done, c = False, {**c, "why": f"{c.get('why', '')}\n(check error: {e})"}
        if done:
            continue
        out.append({"id": c["id"], "title": c["title"], "why": (c.get("why") or "").strip(), "manual": bool(c.get("manual")),
                    "link": c.get("link"), "runnable": bool(c.get("steps"))})
    return out


def run(root: Path, cid: str, actor: str = "omri") -> dict:
    c = next((x for x in load(root) if x["id"] == cid), None)
    if not c or not c.get("steps"):
        return {"ok": False, "output": "no such runnable chore", "done": False}
    log = EventLog(root / "events" / "events.jsonl")
    log.append("system", name="chore", actor=actor, note=f"{cid}: started")
    lines: list[str] = []
    ok = True
    for step in c["steps"]:
        if isinstance(step, dict) and "py" in step:
            mod, _, fn = step["py"].partition(":")
            import importlib
            f = getattr(importlib.import_module(f"scr.{mod}"), fn)
            try:
                res = f(root, **(step.get("kwargs") or {}))
                lines.append(f"$ {step['py']}")
                lines += [str(x) for x in (res if isinstance(res, list) else [res])]
            except Exception as e:  # noqa: BLE001
                lines.append(f"$ {step['py']}\n  FAILED: {e}"); ok = False; break
        else:
            args = [str(a) for a in step]
            rc, out = scr_cmd(root, actor, *args)
            lines.append(f"$ scr {' '.join(args)}\n{out}".rstrip())
            if rc != 0:
                ok = False; break
    try:
        done = check(root, c.get("done"))
    except Exception as e:  # noqa: BLE001
        done = False; lines.append(f"(done check error: {e})")
    log.append("system", name="chore", actor=actor, note=f"{cid}: {'done' if done else 'not done'} (steps {'ok' if ok else 'failed'})")
    return {"ok": ok, "done": done, "output": "\n".join(lines)[-8000:]}
