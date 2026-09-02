"""Allowed status transitions and the record changes that ride along with them."""
from __future__ import annotations

from .events import now_iso

TRANSITIONS: dict[str, set[str]] = {
    "proposed":   {"triaged", "rejected"},
    "triaged":    {"ready", "blocked", "rejected"},
    "blocked":    {"ready", "escalated", "rejected"},
    "ready":      {"dispatched", "blocked", "rejected"},
    "dispatched": {"in_progress", "ready"},              # ready = dispatch_failed, redispatch
    "in_progress": {"checkpoint", "submitted", "returned", "escalated"},
    "checkpoint": {"in_progress", "returned", "escalated"},
    "submitted":  {"accepted", "returned", "escalated"},
    "returned":   {"ready", "escalated", "rejected"},
    "escalated":  {"omri_queue"},
    "omri_queue": {"accepted", "rejected", "triaged", "closed"},
    "accepted":   {"merged", "returned"},                # returned = rebase/rebuild failed
    "merged":     {"closed"},
    "rejected":   {"closed"},
    "closed":     set(),
}

# Omri-queue items (decide/approve/system) have a shorter life.
OMRI_TRANSITIONS: dict[str, set[str]] = {
    "proposed":  {"triaged", "rejected"},
    "triaged":   {"omri_queue", "rejected"},
    "omri_queue": {"closed", "rejected", "triaged"},
    "rejected":  {"closed"},
    "closed":    set(),
}


def allowed(rec: dict, new: str) -> bool:
    table = OMRI_TRANSITIONS if rec.get("queue") == "omri" else TRANSITIONS
    return new in table.get(rec.get("status", "proposed"), set())


def apply(rec: dict, new: str) -> None:
    """Mutate the record for a transition (beyond status)."""
    old = rec.get("status")
    rec["status"] = new
    rec["updated"] = now_iso()
    if new == "ready":
        rec["ready_since"] = now_iso()
    if new == "dispatched":
        rec["attempts"] = int(rec.get("attempts", 0)) + 1   # attempts = dispatches
    if new in {"merged", "rejected", "closed"}:
        rec.pop("ready_since", None)
