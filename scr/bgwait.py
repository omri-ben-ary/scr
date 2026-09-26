"""Per-task background-wait ceiling for headless sessions.

A headless `claude -p` session that ends its turn while background jobs are still running waits for them
only up to Claude Code's ceiling (600 s by default), then kills them and exits: "Background tasks still
running after 600s; terminating." That is how T-0015, T-0051 and T-0226 died mid-LP.

A task that needs longer asks for it in its record:

    budget:
      max_bg_wait_minutes: 240        # the ceiling, in minutes (a positive integer)

and the launcher starts its session with CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS = minutes * 60 000.
No field means Claude Code's default. 0 ("wait forever") is never accepted: the ceiling is always a number.

Anything above AUTH_LIMIT_MINUTES (12 h) needs Omri's authorization first: the value must have been set by
`scr --actor omri task field <id> budget.max_bg_wait_minutes <m>` (the field event's actor is the record).
`scr task field` refuses such a value from any other actor, and the launcher re-checks the event log so a
hand-edited record cannot get past it; an unauthorized task is not launched but parked in Omri's queue.
"""
from __future__ import annotations

ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
FIELD = "budget.max_bg_wait_minutes"
AUTH_LIMIT_MINUTES = 12 * 60


def parse(value) -> int:
    """A valid ceiling in minutes, or ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"{FIELD} must be a positive integer number of minutes, got {value!r}")
    try:
        m = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{FIELD} must be a positive integer number of minutes, got {value!r}") from None
    if m != value and str(m) != str(value).strip():
        raise ValueError(f"{FIELD} must be a whole number of minutes, got {value!r}")
    if m <= 0:
        raise ValueError(f"{FIELD} must be > 0 minutes (0 would mean 'wait forever'), got {value!r}")
    return m


def requested(rec: dict) -> int | None:
    """The task's requested ceiling in minutes, None when it asks for none. ValueError when malformed."""
    v = (rec.get("budget") or {}).get("max_bg_wait_minutes")
    if v is None or v == "":
        return None
    return parse(v)


def needs_omri(minutes: int | None) -> bool:
    return bool(minutes) and minutes > AUTH_LIMIT_MINUTES


def authorized(events: list[dict], tid: str, minutes: int | None) -> bool:
    """<= 12 h (or no request) is always fine. Above that, the LAST field event that set this task's
    ceiling must be Omri's and carry this exact value."""
    if not needs_omri(minutes):
        return True
    last = None
    for ev in events:
        if ev.get("kind") == "field" and ev.get("task") == tid and ev.get("field") == FIELD:
            last = ev
    if last is None or last.get("actor") != "omri":
        return False
    try:
        return parse(last.get("value")) == minutes
    except ValueError:
        return False


def env_for(minutes: int | None) -> dict[str, str]:
    return {ENV: str(minutes * 60_000)} if minutes else {}


def describe(minutes: int | None) -> str:
    if not minutes:
        return "default (600 s)"
    return f"{minutes} min" + (" (authorized by Omri)" if needs_omri(minutes) else "")
