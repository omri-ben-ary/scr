"""Cloud lane (docs/proposals/cloud-pool.md; Omri 2026-10-21 "automate the landing part of the cloud tasks"):
a task handed to a cloud pool session is `blocked` with a note containing "cloud lane"; the pool pushes
`task/<id>` to origin with a terminal `scriber done: <id>` commit and writes the task's report.md. The watcher
then fetches the branch, attaches .worktrees/<id> and walks blocked -> ready -> dispatched -> in_progress ->
submitted, so the Steward reviews it like any local branch. On the first night (20 Oct) Omri did these steps by
hand for six tasks and the loop mis-stepped on three of them."""
from __future__ import annotations

from pathlib import Path

from scr.store import Store
from scr.watch import Watcher
from test_journal_ops import REPO, git, scr, world  # noqa: F401

TID = "T-0001"


def _report(root: Path, text: str | None = None) -> None:
    rep = Store(root).get(TID).dir / "report.md"
    rep.write_text(text if text is not None else
                   "# Report\n\n## Result\nJOINT.md written; verdict open; all done_when lines pass.\n\n"
                   "## What I am unsure about\nnothing\n\n## Statement changes\nNone\n\n## Tokens and attempts\n1\n\n"
                   "## unlocks\n```yaml\non_success: []\non_failure: []\ninforms: []\n```\n", encoding="utf-8")


def _cloud_branch(tmp_path: Path, origin: Path, subject: str = f"scriber done: {TID} — JOINT.md") -> str:
    """What the pool does: a clone of origin, a task/<id> branch with one scope commit, pushed."""
    pool = tmp_path / "pool"
    git(tmp_path, "clone", "-q", str(origin), str(pool))
    git(pool, "checkout", "-q", "-b", f"task/{TID}")
    (pool / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")
    git(pool, "add", "-A"); git(pool, "commit", "-q", "-m", subject)
    git(pool, "push", "-q", "origin", f"task/{TID}")
    return git(pool, "rev-parse", "--short", "HEAD")


def _hand_to_cloud(root: Path, note: str = "cloud lane pool-1: running in the cloud") -> None:
    scr(root, "--actor", "omri", "task", "set", TID, "blocked", "--note", note)


def test_cloud_task_is_the_blocked_one_with_the_cloud_lane_note(world):
    root, _, _ = world
    def cloud():                                                          # a tick reads the events once; so does a fresh Watcher
        w = Watcher(root, quiet=True)
        return [t.id for t in w.cloud_tasks(w.store.load_all())]
    assert cloud() == []
    scr(root, "--actor", "omri", "task", "set", TID, "blocked", "--note", "waits on T-0002")
    assert cloud() == []                                                  # an ordinary block is not the cloud lane
    scr(root, "--actor", "omri", "task", "set", TID, "ready")
    _hand_to_cloud(root)
    assert cloud() == [TID]


def test_nothing_happens_until_the_branch_is_pushed_and_done(world, tmp_path):
    root, repo, origin = world
    _hand_to_cloud(root); _report(root)
    w = Watcher(root, quiet=True)
    assert w.cloud_landing_checks(None, w.store.load_all()) == 0          # no branch on origin yet
    assert Store(root).get(TID).status == "blocked"
    _cloud_branch(tmp_path, origin, subject="wip: half a proof")          # pushed, but not a terminal commit
    w = Watcher(root, quiet=True)
    assert w.cloud_landing_checks(None, w.store.load_all()) == 0
    assert Store(root).get(TID).status == "blocked"
    assert not (repo / ".worktrees" / TID).exists()


def test_done_branch_without_a_report_waits(world, tmp_path):
    root, repo, origin = world
    _hand_to_cloud(root)
    sha = _cloud_branch(tmp_path, origin)
    w = Watcher(root, quiet=True)
    assert w.cloud_landing_checks(None, w.store.load_all()) == 0          # report.md is still the template
    assert Store(root).get(TID).status == "blocked"
    assert not (repo / ".worktrees" / TID).exists()
    _report(root)
    w = Watcher(root, quiet=True)
    assert w.cloud_landing_checks(None, w.store.load_all()) == 1
    t = Store(root).get(TID)
    assert t.status == "submitted"
    wt = repo / ".worktrees" / TID
    assert wt.is_dir() and git(wt, "rev-parse", "--short", "HEAD") == sha
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == f"task/{TID}"
    from scr.events import EventLog
    ev = [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("task") == TID]
    hops = [(e.get("from"), e.get("to")) for e in ev if e.get("kind") == "state"][-4:]
    assert hops == [("blocked", "ready"), ("ready", "dispatched"), ("dispatched", "in_progress"), ("in_progress", "submitted")]
    assert [e for e in ev if e.get("kind") == "system" and e.get("name") == "cloud_landed"]


def test_hand_attached_worktree_is_fast_forwarded_not_recreated(world, tmp_path):
    """Omri attached the worktree himself on the first night; a later fix commit on the branch must reach it."""
    root, repo, origin = world
    _hand_to_cloud(root); _report(root)
    _cloud_branch(tmp_path, origin)
    git(repo, "fetch", "-q", "origin", f"task/{TID}")
    git(repo, "worktree", "add", "-q", "--track", "-b", f"task/{TID}", str(repo / ".worktrees" / TID), f"origin/task/{TID}")
    pool = tmp_path / "pool"
    (pool / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by\n  trivial\n", encoding="utf-8")
    git(pool, "commit", "-q", "-am", f"scriber done: {TID} — attempt 2 fix"); git(pool, "push", "-q", "origin", f"task/{TID}")
    fixed = git(pool, "rev-parse", "--short", "HEAD")
    w = Watcher(root, quiet=True)
    assert w.cloud_landing_checks(None, w.store.load_all()) == 1
    assert Store(root).get(TID).status == "submitted"
    assert git(repo / ".worktrees" / TID, "rev-parse", "--short", "HEAD") == fixed


def test_tick_lands_cloud_branch_into_review(world, tmp_path):
    root, repo, origin = world
    _hand_to_cloud(root); _report(root); _cloud_branch(tmp_path, origin)
    res = Watcher(root, quiet=True).tick()
    assert res["ok"]
    assert Store(root).get(TID).status == "submitted"
    assert (repo / ".worktrees" / TID).is_dir()


# ---- the claim (dispatch half; Omri 21 Oct "start using the cloud in normal flow") ---------------------------------
def _limits(root: Path, **kw) -> None:
    import yaml
    p = root / "config" / "limits.yaml"
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    d.update(kw)
    p.write_text(yaml.safe_dump(d), encoding="utf-8")


def test_claim_hands_a_ready_eligible_task_to_the_cloud_and_dispatch_never_takes_it(world):
    root, repo, _ = world
    _limits(root, cloud_pools=1, cloud_lean=1)
    r = scr(root, "--actor", "steward", "cloud", "claim")
    assert "ready -> blocked" in r.stdout and TID in r.stdout
    t = Store(root).get(TID)
    assert t.status == "blocked" and t.rec.get("lane") == "cloud" and t.rec.get("branch") == f"task/{TID}"
    w = Watcher(root, quiet=True)
    assert [x.id for x in w.cloud_tasks(w.store.load_all())] == [TID]
    r = scr(root, "--actor", "steward", "cloud", "claim")                 # cap 1: nothing more to claim
    assert "nothing to claim" in r.stdout
    r = scr(root, "--actor", "prioritizer", "dispatch", "--dry-run")      # the PC lane never picks a cloud claim
    assert TID not in r.stdout
    assert Store(root).get(TID).status == "blocked"


def test_claim_skips_lean_and_heavy_tasks_when_configured(world):
    root, _, _ = world
    _limits(root, cloud_pools=2, cloud_lean=0)
    r = scr(root, "--actor", "steward", "cloud", "claim", "--dry-run")   # T-0001's scope is QuantumInsertion/A.lean
    assert "would claim: —" in r.stdout and "Lean task" in r.stdout
    _limits(root, cloud_pools=2, cloud_lean=1)
    r = scr(root, "--actor", "steward", "cloud", "claim", "--dry-run")
    assert f"would claim: {TID}" in r.stdout
    from scr.cloud import eligible
    assert eligible({"scope": {"files": ["QuantumInsertion/FourQuery/T8/Grid/A.lean"]}}, lean_ok=True).startswith("heavy")
    assert eligible({"scope": {"files": ["missions/QI30/X9/X.md"]}, "needs": ["lp"]}, lean_ok=False).startswith("needs")
    assert eligible({"scope": {"files": ["missions/QI30/X9/X.md"]}}, lean_ok=False) is None
    assert eligible({"scope": {"files": ["missions/QI30/X9/X.md"]}, "lane": "pc"}, lean_ok=True) == "lane: pc"


def test_cloud_pools_zero_disables_the_lane(world):
    root, _, _ = world
    _limits(root, cloud_pools=0)
    r = scr(root, "--actor", "steward", "cloud", "claim")
    assert "cloud_pools is 0" in r.stdout
    assert Store(root).get(TID).status == "ready"


def test_report_delivered_on_cloud_reports_branch_lands_without_the_bridge(world, tmp_path):
    root, repo, origin = world
    _limits(root, cloud_pools=1, cloud_lean=1)
    scr(root, "--actor", "steward", "cloud", "claim")
    sha = _cloud_branch(tmp_path, origin)
    pool = tmp_path / "pool"
    git(pool, "checkout", "-q", "--orphan", f"cloud-reports/{TID}"); git(pool, "rm", "-rfq", ".")
    (pool / "report.md").write_text("# Report\n\n## Result\nJOINT.md written; verdict open.\n\n## Statement changes\nNone\n\n## unlocks\n```yaml\non_success: []\n```\n", encoding="utf-8")
    (pool / "log.md").write_text("- ran in the cloud\n", encoding="utf-8")
    git(pool, "add", "-A"); git(pool, "commit", "-q", "-m", f"cloud report {TID}"); git(pool, "push", "-q", "origin", f"cloud-reports/{TID}")
    w = Watcher(root, quiet=True)
    assert w.cloud_landing_checks(None, w.store.load_all()) == 1
    t = Store(root).get(TID)
    assert t.status == "submitted" and "lane" not in t.rec
    assert "ran in the cloud" in (t.dir / "log.md").read_text(encoding="utf-8")
    assert "verdict open" in (t.dir / "report.md").read_text(encoding="utf-8")
    assert git(repo / ".worktrees" / TID, "rev-parse", "--short", "HEAD") == sha
