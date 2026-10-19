"""The job runner (scr/jobs.py, 2026-10-10): long commands run detached under the watcher; a scriber hands off with
`WAITING: job J-xxxx`, the task's `waiting` status holds no slot, the watcher polls, kills at the deadline and resumes
the session by id. These tests need no scriptorium world: a tmp root with .scr/jobs is enough."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scr import jobs, lifecycle
import scr.run as run_mod
import scr.watch as watch_mod


def test_parse_timeout():
    assert jobs.parse_timeout(None) == jobs.DEFAULT_TIMEOUT_MIN
    assert jobs.parse_timeout("4h") == 240
    assert jobs.parse_timeout("90m") == 90
    assert jobs.parse_timeout("30") == 30
    assert jobs.parse_timeout("1.5h") == 90
    assert jobs.parse_timeout(15) == 15
    for bad in ("0", "-5", "soon", "2w", True):
        with pytest.raises(ValueError):
            jobs.parse_timeout(bad)


def test_waiting_line_is_found_only_at_the_end():
    assert jobs.waiting_job_in("- did things\n- WAITING: job J-0007\n") == "J-0007"
    assert jobs.waiting_job_in("WAITING: job j-0012") == "J-0012"
    assert jobs.waiting_job_in("- WAITING: job J-0001\n" + "\n".join(f"- line {i}" for i in range(20))) is None   # too far up
    assert jobs.waiting_job_in("- HELP: something\n") is None
    assert jobs.waiting_job_in("") is None


@pytest.fixture
def root(tmp_path, monkeypatch):
    started: list[list[str]] = []
    monkeypatch.setattr(jobs, "start_detached", lambda args, cwd, log=None: started.append(list(args)) or 4242)
    monkeypatch.setattr(jobs, "concurrency", lambda r: 1)
    monkeypatch.setattr(jobs, "pid_alive", lambda pid: True)
    killed: list[int] = []
    monkeypatch.setattr(jobs, "kill", lambda pid: killed.append(pid) or True)
    tmp_path.joinpath(".scr").mkdir()
    return {"root": tmp_path, "started": started, "killed": killed}


def test_submit_records_and_starts_the_wrapper_detached(root):
    j = jobs.submit(root["root"], ["lake", "build"], task="T-0001", name="build", timeout="2h", session="abc")
    assert j["id"] == "J-0001" and j["status"] == "running" and j["pid"] == 4242 and j["timeout_min"] == 120
    assert root["started"][0][-3:] == ["--run", str(root["root"].resolve()), "J-0001"]     # python -m scr.jobs --run <root> <job>
    on_disk = json.loads((root["root"] / ".scr" / "jobs" / "J-0001.json").read_text())
    assert on_disk["cmd"] == ["lake", "build"] and on_disk["task"] == "T-0001" and on_disk["session"] == "abc"
    assert on_disk["deadline"] > on_disk["started"]


def test_over_capacity_queues_and_poll_starts_the_next(root):
    jobs.submit(root["root"], ["a"], name="first")
    j2 = jobs.submit(root["root"], ["b"], name="second")
    assert j2["status"] == "queued" and len(root["started"]) == 1
    # the first finishes (its wrapper wrote rc) -> poll starts the queued one
    j1 = jobs.load(root["root"], "J-0001"); j1.update({"status": "done", "rc": 0, "ended": jobs.now_iso()}); jobs.save(root["root"], j1)
    changed = jobs.poll(root["root"])
    assert [c["id"] for c in changed] == ["J-0002"] and changed[0]["transition"] == "started"
    assert jobs.load(root["root"], "J-0002")["status"] == "running" and len(root["started"]) == 2


def test_poll_marks_a_dead_wrapper_failed_and_enforces_the_deadline(root, monkeypatch):
    jobs.submit(root["root"], ["a"], name="dies")
    monkeypatch.setattr(jobs, "pid_alive", lambda pid: False)
    changed = jobs.poll(root["root"])
    assert changed[0]["id"] == "J-0001" and changed[0]["status"] == "failed" and "gone" in changed[0]["note"]
    monkeypatch.setattr(jobs, "pid_alive", lambda pid: True)
    j = jobs.submit(root["root"], ["b"], name="slow", timeout="1m")
    late = datetime.now(timezone.utc) + timedelta(minutes=2)
    changed = jobs.poll(root["root"], now=late)
    assert changed[0]["id"] == j["id"] and changed[0]["status"] == "timeout" and root["killed"] == [4242]


def test_over_twelve_hours_is_omris_alone(root):
    with pytest.raises(ValueError):
        jobs.submit(root["root"], ["a"], timeout="13h")
    assert jobs.submit(root["root"], ["a"], timeout="13h", actor="omri")["timeout_min"] == 780


def test_wrapper_runs_the_command_and_records_rc(tmp_path):
    tmp_path.joinpath(".scr").mkdir()
    rec = {"id": "J-0001", "task": None, "name": "echo", "cmd": [sys.executable, "-c", "print('hello from the job')"],
           "cwd": str(tmp_path), "log": str(tmp_path / ".scr" / "jobs" / "J-0001.log"), "timeout_min": 5, "session": None,
           "status": "running", "pid": 1, "started": jobs.now_iso(), "deadline": None, "rc": None, "ended": None}
    jobs.jobs_dir(tmp_path); jobs.save(tmp_path, rec)
    assert jobs.run_wrapper(tmp_path, "J-0001") == 0
    j = jobs.load(tmp_path, "J-0001")
    assert j["status"] == "done" and j["rc"] == 0 and j["ended"] and "hello from the job" in jobs.tail(tmp_path, "J-0001")
    rec.update({"id": "J-0002", "cmd": [sys.executable, "-c", "import sys; sys.exit(3)"], "log": str(tmp_path / ".scr" / "jobs" / "J-0002.log")})
    jobs.save(tmp_path, rec)
    assert jobs.run_wrapper(tmp_path, "J-0002") == 3
    assert jobs.load(tmp_path, "J-0002")["status"] == "failed"


def test_waiting_is_a_status_that_holds_no_slot():
    assert lifecycle.allowed({"status": "in_progress"}, "waiting")
    assert lifecycle.allowed({"status": "checkpoint"}, "waiting")
    assert lifecycle.allowed({"status": "waiting"}, "in_progress")
    assert lifecycle.allowed({"status": "waiting"}, "returned")
    assert not lifecycle.allowed({"status": "ready"}, "waiting")
    assert not lifecycle.allowed({"status": "submitted"}, "waiting")        # a reviewer cannot wait: it stays in the foreground
    assert "waiting" not in watch_mod.SLOT_STATUSES


def test_a_resume_costs_no_attempt():
    """T-0342: attempts = scriber sessions started (T-0127). `waiting -> in_progress` resumes the same session and is
    not charged; `dispatched -> in_progress` (a new session) still adds 1; a checkpoint heartbeat adds nothing."""
    rec = {"status": "waiting", "attempts": 1}
    lifecycle.apply(rec, "in_progress")
    assert rec["status"] == "in_progress" and rec["attempts"] == 1
    lifecycle.apply(rec, "checkpoint")
    lifecycle.apply(rec, "in_progress")
    assert rec["attempts"] == 1
    lifecycle.apply(rec, "waiting")
    lifecycle.apply(rec, "in_progress")
    assert rec["attempts"] == 1
    rec2 = {"status": "dispatched", "attempts": 1}
    lifecycle.apply(rec2, "in_progress")
    assert rec2["attempts"] == 2


def test_resume_argv_continues_the_session(monkeypatch, tmp_path):
    monkeypatch.setattr(run_mod, "claude_bin", lambda: str(Path(sys.executable).with_name("claude.exe")))
    monkeypatch.setattr(run_mod, "permission_mode_for", lambda root: "auto")
    argv = run_mod.session_argv(tmp_path, root=tmp_path, resume="sid-123")
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sid-123" and "--session-id" not in argv
    fresh = run_mod.session_argv(tmp_path, root=tmp_path, session_id="sid-456")
    assert "--session-id" in fresh and "--resume" not in fresh


def test_job_submit_argv_does_not_shadow_the_subcommand_name():
    """`scr job submit ... -- <cmd>`: the trailing arguments must not be parsed into `cmd`, which main() reads as the
    subcommand name for the journal step (2026-10-11: TypeError unhashable list after the job had already started)."""
    from scr.cli import build_parser, job_args
    a = build_parser().parse_args(["job", "submit", "--task", "T-0001", "--name", "t", "--timeout", "3h", "--", "python", "-m", "pytest"])
    assert a.cmd == "job"
    merged, cmd = job_args(a)
    assert cmd == ["python", "-m", "pytest"], cmd                      # never `--task` as the program (J-0007, J-0010: rc 127)
    assert merged.task == "T-0001" and merged.name == "t" and merged.timeout == "3h"
    b = build_parser().parse_args(["job", "tail", "J-0010", "-n", "5"])
    mb, cb = job_args(b)
    assert mb.id == "J-0010" and mb.n == 5 and cb == []
    c = build_parser().parse_args(["job", "list", "--all"])
    assert job_args(c)[0].all is True


def test_waiting_is_a_known_state_everywhere():
    """2026-10-12: `scr verify` reported "T-0136: unknown status waiting" and the watcher went read-only for 12 h —
    store.STATES is the verifier's list and must carry every lifecycle status."""
    from scr.store import STATES, OPEN_STATES
    assert "waiting" in STATES and "waiting" in OPEN_STATES
    assert set(lifecycle.TRANSITIONS) <= set(STATES), set(lifecycle.TRANSITIONS) - set(STATES)


# ---- T-0335: `scr --root <live> job submit` from a worktree records under the live root, runs in the worktree ----------
def _live_root(tmp_path: Path) -> Path:
    live = tmp_path / "live"
    for d in ("tasks", "config", "events", ".scr"):
        (live / d).mkdir(parents=True)
    return live


def _submit_from(root, monkeypatch, live: Path, worktree: Path, *extra: str) -> dict:
    from scr.cli import build_parser, cmd_job
    monkeypatch.delenv("SCRIPTORIUM_ROOT", raising=False)
    monkeypatch.chdir(worktree)
    args = build_parser().parse_args(["--root", str(live), "--actor", "steward:T-0001", "job", "submit", *extra,
                                      "--name", "check", "--timeout", "1h", "--", sys.executable, "-V"])
    cmd_job(args)
    return json.loads((live / ".scr" / "jobs" / "J-0001.json").read_text())


def test_submit_from_a_lean_worktree_records_under_the_live_root_with_the_worktree_cwd(root, tmp_path, monkeypatch):
    live = _live_root(tmp_path)
    wt = tmp_path / "quantum-insertion" / ".worktrees" / "T-0001"         # outside the scriptorium tree
    wt.mkdir(parents=True)
    job = _submit_from(root, monkeypatch, live, wt)
    assert Path(job["cwd"]) == wt.resolve() and job["name"] == "check" and job["cmd"] == [sys.executable, "-V"]
    assert job["actor"] == "steward:T-0001"
    assert not (wt / ".scr").exists()


def test_submit_from_a_tooling_worktree_still_records_under_the_live_root(root, tmp_path, monkeypatch):
    live = _live_root(tmp_path)
    wt = tmp_path / "scriptorium-worktrees" / "T-0001"
    for d in ("tasks", "config"):                                          # find_root would pick THIS tree without --root
        (wt / d).mkdir(parents=True)
    job = _submit_from(root, monkeypatch, live, wt)
    assert Path(job["cwd"]) == wt.resolve()
    assert not (wt / ".scr" / "jobs").exists()
    # an explicit --cwd still wins over the caller's directory
    other = tmp_path / "elsewhere"; other.mkdir()
    (live / ".scr" / "jobs" / "J-0001.json").unlink()
    assert Path(_submit_from(root, monkeypatch, live, wt, "--cwd", str(other))["cwd"]) == other.resolve()


def test_submit_refuses_a_flag_as_the_name_or_the_program(root):
    """J-0001: `job submit --root ...` made `--root` the name; J-0002: `-m pytest` lost the interpreter (rc 127)."""
    with pytest.raises(ValueError):
        jobs.submit(root["root"], ["-m", "pytest"], name="t")
    with pytest.raises(ValueError):
        jobs.submit(root["root"], ["python"], name="--root")
    assert not list((root["root"] / ".scr" / "jobs").glob("J-*.json"))


def test_scriber_brief_passes_the_live_root_before_job(tmp_path):
    from scr.brief import BRIEF_TEMPLATE, brief_for
    assert "{scr} --root {root} job submit" in BRIEF_TEMPLATE and "{scr} --root {root} job tail" in BRIEF_TEMPLATE
    live = tmp_path / "live"
    (live / "config").mkdir(parents=True)
    tdir = live / "tasks" / "T-0001-x"
    tdir.mkdir(parents=True)

    class T:                                                               # the fields brief_for reads
        id, dir, rec = "T-0001", tdir, {"title": "x", "worktree": "../wt/T-0001"}

    class S:
        def config(self, name):
            return {}

    text = brief_for(live, S(), T())
    assert f"--root {live.resolve()} job submit --task T-0001" in text
    assert f"--root {live.resolve()} job tail" in text


def test_landing_jobs_have_their_own_lane(tmp_path, monkeypatch):
    """2026-10-15: a queued `land-<id>` job starts even when jobs_concurrency is full; a second landing waits."""
    from datetime import datetime, timezone
    from scr import jobs
    root = tmp_path / "s"; (root / ".scr" / "jobs").mkdir(parents=True); (root / "config").mkdir()
    (root / "config" / "limits.yaml").write_text("jobs_concurrency: 1\n", encoding="utf-8")
    started = []
    monkeypatch.setattr(jobs, "_start", lambda r, j: started.append(j["id"]) or j.update(status="running", pid=1, started=jobs.now_iso()) or jobs.save(r, j))
    monkeypatch.setattr(jobs, "pid_alive", lambda pid: True)
    import json
    for jid, name, st in (("J-0001", "suite", "running"), ("J-0002", "build", "queued"), ("J-0003", "land-T-0001", "queued"), ("J-0004", "land-T-0002", "queued")):
        (root / ".scr" / "jobs" / f"{jid}.json").write_text(json.dumps({"id": jid, "name": name, "status": st, "cmd": ["x"], "pid": 1 if st == "running" else None, "timeout_min": 60}), encoding="utf-8")
    jobs.poll(root, datetime.now(timezone.utc))
    assert started == ["J-0003"]          # the landing lane; J-0002 waits for jobs_concurrency, J-0004 for the lane


def test_event_appends_from_many_processes_never_fork(tmp_path):
    """T-0333 (done 2026-10-15): 8 processes appending 25 events each through EventLog.append leave one unbroken chain."""
    import subprocess, sys
    from scr.events import EventLog
    log = tmp_path / "events.jsonl"
    code = ("import sys; from pathlib import Path; from scr.events import EventLog\n"
            "l = EventLog(Path(sys.argv[1]))\n"
            "for i in range(25): l.append('system', name='probe', actor=sys.argv[2], note=str(i))\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(log), f"p{i}"], cwd=str(Path(__file__).resolve().parent.parent)) for i in range(8)]
    assert all(p.wait() == 0 for p in procs)
    assert EventLog(log).verify_chain() == [] and len(EventLog(log).read()) == 200


def test_the_watchers_landing_job_runs_in_the_landing_lane(tmp_path, monkeypatch):
    """T-0391: the job land_via_job submits is named with jobs.LAND_PREFIX (one constant, not a watcher copy), so a
    full jobs_concurrency does not hold it: poll() starts it in the landing lane (the J-0112 stall, T-0383)."""
    from types import SimpleNamespace
    root = tmp_path / "s"; (root / ".scr" / "jobs").mkdir(parents=True); (root / "config").mkdir()
    (root / "config" / "limits.yaml").write_text("jobs_concurrency: 1\n", encoding="utf-8")
    wt = tmp_path / "wt"; wt.mkdir()
    started = []
    monkeypatch.setattr(jobs, "_start", lambda r, j: started.append(j["id"]) or j.update(status="running", pid=1, started=jobs.now_iso()) or jobs.save(r, j))
    monkeypatch.setattr(jobs, "pid_alive", lambda pid: True)
    (root / ".scr" / "jobs" / "J-0001.json").write_text(json.dumps({"id": "J-0001", "name": "suite", "status": "running", "cmd": ["x"], "pid": 1, "timeout_min": 60}), encoding="utf-8")
    monkeypatch.setattr(watch_mod, "is_scriptorium_task", lambda r, s, t: False)
    monkeypatch.setattr(watch_mod, "workload_for", lambda r, s, t: None)
    monkeypatch.setattr(watch_mod, "task_worktree", lambda wl, t: wt)
    monkeypatch.setattr(watch_mod, "scr_exe", lambda r: ["scr"])
    w = object.__new__(watch_mod.Watcher)
    w.root, w.store, w.flags, w.quiet = root, None, set(), True
    w.log = lambda line: None
    assert w.land_via_job(SimpleNamespace(id="T-0001", rec={})) == "submitted"
    [job] = [j for j in jobs.list_jobs(root) if j["id"] != "J-0001"]
    assert job["name"] == "land-T-0001" and job["name"].startswith(jobs.LAND_PREFIX)
    assert job["status"] == "queued" and started == []      # jobs_concurrency 1 is full at submit time
    jobs.poll(root, datetime.now(timezone.utc))
    assert started == [job["id"]]                           # the landing lane starts it anyway
