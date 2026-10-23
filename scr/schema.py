"""The state schema version and the migrations between versions.

state/STATE.json carries `schema_version` (absent = 1, the format before versions existed). A release that changes
the state format adds a Migration here; `scr migrate` applies the pending ones in order, each as one journaled commit
with a `schema_migrated` system event. Releases expand before they contract: release N reads what N-1 wrote, so a
session started on the old install stays valid. A migration that breaks that (`compatible_with_running_sessions`
False) is refused while a headless session is still running; `scr upgrade` relies on the same check.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .events import EventLog
from .run import pid_alive, process_created

SCHEMA_VERSION = 1          # the version this code writes and reads


@dataclass(frozen=True)
class Migration:
    frm: int
    to: int
    title: str
    apply: Callable[[Path], list[str]]          # changes the state root; returns lines for the log
    compatible_with_running_sessions: bool = True


# Ordered. Version 1 is the first versioned format: nothing to migrate yet.
MIGRATIONS: list[Migration] = []


def current_version(root: Path) -> int:
    p = root / "state" / "STATE.json"
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get("schema_version", 1))
    except (OSError, ValueError, TypeError):
        return 1


def pending(root: Path, migrations: list[Migration] | None = None, target: int | None = None) -> list[Migration]:
    """The chain of migrations from the state's version up to `target` (default: the newest any migration reaches,
    at least SCHEMA_VERSION). Raises when the chain has a gap: a state the code cannot bring forward."""
    migs = MIGRATIONS if migrations is None else migrations
    v = current_version(root)
    target = target if target is not None else max([SCHEMA_VERSION, *(m.to for m in migs)])
    out: list[Migration] = []
    while v < target:
        step = next((m for m in migs if m.frm == v), None)
        if step is None:
            raise RuntimeError(f"no migration from schema version {v} (target {target})")
        out.append(step)
        v = step.to
    return out


def running_sessions(root: Path) -> list[dict]:
    """Headless sessions whose recorded pid is still the same live process (the `launch` events; T-0147's
    created check guards against a reused pid). One entry per session id, newest launch wins."""
    seen: dict[str, dict] = {}
    for e in EventLog(root / "events" / "events.jsonl").read():
        if e.get("kind") == "system" and e.get("name") == "launch":
            d = e.get("data") or {}
            key = d.get("session_id") or f"pid:{d.get('pid')}"
            seen[key] = {"task": e.get("task"), **d}
    out = []
    for s in seen.values():
        pid = s.get("pid")
        if pid and pid_alive(pid) and (s.get("created") is None or process_created(pid) == s.get("created")):
            out.append(s)
    return out


def migrate_schema(root: Path, actor: str, migrations: list[Migration] | None = None, dry_run: bool = False,
                   sessions: Callable[[Path], list[dict]] = running_sessions, commit: Callable[[Path, str, str], None] | None = None) -> list[str]:
    """Apply the pending migrations in order. Each one: apply, `schema_migrated` event, snapshot at the new version,
    one journal commit. An incompatible migration is refused while sessions run (nothing after it is applied)."""
    from .cli import refresh
    from .journal import journal
    steps = pending(root, migrations)
    if not steps:
        return [f"schema version {current_version(root)}: nothing to migrate"]
    if dry_run:
        return [f"would migrate {m.frm} -> {m.to}: {m.title}" + ("" if m.compatible_with_running_sessions else " (needs no running sessions)")
                for m in steps]
    out: list[str] = []
    log = EventLog(root / "events" / "events.jsonl")
    for m in steps:
        if not m.compatible_with_running_sessions:
            live = sessions(root)
            if live:
                names = ", ".join(str(s.get("task") or s.get("kind")) for s in live)
                raise RuntimeError(f"migration {m.frm} -> {m.to} ({m.title}) changes the state format under running sessions "
                                   f"({names}); run it when they have ended")
        lines = m.apply(root) or []
        log.append("system", name="schema_migrated", actor=actor, note=f"{m.frm} -> {m.to}: {m.title}",
                   data={"from": m.frm, "to": m.to})
        refresh(root, quiet=True, note=f"schema {m.frm} -> {m.to}", schema_version=m.to)
        (commit or (lambda r, a, msg: journal(r, a, msg, force_refresh=False)))(root, actor, f"migrate schema {m.frm} -> {m.to}")
        out += [f"migrated {m.frm} -> {m.to}: {m.title}", *[f"  {ln}" for ln in lines]]
    return out
