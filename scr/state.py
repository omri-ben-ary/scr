"""State root, snapshots, verification. See docs/hashing.md."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .canon import H, ZERO, canon, merkle_root
from .events import EventLog, now_iso
from .graph import Graph
from .store import Store, STATES


# T-0207: the numeric config/limits.yaml keys -> (reader's conversion, the default its reader falls back to).
# Every code reader swallows a bad value into its inline default (or crashes on int("abc") later), so verify
# names it instead. `int` keys also refuse a non-integral float (int(0.5) is a silent 0); None as the default
# means an agent reads the key through its brief and has no fallback. statement_window is not numeric (T-0135).
LIMITS_NUMERIC: dict[str, tuple[type, Any]] = {
    "concurrency": (int, 3),                    # scr/cli.py:259, scr/queue.py:14, scr/watch.py:741, scr/ui.py:316
    "wip_ready_scribers": (int, None),          # the Intake agent (scr/launch.py INTAKE_BRIEF, agents/intake.md)
    "max_successors_per_task": (int, None),     # the Intake agent (agents/intake.md)
    "max_depth": (int, None),                   # the Intake agent (scr/launch.py INTAKE_BRIEF, agents/intake.md)
    "default_max_attempts": (int, 3),           # scr/cli.py:68
    "omri_stale_hours": (float, None),          # the Concierge agent (agents/concierge.md)
    "dispatch_timeout_minutes": (float, 30),    # scr/watch.py:881 (DISPATCH_TIMEOUT_MINUTES)
    "checkpoint_every_minutes": (int, 30),      # scr/cli.py:442, scr/watch.py:361
    "land_test_timeout_seconds": (int, 3600),   # scr/ops.py:484 land_test_timeout (LAND_TEST_TIMEOUT_S)
    "review_stale_hours": (float, 2.0),         # scr/watch.py:917 (REVIEW_STALE_HOURS)
    "help_stale_hours": (float, 3.0),           # scr/watch.py:918 (HELP_STALE_HOURS)
    "credits_pause_minutes": (float, 60),       # scr/watch.py:785,793 (CREDITS_PAUSE_MINUTES)
}


def limits_warnings(limits: dict) -> list[str]:
    """One line per LIMITS_NUMERIC key present in `limits` whose value is not a positive number (bools and
    strings count as not numbers). Absent keys are not reported."""
    out = []
    for key, (kind, default) in LIMITS_NUMERIC.items():
        if key not in limits:
            continue
        v = limits[key]
        ok = (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
              and (kind is float or float(v).is_integer()))
        if not ok:
            fallback = default if default is not None else "nothing (an agent reads the raw value)"
            out.append(f"limits.yaml {key}: {v!r} is not a positive number; readers fall back to {fallback}")
    return out


def git_head(root: Path) -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


class State:
    def __init__(self, root: Path):
        self.root = root
        self.store = Store(root)
        self.log = EventLog(root / "events" / "events.jsonl")
        self.snapshot_path = root / "state" / "STATE.json"
        self.graph_path = root / "graph" / "graph.json"

    # ---- computation ------------------------------------------------------
    def compute(self) -> dict[str, Any]:
        tasks = self.store.load_all()
        g = Graph.build(tasks)
        leaves = {tid: {"task_hash": t.task_hash(), "docs_hash": t.docs_hash(), "leaf": t.leaf()} for tid, t in sorted(tasks.items())}
        tasks_root = merkle_root([v["leaf"] for v in leaves.values()])
        gh = g.hashes()
        head, count = self.log.head()
        config_hash = self.store.config_hash()
        state_root = H("state", tasks_root, gh["graph_root"], head, str(count), config_hash)
        return {
            "state_root": state_root,
            "tasks_root": tasks_root,
            "graph": gh,
            "events_head": head,
            "events_count": count,
            "config_hash": config_hash,
            "tasks": leaves,
            "_graph": g,
        }

    def read_snapshot(self) -> dict | None:
        if not self.snapshot_path.exists():
            return None
        return json.loads(self.snapshot_path.read_text(encoding="utf-8"))

    def snapshot(self, note: str | None = None) -> dict:
        c = self.compute()
        prev = self.read_snapshot()
        g: Graph = c.pop("_graph")
        if prev and prev.get("state_root") == c["state_root"]:
            parent = prev.get("parent_state_root", ZERO)   # nothing changed: keep the chain link
            if not note and self.graph_path.exists():
                # Idempotent: the UI and the watcher refresh on every tick; rewriting an unchanged
                # snapshot only bumps `ts`, which left state/STATE.json dirty after every commit
                # (seen 2026-09-15). The snapshot describes the state, and the state did not change.
                return prev
        else:
            parent = prev["state_root"] if prev else ZERO
        snap = {
            "schema": "scriptorium/state/v1",
            "ts": now_iso(),
            "git_head_at_snapshot": git_head(self.root),
            "parent_state_root": parent,
            **c,
        }
        if note:
            snap["note"] = note
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_path.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.graph_path.write_text(json.dumps(g.to_json(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return snap

    # ---- verification -----------------------------------------------------
    def verify(self) -> dict[str, list[str]]:
        errors, warnings = [], []
        c = self.compute()
        g: Graph = c.pop("_graph")

        snap = self.read_snapshot()
        if snap is None:
            errors.append("no state/STATE.json — run `scr snapshot`")
        else:
            for k in ("state_root", "tasks_root", "events_head", "events_count", "config_hash"):
                if snap.get(k) != c[k]:
                    errors.append(f"snapshot {k} is stale: {str(snap.get(k))[:12]} != {str(c[k])[:12]}")
            if snap.get("graph", {}).get("graph_root") != c["graph"]["graph_root"]:
                errors.append("snapshot graph_root is stale")
            for tid, v in c["tasks"].items():
                sv = snap.get("tasks", {}).get(tid)
                if sv is None:
                    errors.append(f"{tid}: not in snapshot")
                elif sv["task_hash"] != v["task_hash"]:
                    errors.append(f"{tid}: task record changed since snapshot")
                elif sv["docs_hash"] != v["docs_hash"]:
                    errors.append(f"{tid}: task docs changed since snapshot")
            for tid in snap.get("tasks", {}):
                if tid not in c["tasks"]:
                    errors.append(f"{tid}: in snapshot but missing from tasks/")

        errors += self.log.verify_chain()

        # replay: records must be a faithful cache of the log
        rp = self.log.replay()
        for tid, t in g.tasks.items():
            derived = rp["status"].get(tid)
            if derived is None:
                errors.append(f"{tid}: no state events (status {t.status} is not derivable)")
            elif derived != t.status:
                errors.append(f"{tid}: status {t.status} but log replays to {derived}")
            if t.status not in STATES:
                errors.append(f"{tid}: unknown status {t.status}")
        for name in ("blocks", "unlocks", "informs", "supersedes"):
            rec_edges = set(g.informs) if name == "informs" else set(getattr(g, name))
            log_edges = set(rp["edges"][name])
            for e in sorted(rec_edges - log_edges):
                errors.append(f"{name} edge {e[0]} -> {e[1]} is in records but not in the log")
            for e in sorted(log_edges - rec_edges):
                errors.append(f"{name} edge {e[0]} -> {e[1]} is in the log but not in records")
        for tid in rp["status"]:
            if tid not in g.tasks:
                errors.append(f"{tid}: has events but no record")

        ie, iw = g.check_invariants()
        errors += ie
        warnings += iw

        # T-0157: ops.workload() resolves a missing or unregistered `workload:` to the first entry on
        # purpose (a record may name a workload before it is registered); make that fallback visible.
        wls = list(self.store.config("workloads") or {})
        if wls:
            for tid, t in sorted(g.tasks.items()):
                key = t.rec.get("workload")
                if t.is_open and (not key or key not in wls):
                    warnings.append(f"{tid}: workload '{key or ''}' is not registered in config/workloads.yaml "
                                    f"(resolves to the first entry, '{wls[0]}')")

        limits = self.store.config("limits")
        warnings += limits_warnings(limits if isinstance(limits, dict) else {})

        if self.graph_path.exists():
            try:
                gj = json.loads(self.graph_path.read_text(encoding="utf-8"))
                if gj.get("hashes", {}).get("graph_root") != c["graph"]["graph_root"]:
                    errors.append("graph/graph.json is stale")
            except json.JSONDecodeError:
                errors.append("graph/graph.json is not valid JSON")
        else:
            warnings.append("graph/graph.json missing — run `scr snapshot`")
        return {"errors": errors, "warnings": warnings, "state_root": c["state_root"]}

    def diff(self, a: dict, b: dict) -> dict:
        """Compare two snapshots by their recorded hashes only."""
        ta, tb = a.get("tasks", {}), b.get("tasks", {})
        return {
            "state_root": [a.get("state_root"), b.get("state_root")],
            "tasks_added": sorted(set(tb) - set(ta)),
            "tasks_removed": sorted(set(ta) - set(tb)),
            "tasks_changed": sorted(t for t in set(ta) & set(tb) if ta[t]["task_hash"] != tb[t]["task_hash"]),
            "docs_changed": sorted(t for t in set(ta) & set(tb) if ta[t]["docs_hash"] != tb[t]["docs_hash"]),
            "graph_changed": [n for n in ("blocks", "unlocks", "informs", "supersedes", "conflicts")
                              if a.get("graph", {}).get(n) != b.get("graph", {}).get(n)],
            "config_changed": a.get("config_hash") != b.get("config_hash"),
            "events": [a.get("events_count"), b.get("events_count")],
        }
