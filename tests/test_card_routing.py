"""2026-10-14 card routing: only decisions reach Omri (docs/proposals/card-routing.md)."""
from __future__ import annotations

import json
from pathlib import Path

from scr.store import Store
from test_journal_ops import world, scr  # noqa: F401


def test_parked_task_is_not_readied_by_the_cascade(world):
    root, _, _ = world
    scr(root, "--quiet", "task", "new", "Dependent", "--id", "T-0002", "--workload", "quantum-insertion", "--files", "QuantumInsertion/B.lean", "--origin", "omri")
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0002", "triaged")
    scr(root, "--quiet", "edge", "add", "blocks", "T-0001", "T-0002")
    scr(root, "--quiet", "--actor", "omri", "task", "set", "T-0002", "blocked", "--note", "parked: superseded, do not auto-dispatch")
    assert Store(root).get("T-0002").rec.get("parked") is True
    for st in ("dispatched", "in_progress", "submitted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    scr(root, "--quiet", "--actor", "omri", "task", "set", "T-0001", "accepted", "--force-accept", "--note", "test")
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", "merged", "--force")
    assert Store(root).get("T-0002").status == "blocked"            # the cascade left it alone
    scr(root, "--quiet", "--actor", "omri", "task", "set", "T-0002", "triaged", "--force")
    scr(root, "--quiet", "--actor", "omri", "task", "set", "T-0002", "ready")
    assert Store(root).get("T-0002").status == "ready" and "parked" not in Store(root).get("T-0002").rec


def test_config_only_edit_opens_no_hotfix(world):
    from scr import hotfix
    root, _, _ = world
    assert hotfix.journal_note(root, "abc1234", ["config/limits.yaml"], "omri") is None
    assert hotfix.current(root) is None
    tid = hotfix.journal_note(root, "abc1235", ["config/limits.yaml", "scr/watch.py"], "omri")
    assert tid and hotfix.current(root) == tid


def test_job_card_only_when_nobody_else_acts(world):
    from scr import jobs
    from scr.watch import Watcher
    root, _, _ = world
    for st in ("dispatched", "in_progress"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    w = Watcher(root, quiet=True)
    killed = {"id": "J-0001", "task": "T-0001", "name": "build", "status": "failed", "note": "killed by steward:T-0001"}
    assert not w.job_needs_omri(killed)
    (root / ".scr" / "jobs").mkdir(parents=True, exist_ok=True)
    for j in ({"id": "J-0002", "task": "T-0001", "name": "build", "status": "failed", "rc": 1},
              {"id": "J-0003", "task": "T-0001", "name": "build", "status": "running"}):
        (root / ".scr" / "jobs" / f"{j['id']}.json").write_text(json.dumps(j), encoding="utf-8")
    superseded = json.loads((root / ".scr" / "jobs" / "J-0002.json").read_text(encoding="utf-8"))
    assert not w.job_needs_omri(superseded)                      # J-0003 (same task + name) came after it
    lone = {"id": "J-0004", "task": "T-0001", "name": "suite", "status": "failed", "rc": 1}
    assert not w.job_needs_omri(lone)                            # the task is in_progress: its scriber gets it
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", "returned")
    assert w.job_needs_omri(lone)                                # nobody is running it: Omri's
    assert w.job_needs_omri({"id": "J-0005", "name": "x", "status": "failed"})


def test_quiet_events_and_one_card_per_task(world):
    from scr import ui
    root, _, _ = world
    for st in ("dispatched", "in_progress"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    scr(root, "--quiet", "--actor", "s", "event", "system", "--name", "scope_drift", "--task", "T-0001", "--note", "x.lean outside scope")
    scr(root, "--quiet", "--actor", "s", "event", "system", "--name", "leak", "--task", "T-0001", "--note", "leak")
    cards = [c for c in ui.needs_cards(root) if c.get("id") == "T-0001"]
    assert [c.get("name") for c in cards] == ["leak"], cards        # scope_drift / budget_wall never; a leak while no status card
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", "escalated", "--force")
    cards = [c for c in ui.needs_cards(root) if c.get("id") == "T-0001"]
    assert len(cards) == 1 and cards[0]["type"] == "gate", cards           # the status card carries it


def test_additions_only_gate1_is_accepted_by_the_watcher(world):
    """Rule 2: a Steward escalation reading 'Gate 1 ... N ADDED / 0 MODIFIED / 0 REMOVED ... all mechanical checks pass'
    is applied as accepted (accept_override by the watcher); any other escalation stays escalated."""
    from scr.watch import Watcher
    from scr.review import additions_only_gate1
    root, _, _ = world
    for st in ("dispatched", "in_progress", "submitted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    d = Store(root).get("T-0001").dir
    rev = (root / "templates" / "review.md").read_text(encoding="utf-8").replace(
        "accepted | returned (feedback below) | escalated (Gate N, reason)",
        "escalated (Gate 1, statement_diff non-empty: 12 ADDED / 0 MODIFIED / 0 REMOVED; all mechanical checks pass; recommend force-accept)")
    assert additions_only_gate1(rev)
    (d / "review.md").write_text(rev, encoding="utf-8")
    Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "accepted"
    other = (root / "templates" / "review.md").read_text(encoding="utf-8").replace(
        "accepted | returned (feedback below) | escalated (Gate N, reason)", "escalated (Gate 1, 3 CHANGED statements need approval)")
    assert not additions_only_gate1(other)


def test_additions_only_gate1_rejects_negated_prose():
    """T-0367 review: a Decision line explaining why the diff is NOT additions-only, or that checks do not pass, never qualifies."""
    from scr.review import additions_only_gate1
    tpl = (Path(__file__).resolve().parent.parent / "templates" / "review.md").read_text(encoding="utf-8")
    mk = lambda line: tpl.replace("accepted | returned (feedback below) | escalated (Gate N, reason)", line)
    assert additions_only_gate1(mk("escalated (Gate 1: 2 ADDED, 0 MODIFIED, 0 REMOVED; mechanical checks pass)"))
    assert additions_only_gate1(mk("escalated (Gate 1: 4 Gate-1-approved decls, PROOF-ONLY vs d256396; recommend --force-accept and land)"))
    assert not additions_only_gate1(mk("escalated (Gate 1: NOT additions-only - 1 MODIFIED; all mechanical checks pass)"))
    assert not additions_only_gate1(mk("escalated (Gate 1: additions only; not all checks pass)"))
    assert not additions_only_gate1(mk("escalated (Gate 1: additions only; check_done FAIL)"))
    assert not additions_only_gate1(mk("escalated (Gate 2: additions only; checks pass)"))
