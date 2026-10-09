"""Prioritization score. Weights live in config/weights.yaml; graph terms are computed."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .graph import Graph

DEFAULT_WEIGHTS = {
    "w_unlock": 2.0, "w_path": 1.0, "w_breadth": 0.5, "w_verify": 1.5,
    "w_omri": 1.0, "w_age": 0.2, "w_risk": 1.5, "w_retry": 1.0, "w_cost": 0.3,
}


def _days_since(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 86400)


def score_task(g: Graph, tid: str, weights: dict, lps: dict[str, float] | None = None, lins: dict[str, float] | None = None) -> dict:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    t = g.tasks[tid]
    pr = t.rec.get("priority") or {}
    budget = t.rec.get("budget") or {}
    terms = {
        "unlock_value": g.unlock_value(tid),
        "critical_path_share": g.critical_path_share(tid, lps, lins),
        "blocking_breadth": float(g.blocking_breadth(tid)),
        "verifiability": float(pr.get("verifiability", 0.5)),
        "omri_weight": float(pr.get("omri_weight", 0)),
        "days_in_ready": _days_since(t.rec.get("ready_since") or t.rec.get("created")),
        "risk": float(pr.get("risk", 0.0)),
        "attempts": float(t.rec.get("attempts", 0)),
        "expected_mtokens": float(budget.get("max_tokens", 1_000_000)) / 1e6 / max(1, int(budget.get("max_attempts", 1))),
    }
    contrib = {
        "unlock": w["w_unlock"] * math.log1p(terms["unlock_value"]),
        "path": w["w_path"] * terms["critical_path_share"],
        "breadth": w["w_breadth"] * terms["blocking_breadth"],
        "verify": w["w_verify"] * terms["verifiability"],
        "omri": w["w_omri"] * terms["omri_weight"],
        "age": w["w_age"] * terms["days_in_ready"],
        "risk": -w["w_risk"] * terms["risk"],
        "retry": -w["w_retry"] * terms["attempts"],
        "cost": -w["w_cost"] * terms["expected_mtokens"],
    }
    return {"task": tid, "score": round(sum(contrib.values()), 4), "terms": terms, "contrib": contrib}


def rank_ready(g: Graph, weights: dict, queue: str = "scribers") -> list[dict]:
    lps, lins = g.longest_paths(), g.longest_into()
    rows = []
    for t in g.tasks.values():
        if t.rec.get("queue") != queue or t.rec.get("kind") == "milestone":
            continue
        if t.status in {"ready"} or (t.status in {"triaged", "blocked"} and g.ready_now(t.id)):
            rows.append(score_task(g, t.id, weights, lps, lins))
    return sorted(rows, key=lambda r: -r["score"])
