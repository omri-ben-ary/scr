"""Append-only, hash-chained event log and its replay.

Event kinds:
  state   {task, from, to, actor, tokens?, note?}
  edge    {edge: blocks|unlocks|informs|supersedes, from, to, op: add|remove, actor, reason?, weight?}
  field   {task, field, value, actor}          # non-status record change (attempts, tokens_used, priority.*)
  system  {name, actor, note?, data?}          # config change, invariant_violation, dispatch_failed, ...
  checkin {actor: omri, op: open|close, note?}
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .canon import H, ZERO, canon

EVENT_KINDS = {"state", "edge", "field", "system", "checkin"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def event_hash(ev: dict) -> str:
    body = {k: v for k, v in ev.items() if k != "h"}
    return H("event", canon(body))


class _file_lock:
    """An exclusive lock on `path` (created if absent), held for the block: msvcrt on Windows, fcntl elsewhere.
    Waits up to `timeout` s in 50 ms steps, then raises TimeoutError — a stuck holder must never deadlock the
    watcher silently."""

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path, self.timeout, self.fh = path, timeout, None

    def __enter__(self):
        import time
        self.fh = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    self.fh.close()
                    raise TimeoutError(f"events lock busy for {self.timeout:g}s: {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


# events.jsonl -> ((inode, mtime_ns, size), parsed events): read() hands out per-event copies of an unchanged file
_READ: dict[str, tuple[tuple[int, int, int], tuple[dict, ...]]] = {}

class EventLog:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        st = self.path.stat()
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        hit = _READ.get(str(self.path))
        if hit and hit[0] == key:          # unchanged since the last read: one UI request used to parse it 14 times
            return [dict(e) for e in hit[1]]
        out = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"events line {i + 1}: {e}") from e
        _READ[str(self.path)] = (key, tuple(out))
        return [dict(e) for e in out]

    def head(self) -> tuple[str, int]:
        evs = self.read()
        return (evs[-1]["h"] if evs else ZERO, len(evs))

    def append(self, kind: str, **fields: Any) -> dict:
        """One event, under the log's file lock (T-0333, done 2026-10-15): head() and the write are one critical
        section, so two processes appending at once — the watcher and a scriber's `scr job submit` forked the
        chain at seq 7996 (10 Oct) and 10249 (15 Oct) — serialize instead of both continuing from the same prev."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.path.with_name(self.path.name + ".lock")):
            prev, n = self.head()
            ev = {"seq": n + 1, "ts": fields.pop("ts", None) or now_iso(), "kind": kind, **fields, "prev": prev}
            ev = {k: v for k, v in ev.items() if v is not None}
            ev["h"] = event_hash(ev)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(canon(ev) + "\n")
        return ev

    # ---- verification ---------------------------------------------------
    def verify_chain(self) -> list[str]:
        errs = []
        prev = ZERO
        for i, ev in enumerate(self.read(), start=1):
            if ev.get("seq") != i:
                errs.append(f"event {i}: seq is {ev.get('seq')}")
            if ev.get("prev") != prev:
                errs.append(f"event {i}: prev does not match previous hash")
            if ev.get("h") != event_hash(ev):
                errs.append(f"event {i}: hash mismatch (content rewritten)")
            prev = ev.get("h", "")
        return errs

    # ---- replay -----------------------------------------------------------
    def replay(self) -> dict[str, Any]:
        """Fold the log into derived state: statuses, edge sets, field values."""
        status: dict[str, str] = {}
        edges: dict[str, dict[tuple, dict]] = {"blocks": {}, "unlocks": {}, "informs": {}, "supersedes": {}}
        fields: dict[str, dict[str, Any]] = {}
        for ev in self.read():
            k = ev["kind"]
            if k == "state":
                status[ev["task"]] = ev["to"]
            elif k == "edge":
                key = (ev["from"], ev["to"])
                bucket = edges[ev["edge"]]
                if ev["op"] == "add":
                    bucket[key] = {kk: ev[kk] for kk in ("weight", "reason") if kk in ev}
                else:
                    bucket.pop(key, None)
            elif k == "field":
                fields.setdefault(ev["task"], {})[ev["field"]] = ev["value"]
        return {"status": status, "edges": edges, "fields": fields}

    def iter_task(self, tid: str) -> Iterator[dict]:
        for ev in self.read():
            if ev.get("task") == tid or ev.get("from") == tid or ev.get("to") == tid:
                yield ev
