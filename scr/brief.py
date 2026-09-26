"""The standard scriber launch prompt for a task (shared by `scr task brief` and the launcher)."""
from __future__ import annotations

from pathlib import Path

from . import bgwait
from .store import Store, Task

BRIEF_TEMPLATE = """Use the scriber agent. This is Scriptorium task {id}: {title}
Your brief is {spec} — read it in full; its Scope and Definition of done are binding, and anything
outside Scope is read-only. Work only inside this worktree ({worktree}); never commit, never switch
branch (the Steward checkpoints your worktree into git every tick). Keep an honest run log in {log}
(append-only; put any question for the Steward on a line starting `HELP:` and keep working on what does
not depend on it). When done, write {report} using the template already there — fill "Statement changes"
truthfully (any entry routes to Gate 1) and the `unlocks` block. Budget: {attempts} attempt(s),
{tokens:,} tokens, {hours} h wall.
This headless session exits once your turn ends and background jobs have run {bgwait} past it; anything
still running is then killed. For long computations, stay in the foreground and poll, or record in {log}
that the task needs a longer `budget.max_bg_wait_minutes` (over 12 h needs Omri).{stmt}"""


def brief_for(root: Path, store: Store, t: Task) -> str:
    d = t.dir
    wl = (store.config("workloads") or {}).get(t.rec.get("workload") or "", {})
    wt = t.rec.get("worktree") or f"{wl.get('worktrees', '../worktrees')}/{t.id}"
    wt_abs = (root / wt).resolve()
    b = t.rec.get("budget") or {}
    stmt = ("\n\nThis task may need NEW or CHANGED statements (may_touch_statements: true). Freeze them in the "
            "mission's TARGETS.md and HALT there once for Omri's approval (Gate 1) before proving anything."
            if (t.rec.get("scope") or {}).get("may_touch_statements") else "")
    try:
        bg = bgwait.describe(bgwait.requested(t.rec))
    except ValueError:
        bg = "default (600 s)"
    bg = "10 min (the default)" if bg.startswith("default") else bg
    return BRIEF_TEMPLATE.format(
        id=t.id, title=t.rec.get("title", ""), spec=(d / "spec.md").resolve(), worktree=wt_abs,
        log=(d / "log.md").resolve(), report=(d / "report.md").resolve(),
        attempts=b.get("max_attempts", 3), tokens=int(b.get("max_tokens", 0)), hours=b.get("max_wall_hours", 6), bgwait=bg, stmt=stmt)
