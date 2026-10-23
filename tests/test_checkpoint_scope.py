"""T-0079: a checkpoint commit never carries a file the task is not allowed to touch. The in-scope part of a
worktree goes into the `[checkpoint]` commit, anything else into a `[checkpoint-drift]` commit of its own
(nothing may exist only in a worktree), ONE scope_drift event names it, and neither `land` merges a branch
that still carries it. T-0021 shipped four missions/SCRIPTORIUM/ files through PR #49 this way."""
from __future__ import annotations

from pathlib import Path

from scr.events import EventLog
from scr.gitops import Git
from scr.ops import checkpoint, checkpoint_commit, land, scope_drift, spawn
from scr.store import Store
from test_journal_ops import REPO, accepted_review, git, scr, world  # noqa: F401  (scriptorium + workload repo + origin)

DRIFT_FILE = "missions/SCRIPTORIUM/scripts/check_done.py"       # what T-0021 copied into its worktree


def _running(world):
    """T-0001 (scope.files = QuantumInsertion/A.lean) dispatched, spawned and in_progress; returns (root, repo, wt)."""
    root, repo, _ = world
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched")
    spawn(root, ["T-0001"], lake=False)
    scr(root, "--actor", "s", "task", "set", "T-0001", "in_progress")
    return root, repo, repo / ".worktrees" / "T-0001"


def _drift_events(root: Path) -> list[dict]:
    return [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("name") == "scope_drift"]


def _files_of(wt: Path, sha: str) -> list[str]:
    return sorted(l for l in git(wt, "show", "--name-only", "--format=", sha).splitlines() if l.strip())


def _write(wt: Path, rel: str, text: str = "x\n") -> None:
    p = wt / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _set_scope(root: Path, tid: str, files: list[str] | None = None, mission: str | None = None) -> None:
    t = Store(root).get(tid)
    sc = t.rec.setdefault("scope", {})
    if files is not None:
        sc["files"] = files
    if mission is not None:
        sc["mission"] = mission
    t.save()


# ---- the git wrapper --------------------------------------------------------------------------------
def test_dirty_reports_a_modified_tracked_file_on_the_first_line_exactly(world):
    """Git.run strips its output, so ` M QuantumInsertion/A.lean` as the FIRST porcelain line used to come
    back as `uantumInsertion/A.lean` (T-0082's report, item 4); scoped staging needs the exact path."""
    root, repo, wt = _running(world)
    (wt / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")
    assert Git(wt).dirty() == ["QuantumInsertion/A.lean"]
    _write(wt, "QuantumInsertion/B.lean", "-- new\n")
    assert Git(wt).dirty() == ["QuantumInsertion/A.lean", "QuantumInsertion/B.lean"]


# ---- the rule ---------------------------------------------------------------------------------------
def test_scope_drift_rule_files_mission_log_paths_ignored_and_undeclared(world):
    root, repo, wt = _running(world)
    t = Store(root).get("T-0001")
    paths = ["QuantumInsertion/A.lean", "QuantumInsertion/A.lean.bak", DRIFT_FILE, "missions/LOOPY/runs/r.log", ".loopy/state", "missions/QI26/notes.md"]
    assert scope_drift(paths, t) == ["QuantumInsertion/A.lean.bak", DRIFT_FILE, "missions/QI26/notes.md"]
    t.rec["scope"]["mission"] = "missions/QI26/"                                # the record names a mission (T-0082's field)
    assert scope_drift(paths, t) == ["QuantumInsertion/A.lean.bak", DRIFT_FILE]
    t.rec["scope"]["files"] = ["QuantumInsertion/"]                            # a directory prefix covers a new file under it
    assert scope_drift(paths + ["QuantumInsertionX/y.lean"], t) == [DRIFT_FILE, "QuantumInsertionX/y.lean"]
    # ignored by the workload's git: not drift (with a repo to ask); a declared-nothing task never drifts
    (wt / ".gitignore").write_text("/scratch/\n", encoding="utf-8")
    assert scope_drift(["scratch/big.bin", DRIFT_FILE], t, repo=wt) == [DRIFT_FILE]
    t.rec["scope"] = {"files": [], "mission": None}
    assert scope_drift(paths, t) == []


# ---- the checkpoint ---------------------------------------------------------------------------------
def test_checkpoint_keeps_an_out_of_scope_file_out_of_the_commit_and_raises_one_drift_event(world):
    """DoD: an untracked file outside scope is planted, `checkpoint_commit` runs; the checkpoint commit does
    not contain it and one scope_drift event names it. It is NOT left uncommitted either: crash safety puts
    it in a `[checkpoint-drift]` commit of its own on the same branch."""
    root, repo, wt = _running(world)
    (wt / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")   # in scope
    _write(wt, DRIFT_FILE, "print('checker')\n")                                                            # out of scope
    tip = checkpoint_commit(root, "T-0001")
    log = git(wt, "log", "--format=%s", "master..HEAD").splitlines()
    assert len(log) == 2 and log[0].startswith("[checkpoint-drift] T-0001") and log[1].startswith("[checkpoint] T-0001"), log
    assert tip == git(wt, "rev-parse", "--short", "HEAD")
    assert _files_of(wt, "HEAD~1") == ["QuantumInsertion/A.lean"]               # (a) the checkpoint does not contain it
    assert _files_of(wt, "HEAD") == [DRIFT_FILE]
    assert DRIFT_FILE in git(wt, "log", "-1", "--format=%B") and "refuses task/T-0001" in git(wt, "log", "-1", "--format=%B")
    assert Git(wt).dirty() == []                                                 # nothing exists only in the worktree
    evs = _drift_events(root)                                                    # (b) one scope_drift event names it
    assert len(evs) == 1 and evs[0]["task"] == "T-0001" and evs[0]["note"].startswith(DRIFT_FILE), evs
    assert evs[0]["data"]["files"] == [DRIFT_FILE] and evs[0]["data"]["commit"] == tip
    ck = [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("name") == "checkpoint_commit"]
    assert len(ck) == 1 and ck[0]["note"] == git(wt, "rev-parse", "--short", "HEAD~1")
    # a clean tree checkpoints nothing and raises nothing twice
    assert checkpoint_commit(root, "T-0001") is None and len(_drift_events(root)) == 1


def test_checkpoint_commits_in_scope_modification_new_file_mission_and_log_paths(world):
    """DoD: an in-scope modification and a new in-scope file are still committed — with the mission folder
    and the run-log path — in ONE [checkpoint] commit, no drift commit, no event. Noise (.lake) and
    gitignored paths stay out as before."""
    root, repo, wt = _running(world)
    _set_scope(root, "T-0001", files=["QuantumInsertion/"], mission="QI26")
    git(repo, "checkout", "-q", "master")
    (repo / ".gitignore").write_text("/scratch/\n", encoding="utf-8")
    git(repo, "add", ".gitignore"); git(repo, "commit", "-q", "-m", "ignore scratch"); git(repo, "push", "-q", "origin", "master")
    git(wt, "merge", "-q", "master")                                             # the worktree sees the ignore rule
    (wt / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")   # modified, in scope
    _write(wt, "QuantumInsertion/Audit/Common.lean", "-- new, in scope\n")
    _write(wt, "missions/QI26/notes.md", "plan\n")
    _write(wt, "missions/LOOPY/runs/r1.log", "run\n")
    _write(wt, "scratch/big.bin", "ignored\n")
    _write(wt, ".lake/build/x.olean", "noise\n")
    c = checkpoint(root, "T-0001")
    assert c.sha and c.drift_sha is None and c.drift == [] and c.head == c.sha
    assert _files_of(wt, c.sha) == ["QuantumInsertion/A.lean", "QuantumInsertion/Audit/Common.lean", "missions/LOOPY/runs/r1.log", "missions/QI26/notes.md"]
    assert git(wt, "log", "-1", "--format=%s").startswith("[checkpoint] T-0001")
    assert not _drift_events(root)
    assert Git(wt).dirty() == [] and "scratch/big.bin" not in git(wt, "ls-files") and ".lake" not in git(wt, "ls-files")
    assert (wt / "scratch" / "big.bin").exists() and (wt / ".lake" / "build" / "x.olean").exists()


def test_a_stray_git_add_of_an_out_of_scope_path_does_not_ride_along(world):
    """The index is reset before staging: a scriber's own `git add` of an out-of-scope file must not smuggle
    it into the [checkpoint] commit."""
    root, repo, wt = _running(world)
    _write(wt, "tools/helper.py", "print()\n")
    git(wt, "add", "tools/helper.py")
    (wt / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")
    c = checkpoint(root, "T-0001")
    assert _files_of(wt, c.sha) == ["QuantumInsertion/A.lean"] and _files_of(wt, c.drift_sha) == ["tools/helper.py"]
    assert c.drift == ["tools/helper.py"] and Git(wt).dirty() == []


# ---- the watcher ------------------------------------------------------------------------------------
def test_watch_tick_flags_the_checkpointed_drift_once(world):
    """The tick's checkpoint raises the scope_drift event; the drift block sees the same file in the branch's
    commits and does not raise a second one (once per file, as before); `drift:1` is still on the row."""
    from scr.watch import Watcher
    root, repo, wt = _running(world)
    _write(wt, DRIFT_FILE, "print('checker')\n")
    res = Watcher(root, quiet=True).tick()
    row = res["rows"][0]
    assert row["flags"].startswith("ckpt:") and "drift:1" in row["flags"], row
    assert git(wt, "log", "-1", "--format=%s").startswith("[checkpoint-drift] T-0001")
    evs = _drift_events(root)
    assert len(evs) == 1 and evs[0]["note"].startswith(DRIFT_FILE) and "[checkpoint-drift]" in evs[0]["note"], evs
    res2 = Watcher(root, quiet=True).tick()
    assert "drift:1" in res2["rows"][0]["flags"] and len(_drift_events(root)) == 1
    assert f"drift:T-0001:{DRIFT_FILE}" in (root / ".scr" / "watch.state.txt").read_text(encoding="utf-8").split()


# ---- land -------------------------------------------------------------------------------------------
def test_land_refuses_a_branch_carrying_out_of_scope_paths_until_the_scope_is_widened(world):
    """Judgment line, replayed: the T-0021 case stops at the PR step with the files named — not pushed, no
    PR, status untouched. `land`'s own commit is scoped the same way (a dirty out-of-scope file at landing
    time goes into a [checkpoint-drift] commit, then the refusal). Widening scope.files in the record is
    the way through, and then the drift commit lands as the task's own work."""
    root, repo, wt = _running(world)
    _write(wt, DRIFT_FILE, "print('checker')\n")
    (wt / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")
    checkpoint_commit(root, "T-0001")
    t = Store(root).get("T-0001")
    (t.dir / "report.md").write_text("# report\n\n## Result\n\nProved a.\n\n## Statement changes\n\nnone\n", encoding="utf-8")
    scr(root, "--actor", "steward:T-0001", "task", "set", "T-0001", "submitted")
    (t.dir / "review.md").write_text(accepted_review(), encoding="utf-8")
    _write(wt, "missions/SCRIPTORIUM/README.md", "tooling\n")                     # still dirty at landing time
    (wt / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by\n  trivial\n", encoding="utf-8")   # and so is in-scope work
    r = land(root, "T-0001")
    assert not r["ok"] and r["url"] is None, r["out"]
    refused = [l for l in r["out"] if "REFUSED" in l]
    assert len(refused) == 1 and DRIFT_FILE in refused[0] and "missions/SCRIPTORIUM/README.md" in refused[0], r["out"]
    assert "task/T-0001" not in git(repo, "branch", "-r")                       # not pushed
    assert Store(root).get("T-0001").status == "submitted"                     # not accepted
    assert Git(wt).dirty() == [] and git(wt, "log", "-1", "--format=%s").startswith("[checkpoint-drift] T-0001")
    assert _files_of(wt, "HEAD") == ["missions/SCRIPTORIUM/README.md"]
    assert _files_of(wt, "HEAD~1") == ["QuantumInsertion/A.lean"]                # the landing commit itself is clean
    assert git(wt, "log", "-1", "--format=%s", "HEAD~1").startswith("[T-0001] Prove a")
    # dry run says the same, and commits nothing
    _write(wt, "notes.txt", "scratch\n")
    d = land(root, "T-0001", dry_run=True)
    assert not d["ok"] and any("REFUSED" in l and "notes.txt" in l and DRIFT_FILE in l for l in d["out"]), d["out"]
    assert Git(wt).dirty() == ["notes.txt"]
    (wt / "notes.txt").unlink()
    # the Steward decides the task may own the folder: widen the record, and the same branch lands
    _set_scope(root, "T-0001", files=["QuantumInsertion/A.lean", "missions/SCRIPTORIUM/"])
    r2 = land(root, "T-0001")
    assert r2["ok"], r2["out"]
    assert "task/T-0001" in git(repo, "branch", "-r") and Store(root).get("T-0001").status == "accepted"


