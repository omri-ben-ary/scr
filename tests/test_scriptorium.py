"""End-to-end tests: hashing, chain, replay, invariants, tamper detection, CLI lifecycle."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scr.canon import H, ZERO, canon, merkle_root
from scr.events import EventLog, event_hash
from scr.graph import Graph
from scr.state import State
from scr.store import Store

REPO = Path(__file__).resolve().parents[1]


def scr(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "SCRIPTORIUM_ROOT": str(root), "PYTHONPATH": str(REPO), "SCR_NO_JOURNAL": os.environ.get("SCR_TEST_JOURNAL", "1")}
    return subprocess.run([sys.executable, "-m", "scr", *args], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=check)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "s"
    root.mkdir()
    for d in ("config", "templates", "agents"):
        shutil.copytree(REPO / d, root / d)
    scr(root, "init", "--quiet")
    return root


# ---- canon / hashes -----------------------------------------------------------
def test_canon_is_order_independent():
    assert canon({"b": 1, "a": [1, 2]}) == canon({"a": [1, 2], "b": 1}) == '{"a":[1,2],"b":1}'


def test_canon_rejects_nan():
    with pytest.raises(ValueError):
        canon({"x": float("nan")})


def test_tagged_hash_differs_by_tag():
    assert H("task", "x") != H("graph", "x")


def test_merkle_changes_with_any_leaf():
    a = merkle_root(["1", "2", "3"])
    assert a != merkle_root(["1", "2", "4"]) and a == merkle_root(["1", "2", "3"])
    assert merkle_root([]) == H("node")


# ---- events -------------------------------------------------------------------
def test_chain_detects_rewrite(tmp_path: Path):
    log = EventLog(tmp_path / "e.jsonl")
    log.append("system", name="a", actor="t")
    log.append("system", name="b", actor="t")
    assert log.verify_chain() == []
    lines = log.path.read_text().splitlines()
    ev = json.loads(lines[0])
    ev["name"] = "tampered"
    lines[0] = canon(ev)
    log.path.write_text("\n".join(lines) + "\n")
    errs = log.verify_chain()
    assert any("hash mismatch" in e for e in errs)


def test_chain_detects_deleted_middle_event(tmp_path: Path):
    log = EventLog(tmp_path / "e.jsonl")
    for n in "abc":
        log.append("system", name=n, actor="t")
    lines = log.path.read_text().splitlines()
    del lines[1]
    log.path.write_text("\n".join(lines) + "\n")
    assert log.verify_chain()


def test_event_hash_excludes_h():
    ev = {"seq": 1, "kind": "system", "prev": ZERO}
    assert event_hash({**ev, "h": "junk"}) == event_hash(ev)


# ---- cli lifecycle ------------------------------------------------------------
def seed(root: Path):
    scr(root, "--quiet", "task", "new", "M", "--id", "T-0001", "--kind", "milestone", "--origin", "omri")
    scr(root, "--quiet", "task", "new", "A", "--id", "T-0002", "--decls", "a", "--origin", "omri")
    scr(root, "--quiet", "task", "new", "B", "--id", "T-0003", "--decls", "a", "b", "--origin", "T-0002", "--blocked-by", "T-0002")
    scr(root, "--quiet", "edge", "add", "blocks", "T-0003", "T-0001")
    for t in ("T-0001", "T-0002", "T-0003"):
        scr(root, "--quiet", "task", "set", t, "triaged")
    scr(root, "--quiet", "task", "set", "T-0002", "ready")
    scr(root, "--quiet", "task", "set", "T-0003", "blocked")


def test_verify_clean_after_seed(repo: Path):
    seed(repo)
    r = scr(repo, "verify")
    assert "0 errors" in r.stdout


def test_merge_cascades_to_ready(repo: Path):
    seed(repo)
    for s in ("dispatched", "in_progress", "submitted", "accepted"):
        scr(repo, "--quiet", "task", "set", "T-0002", s)
    scr(repo, "--quiet", "task", "set", "T-0002", "merged", "--tokens", "1000")
    t3 = Store(repo).get("T-0003")
    assert t3.status == "ready"
    assert Store(repo).get("T-0002").rec["attempts"] == 1
    assert Store(repo).get("T-0002").rec["tokens_used"] == 1000
    assert "0 errors" in scr(repo, "verify").stdout


def test_illegal_transition_refused(repo: Path):
    seed(repo)
    r = scr(repo, "task", "set", "T-0002", "merged", check=False)
    assert r.returncode == 2 and "not an allowed transition" in r.stderr


def test_cycle_refused_and_logged(repo: Path):
    seed(repo)
    r = scr(repo, "edge", "add", "blocks", "T-0003", "T-0002", check=False)
    assert r.returncode == 2 and "cycle" in r.stderr
    assert "T-0003" not in (Store(repo).get("T-0002").rec.get("blocked_by") or [])
    evs = EventLog(repo / "events" / "events.jsonl").read()
    assert any(e.get("name") == "invariant_violation" for e in evs)
    assert "0 errors" in scr(repo, "verify").stdout


def test_tamper_task_record_detected(repo: Path):
    seed(repo)
    t = Store(repo).get("T-0003")
    t.rec["status"] = "merged"      # edit the file behind the log's back
    t.save()
    r = scr(repo, "verify", check=False)
    assert r.returncode == 1
    assert "task record changed since snapshot" in r.stdout
    assert "log replays to blocked" in r.stdout


def test_tamper_docs_detected(repo: Path):
    seed(repo)
    (Store(repo).get("T-0003").dir / "spec.md").write_text("changed\n")
    r = scr(repo, "verify", check=False)
    assert r.returncode == 1 and "task docs changed" in r.stdout
    scr(repo, "refresh", "--quiet")           # a refresh re-blesses the docs (they are prose, not state)
    assert "0 errors" in scr(repo, "verify").stdout


def test_tamper_events_detected(repo: Path):
    seed(repo)
    p = repo / "events" / "events.jsonl"
    lines = p.read_text().splitlines()
    ev = json.loads(lines[3]); ev["actor"] = "mallory"; lines[3] = canon(ev)
    p.write_text("\n".join(lines) + "\n")
    r = scr(repo, "verify", check=False)
    assert r.returncode == 1 and "hash mismatch" in r.stdout


def test_hand_added_edge_without_event_detected(repo: Path):
    seed(repo)
    scr(repo, "--quiet", "task", "new", "D", "--id", "T-0004", "--decls", "a", "--origin", "omri")
    t = Store(repo).get("T-0004")
    t.rec["blocked_by"] = ["T-0002"]
    t.save()
    scr(repo, "refresh", "--quiet")           # snapshot now matches the files...
    r = scr(repo, "verify", check=False)      # ...but the log does not
    assert r.returncode == 1 and "in records but not in the log" in r.stdout


def test_hand_made_cycle_is_reported_not_fatal(repo: Path):
    seed(repo)
    t = Store(repo).get("T-0002")
    t.rec["blocked_by"] = ["T-0001"]          # T-0001 -> T-0002 -> T-0003 -> T-0001
    t.save()
    scr(repo, "refresh", "--quiet")           # must not crash
    r = scr(repo, "verify", check=False)
    assert r.returncode == 1 and "has a cycle" in r.stdout


def test_graph_hashes_and_state_root_change_only_when_they_should(repo: Path):
    seed(repo)
    s1 = json.loads((repo / "state" / "STATE.json").read_text())
    scr(repo, "--quiet", "edge", "add", "informs", "T-0002", "T-0003", "--weight", "0.7", "--reason", "r")
    s2 = json.loads((repo / "state" / "STATE.json").read_text())
    assert s2["parent_state_root"] == s1["state_root"]
    assert s1["graph"]["informs"] != s2["graph"]["informs"]
    assert s1["graph"]["blocks"] == s2["graph"]["blocks"]
    assert s1["state_root"] != s2["state_root"]
    assert s1["config_hash"] == s2["config_hash"]


def test_conflicts_and_dispatch(repo: Path):
    seed(repo)
    scr(repo, "--quiet", "task", "new", "C", "--id", "T-0004", "--decls", "a", "--origin", "omri")
    scr(repo, "--quiet", "edge", "add", "blocks", "T-0004", "T-0001")
    scr(repo, "--quiet", "task", "set", "T-0004", "triaged")
    scr(repo, "--quiet", "task", "set", "T-0004", "ready")
    g = Graph.build(Store(repo).load_all())
    assert ("T-0002", "T-0004") in g.conflicts    # both name decl `a`
    r = scr(repo, "dispatch", "--dry-run")
    # exactly one of the two conflicting ready tasks is chosen
    assert r.stdout.count("T-000") == 1
    scr(repo, "--quiet", "--actor", "prioritizer", "dispatch")
    st = {t: Store(repo).get(t).status for t in ("T-0002", "T-0004")}
    assert sorted(st.values()) == ["dispatched", "ready"]
    assert "0 errors" in scr(repo, "verify").stdout


def test_score_terms(repo: Path):
    seed(repo)
    out = yaml.safe_load(scr(repo, "score", "T-0002").stdout)
    assert out["terms"]["blocking_breadth"] == 1      # T-0003 waits only on T-0002
    assert out["terms"]["unlock_value"] > 0
    assert 0 < out["terms"]["critical_path_share"] <= 1


def test_impact_analysis(repo: Path):
    seed(repo)
    out = yaml.safe_load(scr(repo, "graph", "impact", "T-0002", "--decls", "a").stdout)
    assert out["stale_descendants"] == ["T-0003"]
    out = yaml.safe_load(scr(repo, "graph", "impact", "T-0002", "--decls", "zzz").stdout)
    assert out["stale_descendants"] == []


def test_snapshot_diff(repo: Path):
    seed(repo)
    st = State(repo)
    a = st.read_snapshot()
    scr(repo, "--quiet", "task", "field", "T-0003", "priority.risk", "0.9")
    b = st.read_snapshot()
    d = st.diff(a, b)
    assert d["tasks_changed"] == ["T-0003"] and d["graph_changed"] == [] and not d["config_changed"]


# ---- the scriptorium as its own workload (T-0061) ---------------------------------------
def git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout.strip()


def self_world(tmp_path: Path) -> tuple[Path, Path]:
    """A scriptorium checkout that is a git repo with a bare origin and is registered as its own workload
    (`repo: .`, venv: link, push: true), sitting on a side branch one journal commit ahead of origin/main,
    with a fake .venv to link — the Desktop layout of 2026-09-19."""
    root = tmp_path / "scriptorium"
    root.mkdir()
    for d in ("config", "templates", "agents"):
        shutil.copytree(REPO / d, root / d)
    scr(root, "init", "--quiet")
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text(encoding="utf-8"))
    wl["scriptorium"] = {"repo": ".", "main_branch": "main", "worktrees": "../scriptorium-worktrees", "venv": "link", "push": True}
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl), encoding="utf-8")
    (root / ".gitignore").write_text(".venv/\n.scr/\n__pycache__/\n", encoding="utf-8")
    (root / ".venv" / "Scripts").mkdir(parents=True)
    (root / ".venv" / "Scripts" / "marker.txt").write_text("root venv\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main"); git(root, "add", "-A"); git(root, "commit", "-q", "-m", "seed")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(root, "remote", "add", "origin", str(origin)); git(root, "push", "-q", "-u", "origin", "main")
    git(root, "checkout", "-q", "-b", "worktree-retirement")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "journal.md").write_text("a journal commit origin/main does not have\n", encoding="utf-8")
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "[journal] side branch work")
    return root, origin


def test_scr_under_test_is_this_checkout():
    """pytest must exercise THIS tree's scr, not the checkout's: a spawned worktree links the checkout's
    .venv (editable install of the checkout), and `python -m pytest` run there still imports the worktree's
    package because the cwd precedes the editable finder. Fails if someone runs a bare pytest.exe."""
    import scr
    assert Path(scr.__file__).resolve().parent == REPO / "scr"


def test_ensure_main_folds_side_branch_and_pushes(tmp_path: Path):
    from scr.admin import ensure_main
    from scr.gitops import Git
    root, origin = self_world(tmp_path)
    side = git(root, "rev-parse", "HEAD")
    assert git(origin, "rev-parse", "main") != side
    out = ensure_main(root, Git(root), push=True)
    assert any("fast-forwarded" in l for l in out) and any(l.startswith("pushed main") for l in out), out
    assert git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(root, "rev-parse", "main") == git(origin, "rev-parse", "main") == side
    assert ensure_main(root, Git(root)) == []                                  # already on main: nothing to fold
    assert ensure_main(root, Git(root), push=True) == [f"pushed main to origin ({git(root, 'rev-parse', '--short', 'main')})"]


def test_ensure_main_cli_folds_and_pushes(tmp_path: Path):
    root, origin = self_world(tmp_path)
    r = scr(root, "--actor", "omri", "ensure-main", "--push")
    assert "fast-forwarded" in r.stdout and "pushed main" in r.stdout, r.stdout
    assert git(root, "rev-parse", "main") == git(origin, "rev-parse", "main") == git(root, "rev-parse", "worktree-retirement")


def test_spawn_self_workload_from_checkout_head_with_linked_venv(tmp_path: Path):
    from scr.ops import spawn
    root, origin = self_world(tmp_path)
    scr(root, "--quiet", "task", "new", "Tooling", "--id", "T-0007", "--workload", "scriptorium", "--files", "scr/ops.py", "--origin", "omri")
    for st in ("triaged", "ready", "dispatched"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0007", st)
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "[journal] the task record")   # the record is a journal commit too
    head = git(root, "rev-parse", "HEAD")
    out = spawn(root, ["T-0007"])
    wt = tmp_path / "scriptorium-worktrees" / "T-0007"
    assert wt.is_dir(), out
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "task/T-0007"
    assert git(wt, "rev-parse", "HEAD") == head                                 # the checkout's HEAD ...
    assert git(wt, "rev-parse", "HEAD") != git(origin, "rev-parse", "main")     # ... which origin/main does not have
    assert any("based on checkout HEAD" in l for l in out), out
    assert (wt / "docs" / "journal.md").exists() and (wt / "tasks" / "T-0007-tooling" / "task.yaml").exists()
    assert (wt / ".venv" / "Scripts" / "marker.txt").read_text(encoding="utf-8") == "root venv\n"   # linked, not copied
    assert any(".venv -> checkout .venv" in l for l in out), out
    assert git(wt, "status", "--porcelain") == ""                               # the link is ignored, not staged
    out2 = spawn(root, ["T-0007"])                                              # idempotent: reuse, keep the link
    assert any("reusing" in l for l in out2) and not any("failed" in l for l in out2), out2


def test_watcher_journal_push_comes_from_config(tmp_path: Path):
    from scr.watch import Watcher
    root, origin = self_world(tmp_path)
    scr(root, "--actor", "omri", "ensure-main", "--push")
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "derived views"); git(root, "push", "-q", "origin", "main")
    w = Watcher(root, quiet=True)
    assert w.push_journal and not w.push                                       # journal push on by config; checkpoint push stays a flag
    (root / "docs" / "note.md").write_text("a scriber wrote docs\n", encoding="utf-8")
    env = dict(os.environ); env.pop("SCR_NO_JOURNAL", None)
    old = os.environ.pop("SCR_NO_JOURNAL", None)
    try:
        res = w.tick()
    finally:
        if old is not None:
            os.environ["SCR_NO_JOURNAL"] = old
    assert res["ok"], res
    assert git(root, "status", "--porcelain") == ""                             # journaled ...
    assert git(root, "rev-parse", "main") == git(origin, "rev-parse", "main")   # ... and pushed
    assert git(origin, "show", "main:docs/note.md") == "a scriber wrote docs"
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text(encoding="utf-8"))
    wl["scriptorium"]["push"] = False
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl), encoding="utf-8")
    assert not Watcher(root, quiet=True).push_journal and Watcher(root, quiet=True, push=True).push_journal


def test_ui_page_has_no_inbox_form_or_done_list():
    """T-0086: the left panel no longer carries the 'Add an idea to the Inbox' form or the 'Done' list.
    Reads the file scr.ui serves (UI_DIR/index.html) so a moved UI_DIR would still be checked."""
    from scr.ui import UI_DIR
    page = (UI_DIR / "index.html").read_text(encoding="utf-8")
    for needle in ("inboxForm", "inboxText", 'id="done"', "doneN", "renderDone", "Add an idea to the Inbox", "details.done"):
        assert needle not in page, needle
    assert 'id="needs"' in page  # the rest of the panel is untouched
    assert "planTasks" in page  # the add-task button stays
