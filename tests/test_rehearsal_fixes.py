"""Two failures the v0.2.0 rehearsal hit on the PC: a dry tick that crashed on a missing workload repo (Windows raises
NotADirectoryError for a missing cwd, not FileNotFoundError) and a UI state call slow enough to time out."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

import scr.store as store_mod
from scr.gitops import Git, gh


def test_git_in_a_missing_directory_is_a_failed_result_not_a_crash(tmp_path: Path):
    r = Git(tmp_path / "quantum-insertion").run("status")
    assert r.rc != 0 and "not a directory" in r.out


def test_git_start_error_like_windows_267_is_a_failed_result(tmp_path: Path, monkeypatch):
    def boom(*a, **kw):
        raise NotADirectoryError(20, "The directory name is invalid", None, 267, None)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(Git, "has_identity", lambda self: True)
    assert Git(tmp_path).run("status").rc == 128
    assert gh("pr", "list", cwd=tmp_path).rc == 128


def _record(d: Path, tid: str, title: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    f = d / "task.yaml"
    f.write_text(yaml.safe_dump({"id": tid, "title": title, "status": "ready"}), encoding="utf-8")
    return f


def test_load_all_reparses_a_changed_record_and_hands_out_copies(tmp_path: Path):
    f = _record(tmp_path / "tasks" / "T-0001-a", "T-0001", "first")
    s = store_mod.Store(tmp_path)
    a = s.load_all()["T-0001"].rec
    a["title"] = "mutated by a caller"
    assert s.load_all()["T-0001"].rec["title"] == "first"            # the cache is not shared with callers
    tmp = f.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump({"id": "T-0001", "title": "secnd", "status": "ready"}), encoding="utf-8")
    tmp.replace(f)                                                   # how Task.save writes: same size, new file
    assert s.load_all()["T-0001"].rec["title"] == "secnd"


def test_load_all_parses_an_unchanged_record_once(tmp_path: Path, monkeypatch):
    _record(tmp_path / "tasks" / "T-0001-a", "T-0001", "first")
    calls = []
    real = yaml.load
    monkeypatch.setattr(store_mod.yaml, "load", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    s = store_mod.Store(tmp_path)
    for _ in range(5):
        s.load_all()
    assert len(calls) <= 1


def test_no_dirty_worktrees_is_not_done_when_the_workload_repo_is_missing(tmp_path: Path):
    from scr import chores
    from _paths import CONFIG
    import shutil
    shutil.copytree(CONFIG, tmp_path / "config")          # the first workload's repo (../quantum-insertion) is absent
    (tmp_path / "tasks").mkdir()
    assert chores.check(tmp_path, {"no_dirty_worktrees": True}) is False


def test_event_log_read_reparses_after_append_and_hands_out_copies(tmp_path: Path):
    from scr.events import EventLog
    log = EventLog(tmp_path / "events.jsonl")
    log.append("system", name="genesis", actor="t", note="n")
    a = log.read()
    a[0]["note"] = "mutated by a caller"
    assert log.read()[0]["note"] == "n"
    log.append("system", name="second", actor="t", note="m")
    assert [e["name"] for e in log.read()] == ["genesis", "second"]


def test_ui_verify_is_reused_until_the_state_changes(tmp_path: Path, monkeypatch):
    from scr import ui
    from scr.events import EventLog
    (tmp_path / "tasks").mkdir()
    log = EventLog(tmp_path / "events" / "events.jsonl")
    log.path.parent.mkdir()
    log.append("system", name="genesis", actor="t", note="n")
    runs = []
    monkeypatch.setattr(ui.State, "verify", lambda self: runs.append(1) or {"errors": [], "warnings": []})
    import scr.cli as cli
    monkeypatch.setattr(cli, "refresh", lambda root, quiet=True: None)
    ui._VERIFIED.clear()
    ui._verified(tmp_path); ui._verified(tmp_path)
    assert len(runs) == 1                                  # unchanged state: verified once
    log.append("system", name="later", actor="t", note="m")
    ui._verified(tmp_path)
    assert len(runs) == 2                                  # an event appended: verified again


def test_watcher_running_never_blocks_a_request_on_the_process_listing(monkeypatch):
    import threading
    import scr.run as run_mod
    from scr import ui
    gate = threading.Event()
    monkeypatch.setattr(run_mod, "pids_matching", lambda pat: gate.wait(5) and [123])
    ui._WATCHER.update(value=None, at=0.0, busy=False)
    assert ui.watcher_running() is None                    # returns at once; the listing runs in the background
    gate.set()
    for _ in range(100):
        if ui._WATCHER["value"] is not None:
            break
        threading.Event().wait(0.02)
    assert ui.watcher_running() is True                    # the next request sees the answer
    ui._WATCHER.update(value=None, at=0.0, busy=False)
