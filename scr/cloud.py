"""Cloud lane: which ready tasks may run in a cloud pool session, and the claim that hands one over.

Design: docs/proposals/cloud-pool.md (Omri, 20–21 Oct). The journal stays single-writer: a claim is an ordinary
`ready -> blocked` transition whose note carries CLOUD_MARK, made here on the PC by the watcher (or by Omri by hand).
The pool session (a cloud container) reads the claims from the scriptorium repo on GitHub, runs the scriber, pushes
`task/<id>` with a terminal `scriber done: <id>` commit and `cloud-reports/<id>` carrying report.md + log.md; the
watcher's cloud_landing_checks (scr/watch.py) then walks the task into review. The 21 Oct double run of T-0495 (cloud
launch on a claim that was never recorded) is what this module prevents: no record, no cloud run.
"""
from __future__ import annotations

from pathlib import Path

CLOUD_MARK = "cloud lane"
LEAN_KINDS = {"lean", "proof", "formalize", "formalization", "formalisation"}


def eligible(rec: dict, lean_ok: bool = False) -> str | None:
    """None when the task may run in the cloud, else the reason it may not.

    lean_ok: the pool can restore the build cache (`.lake/cloud-cache/master` on the PC via the bridge), so Lean tasks
    are allowed except the kernel-replay certificate families, which exceed the container's 7 GB."""
    scope = rec.get("scope") or {}
    files = [str(f) for f in (scope.get("files") or [])]
    if rec.get("queue") not in (None, "scribers"):
        return "not a scribers task"
    if rec.get("workload") not in (None, "quantum-insertion"):
        return f"workload {rec.get('workload')} is PC-only"
    if str(rec.get("title") or "").startswith("hotfix:"):
        return "hotfix"
    if (rec.get("lane") or "auto") == "pc":
        return "lane: pc"
    if int(rec.get("attempts") or 0) > 0:
        return "attempt 2+ runs on the PC lane (its worktree is attached there; 21 Oct: a re-claim re-landed T-0457's stale branch)"
    if not files:
        return "no scope files (housekeeping / PC-local work)"
    if "housekeeping" in str(rec.get("title") or "").lower():
        return "housekeeping task is PC-local"
    heavy = [f for f in files if f.startswith(("QuantumInsertion/FourQuery/", "numerics/certificates", "numerics/qi3/"))]
    if heavy:
        return f"heavy certificate paths: {', '.join(heavy[:2])}"
    lean = [f for f in files if f.endswith(".lean") or f.startswith("QuantumInsertion/")]
    if lean or str(rec.get("kind") or "").lower() in LEAN_KINDS or scope.get("may_touch_statements"):
        if not lean_ok:
            return "Lean task (cloud build cache not enabled)"
    if rec.get("needs") and any(str(n).lower() in {"lp", "local-solver", "local", "gpu", "pc"} for n in rec["needs"]):
        return f"needs {rec['needs']}"
    return None


def claim_note(who: str, ts: str) -> str:
    return f"{CLOUD_MARK}: claimed by {who} at {ts}; a cloud pool session runs it and pushes task/<id>"


def claimed_ids(events: list[dict], tasks: dict) -> list[str]:
    """Tasks currently handed to the cloud: `blocked` with the newest `-> blocked` note carrying CLOUD_MARK."""
    last: dict[str, str] = {}
    for e in events:
        if e.get("kind") == "state" and e.get("to") == "blocked" and e.get("task"):
            last[e["task"]] = str(e.get("note") or "")
    return [tid for tid, t in tasks.items() if t.status == "blocked"
            and (t.rec.get("lane") == "cloud" or CLOUD_MARK in last.get(tid, "").lower())]


def cloud_limits(store) -> tuple[int, bool]:
    lim = store.config("limits") or {}
    return int(lim.get("cloud_pools", 0) or 0), bool(int(lim.get("cloud_lean", 0) or 0))


def pick(store, graph, events: list[dict], weights: dict) -> tuple[list[str], list[tuple[str, str]]]:
    """Ready tasks to hand over now, in rank order, up to the free cloud slots; plus (id, reason) for the skipped."""
    from .score import rank_ready
    cap, lean_ok = cloud_limits(store)
    tasks = graph.tasks
    busy = claimed_ids(events, tasks)
    free = cap - len(busy)
    chosen: list[str] = []
    skipped: list[tuple[str, str]] = []
    if free <= 0:
        return chosen, skipped
    for row in rank_ready(graph, weights, "scribers"):
        tid = row["task"]
        t = tasks[tid]
        if t.status != "ready":
            continue
        why = eligible(t.rec, lean_ok)
        if why:
            skipped.append((tid, why)); continue
        if any(tuple(sorted((tid, r))) in graph.conflicts for r in busy + chosen):
            skipped.append((tid, "conflicts with a running cloud task")); continue
        chosen.append(tid)
        if len(chosen) >= free:
            break
    return chosen, skipped
