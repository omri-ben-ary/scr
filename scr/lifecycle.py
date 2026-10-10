"""Allowed status transitions and the record changes that ride along with them."""
from __future__ import annotations

from .events import now_iso

TRANSITIONS: dict[str, set[str]] = {
    "proposed":   {"triaged", "rejected"},
    "triaged":    {"ready", "blocked", "rejected"},
    "blocked":    {"ready", "escalated", "rejected"},
    "ready":      {"dispatched", "blocked", "rejected"},
    "dispatched": {"in_progress", "ready"},              # ready = dispatch_failed, redispatch
    "in_progress": {"checkpoint", "submitted", "returned", "escalated", "waiting"},
    "checkpoint": {"in_progress", "returned", "escalated", "waiting"},
    # waiting = the session ended its turn with `WAITING: job J-xxxx` (scr/jobs.py): the job runs detached under
    # the watcher, the task holds NO concurrency slot, and the watcher resumes the session when the job ends
    "waiting":    {"in_progress", "returned", "escalated"},
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


def cascade(store, log, tid: str, status: str, actor: str = "scr") -> list[str]:
    """After `tid` reaches merged / closed / rejected: dependents whose blockers are now all done become
    `ready`; a rejected blocker raises `blocker_rejected`. One place for this, called by every path that
    finishes a task — `scr task set` AND the watcher's auto-land (which bypassed it until 2026-09-16 and
    left T-0033 blocked on a merged T-0030). Returns the ids made ready."""
    if status not in {"merged", "closed", "rejected"}:
        return []
    from .graph import Graph
    g = Graph.build(store.load_all())
    made: list[str] = []
    for d in g.succ(g.blocks).get(tid, []):
        dt = g.tasks[d]
        if dt.status in {"triaged", "blocked"} and g.ready_now(d) and dt.rec.get("queue") == "scribers":
            was = dt.status
            apply(dt.rec, "ready")
            dt.save()
            log.append("state", task=d, **{"from": was}, to="ready", actor=actor, note=f"unblocked by {tid}")
            made.append(d)
        if status == "rejected" and dt.is_open:
            log.append("system", name="blocker_rejected", actor=actor, data={"task": d, "blocker": tid}, note="Gate 2: rewrite, reparent, or drop")
    return made


def requeue(store, log, tid: str, note: str, actor: str, refund: bool = False) -> bool:
    """A running task whose session is gone goes back to `ready` through the sanctioned path
    (in_progress / checkpoint -> returned -> ready; a task already `returned` takes the second step only), one
    state event per step (T-0187). `refund` gives back the attempt its launch counted (attempts = sessions started, T-0127),
    with a `field` event: a session that died of an exhausted usage-credit balance never ran, so it is not an
    attempt. False when the task is in no status this applies to. Callers refresh STATE.json afterwards."""
    t = store.get(tid)
    steps = {"in_progress": ["returned", "ready"], "checkpoint": ["returned", "ready"], "returned": ["ready"]}.get(t.status)
    if not steps:
        return False
    for new in steps:
        old = t.status
        apply(t.rec, new)
        t.save()
        log.append("state", task=tid, **{"from": old}, to=new, actor=actor, note=note)
    if refund and int(t.rec.get("attempts", 0)) > 0:
        t.rec["attempts"] = int(t.rec["attempts"]) - 1
        t.save()
        log.append("field", task=tid, field="attempts", value=t.rec["attempts"], actor=actor, note=f"refunded: {note}"[:300])
    return True


def apply(rec: dict, new: str) -> None:
    """Mutate the record for a transition (beyond status)."""
    old = rec.get("status")
    rec["status"] = new
    rec["updated"] = now_iso()
    if new == "ready":
        rec["ready_since"] = now_iso()
    if new == "in_progress" and old == "dispatched":
        # attempts = scriber sessions started (T-0127): charged when the launcher opens the session (it makes this
        # transition right before it starts claude), never at `scr dispatch`, so a dispatch that never launched and
        # went back to ready (dispatch_timeout, T-0122) costs nothing. checkpoint -> in_progress is a heartbeat.
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
    if new in {"merged", "rejected", "closed"}:
        rec.pop("ready_since", None)
