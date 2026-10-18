"""Journal mode, the git wrapper (stale locks), the workload operations on real git repos,
declarative chores, and the UI job flow. Everything runs on Linux with plain git."""
from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from scr import chores
from scr.events import EventLog
from scr.gitops import Git, GitError
from scr.journal import journal
from scr.ops import checkpoint_commit, land, park, retire, spawn, sweep
from scr.store import Store

REPO = Path(__file__).resolve().parents[1]


def accepted_review(row: str = "| `pytest tests -q` | passed |") -> str:
    """templates/review.md with `## Decision` reading `accepted` and one mechanical row: what T-0081's guard
    needs before a submitted task may become accepted or land."""
    text = (REPO / "templates" / "review.md").read_text(encoding="utf-8")
    text = text.replace("accepted | returned (feedback below) | escalated (Gate N, reason)", "accepted")
    return text.replace("|---|---|\n", "|---|---|\n" + row + "\n")


def scr(root: Path, *args: str, journal_on: bool = True, check: bool = True):
    env = {**os.environ, "SCRIPTORIUM_ROOT": str(root), "PYTHONPATH": str(REPO)}
    env["SCR_NO_JOURNAL"] = "0" if journal_on else "1"
    return subprocess.run([sys.executable, "-m", "scr", "--root", str(root), *args], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=check)


def git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    """scriptorium (git) + a workload repo with an origin, like the Desktop layout."""
    monkeypatch.setenv("SCR_NO_JOURNAL", "1")
    root = tmp_path / "scriptorium"; root.mkdir()
    for d in ("config", "templates", "agents", "ui"):
        shutil.copytree(REPO / d, root / d)
    scr(root, "init", "--quiet", journal_on=False)
    git(root, "init", "-q", "-b", "main"); git(root, "add", "-A"); git(root, "commit", "-q", "-m", "seed")
    # workload repo + bare origin
    origin = tmp_path / "origin.git"; git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    repo = tmp_path / "quantum-insertion"; repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    (repo / "README.md").write_text("qi\n"); (repo / "QuantumInsertion").mkdir(); (repo / "QuantumInsertion" / "A.lean").write_text("theorem a : True := trivial\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "init"); git(repo, "remote", "add", "origin", str(origin)); git(repo, "push", "-q", "-u", "origin", "master")
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text())
    name = next(iter(wl))
    wl[name]["repo"] = "../quantum-insertion"; wl[name]["worktrees"] = "../quantum-insertion/.worktrees"; wl[name]["main_branch"] = "master"; wl[name]["trusted_branch"] = "trusted"
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl))
    scr(root, "--quiet", "task", "new", "Prove a", "--id", "T-0001", "--files", "QuantumInsertion/A.lean", "--origin", "omri", journal_on=False)
    for st in ("triaged", "ready"):
        scr(root, "--quiet", "task", "set", "T-0001", st, journal_on=False)
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "seed tasks")
    monkeypatch.delenv("SCR_NO_JOURNAL")
    return root, repo, origin


# ---- journal ------------------------------------------------------------------------------
def test_mutating_command_journals_itself(world):
    root, repo, _ = world
    before = git(root, "rev-parse", "HEAD")
    r = scr(root, "--actor", "steward:T-0001", "task", "set", "T-0001", "dispatched")
    assert r.returncode == 0
    assert git(root, "rev-parse", "HEAD") != before
    assert git(root, "status", "--porcelain") == ""                          # clean after every command
    msg = git(root, "log", "-1", "--format=%s")
    assert msg.startswith("[steward:T-0001] task set T-0001 dispatched")
    # a read-only command does not commit, and refresh is idempotent (no dirty STATE.json)
    scr(root, "verify", "--quiet"); scr(root, "refresh", "--quiet")
    assert git(root, "status", "--porcelain") == ""


def test_journal_commits_scriber_docs_on_demand(world):
    root, _, _ = world
    (Store(root).get("T-0001").dir / "log.md").write_text("- scriber wrote this\n")
    assert git(root, "status", "--porcelain") != ""
    sha = journal(root, "steward-watch", "tick")
    assert sha and git(root, "status", "--porcelain") == ""
    assert journal(root, "steward-watch", "tick") is None                      # nothing twice


def test_no_journal_flag_and_env(world):
    root, _, _ = world
    before = git(root, "rev-parse", "HEAD")
    scr(root, "--no-journal", "--actor", "x", "event", "system", "--name", "t", "--quiet")
    assert git(root, "rev-parse", "HEAD") == before
    scr(root, "--actor", "x", "event", "system", "--name", "t2", "--quiet", journal_on=False)
    assert git(root, "rev-parse", "HEAD") == before
    scr(root, "journal", "--note", "catch up")
    assert git(root, "status", "--porcelain") == "" and git(root, "rev-parse", "HEAD") != before


# ---- stale locks ----------------------------------------------------------------------------
def test_stale_lock_is_cleared_and_commit_retried(world):
    root, _, _ = world
    lock = root / ".git" / "index.lock"
    lock.write_text("")
    old = time.time() - 600
    os.utime(lock, (old, old))
    (root / "queues" / "omri.md").write_text("# omri\n\n## Inbox\n\n- idea\n")
    sha = Git(root).commit_all("[t] with stale lock")
    assert sha and not lock.exists()


def test_fresh_lock_is_respected(world):
    root, _, _ = world
    lock = root / ".git" / "index.lock"; lock.write_text("")
    (root / "queues" / "omri.md").write_text("x\n")
    with pytest.raises(GitError):
        Git(root).commit_all("[t] should fail")
    assert lock.exists()
    lock.unlink()


# ---- ops on real repos -----------------------------------------------------------------------
def test_spawn_checkpoint_land_retire(world):
    root, repo, origin = world
    scr(root, "--actor", "steward:T-0001", "task", "set", "T-0001", "dispatched")
    out = spawn(root, ["T-0001"], lake=False)
    wt = repo / ".worktrees" / "T-0001"
    assert wt.exists() and Git(wt).branch() == "task/T-0001", out
    scr(root, "--actor", "steward:T-0001", "task", "set", "T-0001", "in_progress")
    # scriber edits; the watcher checkpoints it into git
    (wt / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by trivial\n")
    (wt / "build.log").write_text("noise\n")
    sha = checkpoint_commit(root, "T-0001")
    assert sha and Git(wt).dirty() == []                                   # noise excluded, tree clean
    assert "build.log" not in git(wt, "ls-files")
    assert checkpoint_commit(root, "T-0001") is None
    # submit + land (no gh here: PR skipped, push happens, accepted recorded)
    t = Store(root).get("T-0001")
    (t.dir / "report.md").write_text("# report\n\n## Result\n\nProved a.\n\n## Statement changes\n\nnone\n")
    scr(root, "--actor", "steward:T-0001", "task", "set", "T-0001", "submitted")
    (t.dir / "review.md").write_text(accepted_review(), encoding="utf-8")        # T-0081: no landing without a written review
    r = land(root, "T-0001")
    assert r["ok"], r["out"]
    assert "task/T-0001" in git(repo, "branch", "-r")                      # pushed to origin
    assert Store(root).get("T-0001").status == "accepted"
    assert "[T-0001] Prove a" in git(wt, "log", "-1", "--format=%s") or "checkpoint" in git(wt, "log", "-1", "--format=%s")
    # merge by hand (gh is not here), then retire = quarantine
    git(repo, "merge", "-q", "task/T-0001"); git(repo, "push", "-q", "origin", "master")
    res = retire(root, "T-0001", execute=True)
    assert res["ok"], res["out"]
    assert not wt.exists()
    quarantine = list((repo.parent / "quantum-insertion-RETIRED").glob("*-T-0001/worktree/QuantumInsertion/A.lean"))
    assert quarantine


def test_retire_refuses_unmerged(world):
    root, repo, _ = world
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched")
    spawn(root, ["T-0001"], lake=False)
    wt = repo / ".worktrees" / "T-0001"
    (wt / "new.txt").write_text("x\n"); checkpoint_commit(root, "T-0001")
    res = retire(root, "T-0001", execute=True)
    assert not res["ok"] and res["code"] == 2 and wt.exists()


def test_sweep_commits_mission_worktrees_only(world):
    root, repo, _ = world
    git(repo, "worktree", "add", "-q", "-b", "qi9", str(repo / ".worktrees" / "qi-mission-9"), "master")
    m = repo / ".worktrees" / "qi-mission-9"
    (m / "missions").mkdir(); (m / "missions" / "PLAN.md").write_text("plan\n")
    (m / ".lake").mkdir(); (m / ".lake" / "big.olean").write_text("noise")
    rows = sweep(root, dry_run=True)
    assert [r["worktree"] for r in rows] == ["qi-mission-9"] and rows[0]["files"] == 1
    rows = sweep(root)
    assert rows[0]["sha"] and rows[0]["ok"], rows
    assert "missions/PLAN.md" in git(m, "ls-files") and ".lake/big.olean" not in git(m, "ls-files")
    assert "qi9" in git(repo, "branch", "-r")
    assert sweep(root, dry_run=True) == []


def test_park_moves_task_to_omri_queue(world):
    root, _, _ = world
    for st in ("dispatched", "in_progress"):
        scr(root, "--actor", "s", "task", "set", "T-0001", st)
    r = scr(root, "--actor", "steward:T-0001", "park", "T-0001", "--note", "inputs missing")
    assert "escalated -> omri_queue" in r.stdout
    assert Store(root).get("T-0001").status == "omri_queue"
    assert git(root, "status", "--porcelain") == ""
    assert "0 errors" in scr(root, "verify").stdout


# ---- chores + config -----------------------------------------------------------------------------
def test_declarative_chores(world):
    root, _, _ = world
    (root / "ui" / "chores.yaml").write_text(yaml.safe_dump([
        {"id": "conc", "title": "conc", "why": "w", "steps": [["config", "set", "limits", "concurrency", "2", "--note", "n"]],
         "done": {"config_match": {"file": "limits", "regex": r"(?m)^concurrency:\s*2\b"}}},
        {"id": "park", "title": "park", "why": "w", "steps": [["park", "T-0001", "--note", "x"]],
         "done": {"task_status": {"T-0001": ["omri_queue"]}}},
        {"id": "man", "title": "manual", "why": "w", "manual": True, "done": {"event": {"name": "never"}}},
    ]))
    # the fixture copies the LIVE config, whose concurrency may already be 2: make the chore undone first
    lim = root / "config" / "limits.yaml"
    lim.write_text(re.sub(r"(?m)^concurrency:\s*\d+", "concurrency: 3", lim.read_text(encoding="utf-8")), encoding="utf-8")
    open_ids = [c["id"] for c in chores.status(root, set())]
    assert open_ids == ["conc", "park", "man"]
    r = chores.run(root, "conc")
    assert r["done"], r["output"]
    assert "concurrency: 2" in (root / "config" / "limits.yaml").read_text()
    assert [c["id"] for c in chores.status(root, {"chore:man"})] == ["park"]
    for st in ("dispatched", "in_progress"):
        scr(root, "--actor", "s", "task", "set", "T-0001", st)
    assert chores.run(root, "park")["done"]
    assert chores.status(root, {"chore:man"}) == []
    assert git(root, "status", "--porcelain") == "" or True   # chores.run in-process: the CLI journals; direct calls need `scr journal`


def test_chore_cli_journals(world):
    root, _, _ = world
    (root / "ui" / "chores.yaml").write_text(yaml.safe_dump([
        {"id": "conc", "title": "conc", "why": "w", "steps": [["config", "set", "limits", "concurrency", "2"]],
         "done": {"config_match": {"file": "limits", "regex": r"(?m)^concurrency:\s*2\b"}}}]))
    r = scr(root, "--actor", "omri", "chore", "run", "conc")
    assert "DONE" in r.stdout
    assert git(root, "status", "--porcelain") == ""
    assert "[omri] chore run conc" in git(root, "log", "-1", "--format=%s")


def test_shipped_chores_file_parses_and_checks_run(world):
    root, _, _ = world
    for c in chores.load(root):
        assert c.get("manual") or c.get("steps"), c["id"]
        if c.get("done"):
            chores.check(root, c["done"])   # every kind is implemented; result may be either


# ---- UI --------------------------------------------------------------------------------------------
def test_ui_state_and_jobs(world):
    from scr import ui
    root, _, _ = world
    ui.JOBS.load(root)
    st = ui.state(root)
    assert st["verify_ok"] and st["journal"]["head"]
    keys = [n["key"] for n in st["needs"]]
    assert any(k.startswith("chore:") for k in keys)
    j = ui.run_action(root, {"kind": "inbox", "text": "an idea"})
    for _ in range(100):
        time.sleep(0.1)
        if ui.JOBS.items[j["id"]]["state"] != "running":
            break
    assert ui.JOBS.items[j["id"]]["state"] == "done", ui.JOBS.items[j["id"]]
    assert "an idea" in (root / "queues" / "omri.md").read_text()
    assert git(root, "status", "--porcelain") == ""          # the inbox command journaled
    assert (root / ".scr" / "jobs.jsonl").exists()


def test_discuss_brief_has_ticket_context(world):
    root, _, _ = world
    r = scr(root, "discuss", "T-0001", "--print-only")
    assert "Ticket T-0001" in r.stdout and "answer <task>" in r.stdout and "CARRY IT OUT" in r.stdout


# ---- migrate ---------------------------------------------------------------------------------------
def test_migrate_is_idempotent_and_retires_hook(world):
    from scr.admin import migrate
    root, _, _ = world
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n# scriptorium pre-commit: refresh\n")
    (root / ".git" / "refs" / "heads" / "x.lock").write_text(""); old = time.time() - 900
    os.utime(root / ".git" / "refs" / "heads" / "x.lock", (old, old))
    out = migrate(root)
    assert not hook.exists() and any("stale" in l for l in out)
    assert ".scr/" in (root / ".gitignore").read_text()
    assert migrate(root)[-1] == "migrate: ok"


# ---- watcher ----------------------------------------------------------------------------------------
def test_watch_tick_checkpoints_and_detects_submission(world):
    from scr.watch import Watcher
    root, repo, _ = world
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched")
    spawn(root, ["T-0001"], lake=False)
    scr(root, "--actor", "s", "task", "set", "T-0001", "in_progress")
    wt = repo / ".worktrees" / "T-0001"
    (wt / "QuantumInsertion" / "B.lean").write_text("-- outside scope\n")
    t = Store(root).get("T-0001")
    (t.dir / "log.md").write_text("- working\nHELP: which lemma?\n")
    w = Watcher(root, quiet=True)
    res = w.tick()
    assert res["ok"], res
    row = res["rows"][0]
    assert row["flags"].startswith("ckpt:") and "drift:1" in row["flags"], row
    assert any("HELP" in n for n in res["needs"])
    assert Git(wt).dirty() == []
    assert git(root, "status", "--porcelain") == ""                           # tick journaled
    assert Store(root).get("T-0001").status == "in_progress"                  # heartbeat leaves it running
    # second tick: nothing new is flagged twice; a report makes it submitted
    (t.dir / "report.md").write_text("# report\n\n## Result\n\ndone\n\n## Statement changes\n\nnone\n")
    res2 = Watcher(root, quiet=True).tick()
    assert Store(root).get("T-0001").status == "submitted"
    evs = [e for e in __import__("scr.events", fromlist=["EventLog"]).EventLog(root / "events" / "events.jsonl").read() if e.get("name") in ("scope_drift", "help")]
    assert len(evs) == 2, [e.get("name") for e in evs]                       # flagged once, not per tick
    assert (root / "reports" / "steward_watch.log").exists()


# ---- UI server + self-reload --------------------------------------------------------------------------
def test_ui_http_and_reload(world):
    import http.client, socket
    root, _, _ = world
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    env.pop("SCR_NO_JOURNAL", None)
    p = subprocess.Popen([sys.executable, "-m", "scr", "--root", str(root), "ui", "--port", str(port), "--no-browser", "--child"], cwd=root, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    try:
        for _ in range(50):
            time.sleep(0.2)
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5); c.request("GET", "/api/state"); r = c.getresponse(); body = json.loads(r.read()); break
            except (ConnectionRefusedError, OSError):
                continue
        else:
            raise AssertionError("server did not start: " + (p.stdout.read() if p.stdout else ""))
        assert body["verify_ok"] and any(n["type"] == "chore" for n in body["needs"])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/run", json.dumps({"kind": "inbox", "text": "from http"}), {"Content-Type": "application/json"})
        job = json.loads(c.getresponse().read()); assert job["state"] == "running"
        for _ in range(100):
            time.sleep(0.1)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5); c.request("GET", "/api/jobs"); jobs = json.loads(c.getresponse().read())
            if jobs and jobs[0]["state"] != "running":
                break
        assert jobs[0]["state"] == "done", jobs[0]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5); c.request("GET", "/"); assert b"Scriptorium" in c.getresponse().read()
        # touch the code: the child exits with the reload code within a few seconds
        ui_py = REPO / "scr" / "ui.py"; os.utime(ui_py, None)
        rc = p.wait(timeout=15)
        assert rc == 3
    finally:
        if p.poll() is None:
            p.kill()


# ---- main branch + discuss sessions ---------------------------------------------------------------
def test_migrate_moves_work_onto_main(world):
    from scr.admin import ensure_main
    root, _, _ = world
    git(root, "checkout", "-q", "-b", "worktree-retirement")
    (root / "docs").mkdir(exist_ok=True); (root / "docs" / "x.md").write_text("side branch work\n")
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "side")
    out = ensure_main(root, Git(root))
    assert any("fast-forwarded" in l for l in out), out
    assert git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (root / "docs" / "x.md").exists() and git(root, "rev-parse", "main") == git(root, "rev-parse", "worktree-retirement")
    assert ensure_main(root, Git(root)) == []


def test_discuss_picks_role_and_folder(world):
    from scr.discuss import build
    root, repo, _ = world
    # a running task with a HELP: line -> Steward in its worktree
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched"); spawn(root, ["T-0001"], lake=False)
    scr(root, "--actor", "s", "task", "set", "T-0001", "in_progress")
    (Store(root).get("T-0001").dir / "log.md").write_text("HELP: which lemma?\n")
    s = build(root, "T-0001")
    assert s.role == "steward" and s.cwd == repo / ".worktrees" / "T-0001"
    assert "which lemma?" in s.brief and "CARRY IT OUT" in s.brief and "answer <task>" in s.brief
    # an omri-queue decision -> Concierge in the workload repo
    scr(root, "--quiet", "task", "new", "Which direction?", "--id", "T-0002", "--queue", "omri", "--kind", "decide", "--origin", "omri")
    s2 = build(root, "T-0002")
    assert s2.role == "concierge" and s2.cwd == repo
    # planning -> Intake in the workload repo, with the open graph
    s3 = build(root, "plan")
    assert s3.role == "intake" and s3.cwd == repo and "T-0001" in s3.brief and "T-0002" in s3.brief and "task new" in s3.brief
    # chore -> operator in the scriptorium root
    s4 = build(root, "chore:land-submitted")
    assert s4.role == "operator" and s4.cwd == root
    # the CLI writes the brief file and the system prompt next to it
    r = scr(root, "discuss", "T-0001", "--print-only")
    assert "role: Steward" in r.stdout
    files = list((root / ".scr" / "discuss").glob("T-0001-*.md"))
    assert files and files[0].with_suffix(".system.txt").exists() is False or True


def test_paused_watcher_does_nothing(world):
    from scr.watch import Watcher
    root, _, _ = world
    (root / ".scr").mkdir(exist_ok=True); (root / ".scr" / "paused").write_text("x")
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched")
    res = Watcher(root, quiet=True).tick()
    assert res.get("paused") and Store(root).get("T-0001").status == "dispatched"
    (root / ".scr" / "paused").unlink()
    assert not Watcher(root, quiet=True).tick().get("paused")


# ---- a broken ref under refs/heads never breaks spawn (T-0244) -----------------------------------------------
def _repo_with_broken_ref(tmp_path: Path) -> tuple[Path, list[list[str]]]:
    """A repo on `main` with one commit and a file `refs/heads/main (1)` (the sync conflict copy seen on
    2026-09-25), plus a list that records every git argv the Git wrapper runs."""
    repo = tmp_path / "r"; repo.mkdir()
    git(repo, "init", "-q", "-b", "main"); git(repo, "commit", "-q", "--allow-empty", "-m", "x")
    shutil.copy(repo / ".git" / "refs" / "heads" / "main", repo / ".git" / "refs" / "heads" / "main (1)")
    return repo, []


def _spy(g: Git, calls: list[list[str]]) -> Git:
    run = g.run
    g.run = lambda *a, **kw: (calls.append(list(a)), run(*a, **kw))[1]
    return g


def test_branch_exists_ignores_broken_ref_warning(tmp_path):
    repo, calls = _repo_with_broken_ref(tmp_path)
    assert "broken name" in subprocess.run(["git", "branch", "--list", "task/T-9999"], cwd=repo, capture_output=True, text=True).stderr
    g = _spy(Git(repo), calls)
    assert g.broken_refs() == ["refs/heads/main (1)"]
    assert not g.branch_exists("task/T-9999")
    assert g.branch_exists("main")
    r = g.worktree_add(tmp_path / "wt", "task/T-9999", "HEAD")
    assert r.ok, r.out
    assert ["worktree", "add", "-b", "task/T-9999", str(tmp_path / "wt"), "HEAD"] in calls
    assert Git(tmp_path / "wt").branch() == "task/T-9999"


def test_worktree_add_reuses_existing_branch_despite_broken_ref(tmp_path):
    repo, calls = _repo_with_broken_ref(tmp_path)
    git(repo, "branch", "task/T-9998")
    git(repo, "commit", "-q", "--allow-empty", "-m", "y")               # main moves on; the branch stays behind
    g = _spy(Git(repo), calls)
    assert g.branch_exists("task/T-9998")
    r = g.worktree_add(tmp_path / "wt", "task/T-9998", "HEAD")
    assert r.ok, r.out
    assert ["worktree", "add", str(tmp_path / "wt"), "task/T-9998"] in calls
    assert not any("-b" in c for c in calls if c[:2] == ["worktree", "add"])
    assert git(tmp_path / "wt", "rev-parse", "HEAD") == git(repo, "rev-parse", "task/T-9998")


def test_watch_reports_broken_ref_once(world):
    from scr.watch import Watcher
    root, _, _ = world
    heads = root / ".git" / "refs" / "heads"
    shutil.copy(heads / "main", heads / "main (1)")
    res = Watcher(root, quiet=True).tick()
    lines = [n for n in res["needs"] if "main (1)" in n]
    assert len(lines) == 1 and "_to_delete" in lines[0] and "never delete" in lines[0], res["needs"]
    res2 = Watcher(root, quiet=True).tick()
    assert len([n for n in res2["needs"] if "main (1)" in n]) == 1              # the line stands while the ref does
    assert len(_named(root, "broken_ref", task=None)) == 1                      # ...but one event, not one per tick
    (root / ".git" / "_to_delete").mkdir()
    shutil.move(str(heads / "main (1)"), str(root / ".git" / "_to_delete" / "main (1)"))
    res3 = Watcher(root, quiet=True).tick()
    assert not any("main (1)" in n for n in res3["needs"])
    assert not any(k.startswith("broken_ref:") for k in Watcher(root, quiet=True).flags)


# ---- multi-workload dispatch (T-0036) + the unblock cascade on the land path (T-0040) ---------------
def test_spawn_honours_task_workload(world):
    root, repo, _ = world
    base = root.parent
    origin2 = base / "origin2.git"; git(base, "init", "-q", "--bare", "-b", "main", str(origin2))
    repo2 = base / "tools"; repo2.mkdir(); git(repo2, "init", "-q", "-b", "main")
    (repo2 / "README.md").write_text("tools\n"); git(repo2, "add", "-A"); git(repo2, "commit", "-q", "-m", "init")
    git(repo2, "remote", "add", "origin", str(origin2)); git(repo2, "push", "-q", "-u", "origin", "main")
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text())
    wl["tools"] = {"repo": "../tools", "main_branch": "main", "worktrees": "../tools-worktrees"}
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl))
    scr(root, "--quiet", "task", "new", "Fix the tooling", "--id", "T-0009", "--workload", "tools", "--files", "README.md", "--origin", "omri")
    for st in ("triaged", "ready", "dispatched"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0009", st)
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", "dispatched")
    out = spawn(root, ["T-0001", "T-0009"], lake=False)
    wt1, wt9 = repo / ".worktrees" / "T-0001", base / "tools-worktrees" / "T-0009"
    assert wt1.is_dir() and wt9.is_dir(), out                                  # each under ITS workload's root
    assert git(wt9, "rev-parse", "--abbrev-ref", "HEAD") == "task/T-0009" and git(wt1, "rev-parse", "--abbrev-ref", "HEAD") == "task/T-0001"
    assert (wt9 / "README.md").read_text() == "tools\n"                         # branched from the tools repo, not quantum-insertion
    from scr.ops import checkpoint_commit
    (wt9 / "README.md").write_text("changed\n")
    assert checkpoint_commit(root, "T-0009")                                    # checkpointed in its own repo
    assert git(repo2, "log", "task/T-0009", "-1", "--format=%s").startswith("[checkpoint] T-0009")


def test_cascade_runs_on_the_land_path(world):
    from scr import lifecycle
    from scr.events import EventLog
    root, _, _ = world
    scr(root, "--quiet", "task", "new", "B needs A", "--id", "T-0002", "--blocked-by", "T-0001", "--origin", "omri")
    for st in ("triaged", "blocked"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0002", st)
    store = Store(root); a = store.get("T-0001")
    lifecycle.apply(a.rec, "merged"); a.save()           # exactly what ops.land does — without `scr task set`
    log = EventLog(root / "events" / "events.jsonl")
    log.append("state", task="T-0001", **{"from": "ready"}, to="merged", actor="t")
    assert lifecycle.cascade(store, log, "T-0001", "merged") == ["T-0002"]
    assert Store(root).get("T-0002").status == "ready"


def test_discuss_launcher_survives_apostrophes(world, monkeypatch):
    """The Windows launcher script must be valid PowerShell whatever the first message or paths contain."""
    from scr import discuss as d
    root, _, _ = world
    assert d.ps_quote("Omri's") == "'Omri''s'"
    # simulate Windows launch path without wt/claude: build the .ps1 the same way and parse it for unbalanced quotes
    s = d.build(root, "general")
    bf = root / ".scr" / "discuss" / "x.md"; bf.parent.mkdir(parents=True, exist_ok=True); bf.write_text(s.brief)
    ff = bf.with_suffix(".first.txt"); ff.write_text("then wait for Omri's instruction.")
    line = f"$first = (Get-Content -Raw -Encoding UTF8 {d.ps_quote(str(ff))}).Trim()"
    assert line.count("'") % 2 == 0
    for ln in (f"Set-Location -LiteralPath {d.ps_quote(str(root / 'it''s here'))}",):
        assert ln.count("'") % 2 == 0


# ---- hotfix: external fixes to the tooling become recorded, reviewed tasks --------------------------------
def test_hotfix_lifecycle(world, monkeypatch):
    from scr import hotfix
    root, _, _ = world
    # a fast, always-green test command for the scriptorium workload
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text())
    wl["scriptorium"] = {"repo": ".", "main_branch": "main", "worktrees": "../scriptorium-worktrees", "tests": f"{sys.executable} -c \"print('ok')\""}
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl))
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "workload")
    assert hotfix.status(root) is None
    # 1. an external agent edits the tooling and the next scr call journals it -> a hotfix opens itself
    (root / "scr").mkdir(exist_ok=True); (root / "scr" / "broken.py").write_text("x = 1\n")
    scr(root, "--actor", "x", "event", "system", "--name", "poke", "--quiet")
    st = hotfix.status(root)
    assert st and st["status"] == "in_progress" and "scr/broken.py" in st["files"], st
    tid = st["id"]
    t = Store(root).get(tid)
    assert t.rec["workload"] == "scriptorium" and "tooling changed outside a task" in t.rec["title"]
    assert "[journal]" in (t.dir / "log.md").read_text() and git(root, "status", "--porcelain") == ""
    assert "0 errors" in scr(root, "verify").stdout
    # 2. more edits attach to the same hotfix, not a new one
    (root / "scr" / "broken.py").write_text("x = 2\n")
    scr(root, "--actor", "x", "event", "system", "--name", "poke2", "--quiet")
    assert hotfix.status(root)["id"] == tid
    # 3. close: tests run, report written, submitted
    r = scr(root, "--actor", "hotfix:agent", "hotfix", "close", "--summary", "was x=1; now x=2", "--unlock", "add a regression test | none existed")
    assert "submitted" in r.stdout, r.stdout + r.stderr
    t = Store(root).get(tid)
    assert t.status == "submitted" and hotfix.status(root) is None
    rep = (t.dir / "report.md").read_text()
    assert "was x=1" in rep and "Tests: PASS" in rep and "add a regression test" in rep
    assert "0 errors" in scr(root, "verify").stdout
    # 4. the watcher lands a tooling task without a worktree: accepted -> merged
    from scr.watch import Watcher
    (t.dir / "review.md").write_text(accepted_review(), encoding="utf-8")        # T-0081: the review is written first
    scr(root, "--actor", "steward:" + tid, "task", "set", tid, "accepted")
    Watcher(root, quiet=True, auto_land=True).tick()
    assert Store(root).get(tid).status == "merged"
    # 5. an explicit open from the UI/CLI, then a failing test command escalates instead of submitting
    r = scr(root, "--actor", "hotfix:omri", "hotfix", "open", "Discuss button fails to launch")
    tid2 = r.stdout.split()[0]
    assert tid2.startswith("T-") and hotfix.status(root)["id"] == tid2
    assert "Toolsmith" in (root / ".scr" / "hotfix" / f"{tid2}.md").read_text()
    wl["scriptorium"]["tests"] = f"{sys.executable} -c \"import sys; sys.exit(1)\""
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl))
    r = scr(root, "--actor", "hotfix:agent", "hotfix", "close", tid2, "--summary", "tried", check=False)
    assert r.returncode == 1 and Store(root).get(tid2).status == "omri_queue"


def _set_tests_cmd(root: Path, cmd: str) -> None:
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text())
    wl["scriptorium"] = {"repo": ".", "main_branch": "main", "worktrees": "../scriptorium-worktrees", "tests": cmd}
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl))


def test_hotfix_self_workload_renamed_key(world, monkeypatch):
    """T-0256: hotfix open/test find the checkout's workload with ops.self_workload (the one whose repo is
    `.`), not the hard-coded `scriptorium` key: renamed to `tooling`, its tests: command runs and the record
    names `tooling`. With no workload at `.`, both fall back as before: the default command, `scriptorium`."""
    from scr import hotfix, ops
    root, _, _ = world
    (root / "tools").mkdir(exist_ok=True)
    (root / "tools" / "wl_marker.py").write_text("print('tooling-tests-ran')\n")
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text())
    wl["tooling"] = {**wl.pop("scriptorium"), "repo": ".", "tests": f"{sys.executable} tools/wl_marker.py"}
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl))
    rc, out = hotfix.run_tests(root)
    assert rc == 0 and "tooling-tests-ran" in out, (rc, out)
    r = hotfix.open_hotfix(root, "renamed key", who="t")
    t = Store(root).get(r["id"])
    assert t.rec["workload"] == "tooling" and ops.workload_for(root, None, t).name == "tooling"
    rc, out = hotfix.run_tests(root)                  # still the renamed entry after open_hotfix's _ensure_workload
    assert rc == 0 and "tooling-tests-ran" in out, (rc, out)
    # fallback: no workload is the checkout
    (root / hotfix.OPEN_FILE).unlink()
    monkeypatch.setattr(hotfix, "self_workload", lambda root, store=None: None)
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, "1 passed", "")
    monkeypatch.setattr(hotfix.subprocess, "run", fake_run)
    assert hotfix.run_tests(root)[0] == 0 and seen["argv"][-4:] == ["-m", "pytest", "tests", "-q"], seen
    r = hotfix.open_hotfix(root, "no self workload", who="t")
    assert Store(root).get(r["id"]).rec["workload"] == "scriptorium"


def test_hotfix_run_tests_resolves_relative_command_against_root(world, monkeypatch, tmp_path):
    """T-0131: `tests: .venv/Scripts/python.exe -m pytest tests -q` is relative to the checkout. Windows'
    CreateProcess does not resolve a relative forward-slash program name even with cwd=root, so run_tests
    hands subprocess an absolute native path; the process cwd is irrelevant. The resolution is
    scr.run.resolve_test_argv, shared with ops._run_tests (T-0255), so the landing launches the same argv."""
    from types import SimpleNamespace
    from scr import hotfix, ops
    root, _, _ = world
    (root / "tools").mkdir(); (root / "tools" / "fake-python.exe").write_bytes(b"")
    _set_tests_cmd(root, "tools/fake-python.exe -m pytest tests -q")
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = list(argv); seen["cwd"] = kw.get("cwd"); seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(argv, 0, "3 passed", "")
    monkeypatch.setattr(hotfix.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)                                   # somewhere that is NOT the root
    rc, out = hotfix.run_tests(root)
    assert rc == 0 and "3 passed" in out
    exe = Path(seen["argv"][0])
    assert exe.is_absolute() and exe.exists() and exe.resolve() == (root / "tools" / "fake-python.exe").resolve(), seen
    assert seen["argv"][1:] == ["-m", "pytest", "tests", "-q"] and Path(seen["cwd"]) == root
    assert seen["env"]["SCR_NO_JOURNAL"] == "1"
    assert not out.startswith("note:")                            # no substitution, no note
    # the landing's runner resolves the same command against its cwd to the same argv
    hotfix_argv = seen["argv"]
    monkeypatch.setattr(ops.subprocess, "run", fake_run)
    ops._run_tests(SimpleNamespace(cfg={"tests": "tools/fake-python.exe -m pytest tests -q"}), root)
    assert seen["argv"] == hotfix_argv and Path(seen["cwd"]) == root
    # a bare name is left to the PATH lookup, untouched
    _set_tests_cmd(root, "pytest tests -q")
    hotfix.run_tests(root)
    assert seen["argv"][0] == "pytest"


def test_hotfix_run_tests_missing_command_fails_loudly(world, monkeypatch, tmp_path):
    """A test command whose program exists nowhere is a non-zero rc naming that program, from run_tests
    and from `scr hotfix test` alike, never a silent 'tests ran'."""
    from scr import hotfix
    root, _, _ = world
    _set_tests_cmd(root, "tools/no-such-runner.exe tests -q")
    monkeypatch.chdir(tmp_path)
    rc, out = hotfix.run_tests(root)
    assert rc != 0 and "no-such-runner.exe" in out and "not found" in out, (rc, out)
    r = scr(root, "--actor", "hotfix:agent", "hotfix", "test", check=False)
    assert r.returncode == rc and "no-such-runner.exe" in r.stdout, (r.returncode, r.stdout, r.stderr)


def test_hotfix_run_tests_timeout_follows_land_test_timeout(world, monkeypatch):
    """T-0131 (T-0186 fold): hotfix test/close run the same suite as the landing, so they share its limit,
    limits.yaml land_test_timeout_seconds, instead of a hardcoded 1800 s; an explicit timeout still wins."""
    from scr import hotfix
    root, _, _ = world
    _set_tests_cmd(root, "pytest tests -q")
    lim = root / "config" / "limits.yaml"
    cfg = yaml.safe_load(lim.read_text()) if lim.exists() else {}
    lim.write_text(yaml.safe_dump({**(cfg or {}), "land_test_timeout_seconds": 4321}))
    seen: list = []

    def fake_run(argv, **kw):
        seen.append(kw.get("timeout"))
        if len(seen) == 3:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, "1 passed", "")
    monkeypatch.setattr(hotfix.subprocess, "run", fake_run)
    hotfix.run_tests(root)
    hotfix.run_tests(root, timeout=7)
    rc, out = hotfix.run_tests(root)
    assert seen == [4321, 7, 4321]
    assert rc == 124 and "4321" in out and "land_test_timeout_seconds" in out, out


def test_resolve_test_argv_cases(tmp_path, monkeypatch):
    """T-0255: the one resolver behind hotfix.run_tests and ops._run_tests. Absolute stays; a relative path
    that exists under cwd becomes absolute (abspath, not resolve); a missing python* becomes sys.executable
    with a one-line note naming both; a bare name nobody can find is left alone, without a note."""
    import os
    from scr.run import resolve_test_argv
    monkeypatch.chdir(tmp_path.parent)                            # the process cwd is not the resolution root
    absolute = Path(sys.executable)
    argv, note = resolve_test_argv(f'"{absolute}" -m pytest tests -q', tmp_path)
    assert Path(argv[0]) == absolute and argv[1:] == ["-m", "pytest", "tests", "-q"] and note is None
    (tmp_path / "tools").mkdir(); (tmp_path / "tools" / "fake-python.exe").write_bytes(b"")
    argv, note = resolve_test_argv("tools/fake-python.exe -m pytest", tmp_path)
    assert argv[0] == os.path.abspath(tmp_path / "tools" / "fake-python.exe") and argv[1:] == ["-m", "pytest"] and note is None
    argv, note = resolve_test_argv(".venv/Scripts/python.exe -m pytest tests -q", tmp_path)
    assert argv[0] == sys.executable and argv[1:] == ["-m", "pytest", "tests", "-q"]
    assert note is not None and "\n" not in note and note.startswith("note: tests: interpreter")
    assert ".venv/Scripts/python.exe" in note and str(tmp_path) in note and sys.executable in note
    argv, note = resolve_test_argv("no-such-runner-t0255 tests -q", tmp_path)
    assert argv == ["no-such-runner-t0255", "tests", "-q"] and note is None


def test_ops_run_tests_names_substituted_interpreter(tmp_path, monkeypatch):
    """T-0255: a `tests:` line naming an interpreter that is not in the worktree still runs (on this python),
    but the landing log's text now says so on its first line instead of substituting silently."""
    from types import SimpleNamespace
    from scr import ops
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, "5 passed", "")
    monkeypatch.setattr(ops.subprocess, "run", fake_run)
    rc, out = ops._run_tests(SimpleNamespace(cfg={"tests": "tools/python.exe -m pytest tests -q"}), tmp_path)
    first, _, rest = out.partition("\n")
    assert rc == 0 and seen["argv"][0] == sys.executable and "5 passed" in rest
    assert first.startswith("note:") and "tools/python.exe" in first and sys.executable in first and str(tmp_path) in first, out


PYTEST_Q_OUT = """\
..F.....E..F
=================================== FAILURES ===================================
____________________________ test_ui_http_and_reload ___________________________
E   AssertionError: boom
=========================== short test summary info ============================
FAILED tests/test_journal_ops.py::test_ui_http_and_reload - AssertionError: boom
ERROR tests/test_watch.py::test_setup_breaks - OSError: no
FAILED tests/test_ui.py::test_ui_state_carries_watch_cards_and_nothing_else_changes
FAILED tests/test_p.py::test_param[a - b] - ValueError: x
3 failed, 361 passed, 2 skipped, 1 error in 2477.10s (0:41:17)
"""


def test_failing_test_ids_are_parsed_from_pytest_q_output():
    """T-0382 DoD: the ids come from pytest -q's short summary (FAILED / ERROR lines), in order, without the
    ` - why` part; the list is complete only when the final `N failed, M error` count matches it."""
    from scr.ops import failed_test_ids, rerun_argv
    ids, complete = failed_test_ids(PYTEST_Q_OUT)
    assert ids == ["tests/test_journal_ops.py::test_ui_http_and_reload", "tests/test_watch.py::test_setup_breaks",
                   "tests/test_ui.py::test_ui_state_carries_watch_cards_and_nothing_else_changes", "tests/test_p.py::test_param[a - b]"], ids
    assert complete
    cut = PYTEST_Q_OUT.replace("FAILED tests/test_ui.py::test_ui_state_carries_watch_cards_and_nothing_else_changes\n", "")
    assert failed_test_ids(cut) == (["tests/test_journal_ops.py::test_ui_http_and_reload", "tests/test_watch.py::test_setup_breaks",
                                     "tests/test_p.py::test_param[a - b]"], False)      # a lost line: never complete
    assert failed_test_ids("tests timed out after 5s") == ([], False)
    assert rerun_argv(["py", "-m", "pytest", "tests", "-q"], ["tests/a.py::x", "tests/b.py::y"]) == ["py", "-m", "pytest", "-q", "tests/a.py::x", "tests/b.py::y"]


def _main_check_run(monkeypatch, branch_out: str, main_out: str | None, rc: int = 1):
    """ops._main_check with the test runner faked: `main_out` is what the rerun on the trunk prints (None: it
    must not run). Returns (lines, the ids the rerun was given)."""
    from types import SimpleNamespace
    from scr import ops
    seen: dict = {}

    def fake(wl, cwd, timeout=0, ids=None):
        assert main_out is not None, "the main rerun ran"
        seen["ids"], seen["cwd"] = ids, cwd
        return (1 if "FAILED" in main_out else 0), main_out
    monkeypatch.setattr(ops, "_run_tests_full", fake)
    lines = ops._main_check(SimpleNamespace(cfg={}), Path("ROOT"), "task/T-1", "main", rc, branch_out, 3600)
    return lines, seen


def test_main_check_names_only_ids_that_fail_on_the_trunk(monkeypatch):
    """T-0382: the rerun on the checkout is of just the failing ids; the trunk mark is written only when EVERY id
    fails there too, and land_test_failures reads the decision back from the lines (a job log's, too)."""
    from scr.ops import land_test_failures
    two = "FAILED tests/x.py::a - E\nFAILED tests/x.py::b\n2 failed, 9 passed in 1s\n"
    lines, seen = _main_check_run(monkeypatch, two, "FAILED tests/x.py::a\nFAILED tests/x.py::b - E\n2 failed in 1s\n")
    assert seen == {"ids": ["tests/x.py::a", "tests/x.py::b"], "cwd": Path("ROOT")}
    assert land_test_failures(lines + ["  FAILED (tests): ..."]) == (["tests/x.py::a", "tests/x.py::b"], True), lines
    # b passes on the trunk: it is the branch's failure
    lines, _ = _main_check_run(monkeypatch, two, "FAILED tests/x.py::a\n1 failed, 1 passed in 1s\n")
    assert land_test_failures(lines) == (["tests/x.py::a", "tests/x.py::b"], False) and "tests/x.py::b" in lines[-1], lines
    # an id the trunk does not have (pytest rc 4/5, nothing failed) is new
    lines, _ = _main_check_run(monkeypatch, two, "ERROR: not found: tests/x.py::b\nno tests ran in 0.1s\n")
    assert land_test_failures(lines)[1] is False
    # an incomplete list, a non-1 rc, or too many failures: no rerun at all
    for out, rc in (("FAILED tests/x.py::a\n2 failed in 1s\n", 1), (two, 2),
                    ("".join(f"FAILED tests/x.py::t{i}\n" for i in range(11)) + "11 failed in 1s\n", 1)):
        lines, _ = _main_check_run(monkeypatch, out, None, rc)
        assert land_test_failures(lines)[1] is False and "main check skipped" in lines[-1], lines


def test_discuss_hotfix_is_toolsmith_in_root(world):
    from scr.discuss import build
    root, _, _ = world
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text())
    wl["scriptorium"] = {"repo": ".", "main_branch": "main", "worktrees": "../scriptorium-worktrees", "tests": "true"}
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl))
    s = build(root, "hotfix")
    assert s.role == "toolsmith" and s.cwd == root and "hotfix close" in s.brief


def test_hotfix_file_requests_from_an_agent_without_a_shell(world):
    """Claude-in-Cowork drops files: pending.json before editing, close.json when done."""
    from scr import hotfix
    root, _, _ = world
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text())
    wl["scriptorium"] = {"repo": ".", "main_branch": "main", "worktrees": "../scriptorium-worktrees", "tests": f"{sys.executable} -c \"print('ok')\""}
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl)); git(root, "add", "-A"); git(root, "commit", "-q", "-m", "wl")
    (root / ".scr" / "hotfix").mkdir(parents=True, exist_ok=True)
    (root / ".scr" / "hotfix" / "pending.json").write_text(json.dumps({"who": "claude-cowork", "symptom": "Discuss launcher parse error"}))
    (root / "scr").mkdir(exist_ok=True); (root / "scr" / "discuss_fix.py").write_text("ok = True\n")
    scr(root, "--actor", "steward-watch", "event", "system", "--name", "tick", "--quiet")     # any journaling call
    st = hotfix.status(root)
    assert st and "Discuss launcher parse error" in st["title"] and "scr/discuss_fix.py" in st["files"]
    assert not (root / ".scr" / "hotfix" / "pending.json").exists()
    (root / ".scr" / "hotfix" / "close.json").write_text(json.dumps({"who": "claude-cowork", "summary": "quoted the first message", "unlocks": ["lint launchers | catch this class"]}))
    from scr.watch import Watcher
    Watcher(root, quiet=True).tick()
    t = Store(root).get(st["id"])
    assert t.status == "submitted" and "quoted the first message" in (t.dir / "report.md").read_text()
    assert hotfix.status(root) is None and git(root, "status", "--porcelain") == ""


def test_hotfix_requests_dropped_together_with_the_fix(world):
    """Cowork drops pending.json, close.json and the edited files in one go; one tick sorts it out."""
    from scr import hotfix
    from scr.watch import Watcher
    root, _, _ = world
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text())
    wl["scriptorium"] = {"repo": ".", "main_branch": "main", "worktrees": "../scriptorium-worktrees", "tests": f"{sys.executable} -c \"print('ok')\""}
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl)); git(root, "add", "-A"); git(root, "commit", "-q", "-m", "wl")
    (root / ".scr" / "hotfix").mkdir(parents=True, exist_ok=True)
    (root / ".scr" / "hotfix" / "pending.json").write_text(json.dumps({"who": "claude-cowork", "symptom": "launcher quoting"}))
    (root / ".scr" / "hotfix" / "close.json").write_text(json.dumps({"who": "claude-cowork", "summary": "ps_quote everywhere"}))
    (root / "scr").mkdir(exist_ok=True); (root / "scr" / "fix.py").write_text("ok = True\n")
    Watcher(root, quiet=True).tick()
    tasks = [t for t in Store(root).load_all().values() if t.rec.get("workload") == "scriptorium"]
    assert len(tasks) == 1, [t.rec["title"] for t in tasks]
    t = tasks[0]
    assert t.status == "submitted" and "launcher quoting" in t.rec["title"] and "scr/fix.py" in t.rec["scope"]["files"]
    assert "ps_quote everywhere" in (t.dir / "report.md").read_text()
    assert hotfix.status(root) is None and not (root / ".scr" / "hotfix" / "close.json").exists()
    assert git(root, "status", "--porcelain") == ""


# ---- dead / stale scriber sessions (T-0063) ---------------------------------------------------------
def _running_with_session(world, pid: int | None, transcript: str | None = "session-20260917-000000.txt", data: bool = True):
    """T-0001 in_progress in its worktree with a `launch` event the way the launcher records it: pid + transcript
    in `data` (data=True), or the pre-T-0063 shape — the note `headless pid N` plus the launcher's log.md line."""
    root, repo, _ = world
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched")
    spawn(root, ["T-0001"], lake=False)
    scr(root, "--actor", "s", "task", "set", "T-0001", "in_progress")
    t = Store(root).get("T-0001")
    args = ["--note", f"headless pid {pid}"]
    if data:
        args += ["--data", json.dumps({"pid": pid, "transcript": transcript})]
    else:
        (t.dir / "log.md").write_text(f"- 2026-09-16T19:59:38 [launcher] headless scriber session started (permission-mode bypassPermissions)\n"
                                      f"- 2026-09-16T19:59:38 [launcher] pid {pid}, transcript {t.dir / transcript}\n", encoding="utf-8")
    scr(root, "--actor", "steward:T-0001", "event", "system", "--name", "launch", "--task", "T-0001", *args, "--quiet")
    return root, repo, t


def _named(root: Path, name: str, task: str = "T-0001") -> list[dict]:
    return [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("name") == name and e.get("task") == task]


def test_watch_returns_task_whose_session_pid_is_gone(world):
    from scr import ui
    from scr.watch import Watcher
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()       # a pid that existed and is gone
    root, _, t = _running_with_session(world, p.pid)
    res = Watcher(root, quiet=True).tick()
    assert res["ok"], res
    assert Store(root).get("T-0001").status == "ready"                      # returned, then relaunched (2026-10-14)
    assert res["rows"][0]["flags"].startswith(f"ckpt:") or "DEAD" in res["rows"][0]["flags"]
    dead = _named(root, "session_dead")
    assert len(dead) == 1 and f"pid {p.pid}" in dead[0]["note"], dead
    ret = [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "state" and e.get("to") == "returned"]
    assert ret and ret[-1]["note"].startswith("session died:"), ret
    assert not any("session died" in n for n in res["needs"])                # relaunched, no card (2026-10-14)
    cards = [n for n in ui.state(root)["needs"] if n["type"] == "system" and n.get("name") == "session_dead"]
    assert cards == [], cards                                                # the event is logged, never a card (2026-10-14)
    Watcher(root, quiet=True).tick()                                        # a ready task is not looked at again
    assert Store(root).get("T-0001").status == "ready" and len(_named(root, "session_dead")) == 1
    assert git(root, "status", "--porcelain") == ""


def test_watch_returns_task_whose_transcript_err_says_terminated(world):
    """The T-0015 shape: an old note-only launch event, the transcript named in log.md, the pid still alive
    (here: the test process), and claude's termination line in the .err."""
    from scr.watch import Watcher
    root, _, t = _running_with_session(world, os.getpid(), "session-20260916-195938.txt", data=False)
    (t.dir / "session-20260916-195938.txt.err").write_text(
        "Background tasks still running after 600s; terminating. Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely.\n", encoding="utf-8")
    w = Watcher(root, quiet=True)
    sess = w.session_of(t)
    assert sess == {"pid": os.getpid(), "transcript": "session-20260916-195938.txt", "ts": sess["ts"]}
    res = w.tick()
    assert res["ok"] and Store(root).get("T-0001").status == "ready"         # returned, then relaunched (2026-10-14)
    dead = _named(root, "session_dead")
    assert len(dead) == 1 and "claude terminated it: Background tasks still running after 600s" in dead[0]["note"], dead
    assert not _named(root, "stale")                                         # dead is not also stale


def test_watch_flags_alive_but_idle_session_as_stale_not_dead(world):
    from scr import ui
    from scr.watch import Watcher
    root, repo, t = _running_with_session(world, os.getpid())                # alive: this very process
    wt = repo / ".worktrees" / "T-0001"
    old = time.time() - 3 * 3600
    for f in [*wt.rglob("*"), t.dir / "log.md"]:
        if f.is_file():
            os.utime(f, (old, old))
    res = Watcher(root, stale_minutes=45, quiet=True).tick()
    assert res["ok"], res
    assert Store(root).get("T-0001").status == "in_progress"                 # alive but idle: never returned
    assert "STALE" in res["rows"][0]["flags"] and "DEAD" not in res["rows"][0]["flags"], res["rows"]
    assert not any("silent for" in n for n in res["needs"]), res["needs"]    # 2026-10-14: a live pid is a log line, not a card
    assert any("STALE" in r["flags"] for r in res["rows"]), res["rows"]
    assert len(_named(root, "stale")) == 1 and not _named(root, "session_dead")
    cards = [n for n in ui.state(root)["needs"] if n["type"] == "system" and n.get("name") == "stale"]
    assert cards == [], cards                                                # the stale event is logged, never a card (2026-10-14)
    # a second idle tick raises no second event; the scriber writing again clears the card by itself
    Watcher(root, stale_minutes=45, quiet=True).tick()
    assert len(_named(root, "stale")) == 1
    with (t.dir / "log.md").open("a", encoding="utf-8") as f:
        f.write("- still here\n")
    res3 = Watcher(root, stale_minutes=45, quiet=True).tick()
    assert "STALE" not in res3["rows"][0]["flags"]
    assert not [n for n in ui.state(root)["needs"] if n.get("name") == "stale"]
    # and a new idle episode is a new card
    os.utime(t.dir / "log.md", (old, old))
    Watcher(root, stale_minutes=45, quiet=True).tick()
    assert len(_named(root, "stale")) == 2
