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


def test_resume_argv_continues_the_session(monkeypatch, tmp_path):
    monkeypatch.setattr(run_mod, "claude_bin", lambda: str(Path(sys.executable).with_name("claude.exe")))
    monkeypatch.setattr(run_mod, "permission_mode_for", lambda root: "auto")
    argv = run_mod.session_argv(tmp_path, root=tmp_path, resume="sid-123")
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sid-123" and "--session-id" not in argv
    fresh = run_mod.session_argv(tmp_path, root=tmp_path, session_id="sid-456")
    assert "--session-id" in fresh and "--resume" not in fresh
