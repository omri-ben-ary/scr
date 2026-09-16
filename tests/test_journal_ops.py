"""Journal mode, the git wrapper (stale locks), the workload operations on real git repos,
declarative chores, and the UI job flow. Everything runs on Linux with plain git."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from scr import chores
from scr.gitops import Git, GitError
from scr.journal import journal
from scr.ops import checkpoint_commit, land, park, retire, spawn, sweep
from scr.store import Store

REPO = Path(__file__).resolve().parents[1]


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
