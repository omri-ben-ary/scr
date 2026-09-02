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
        snap = {
            "schema": "scriptorium/state/v1",
            "ts": now_iso(),
            "git_head_at_snapshot": git_head(self.root),
            "parent_state_root": prev["state_root"] if prev else ZERO,
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
