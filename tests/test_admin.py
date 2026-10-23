"""scr admin stop reports what the kill actually did (T-0148)."""
from pathlib import Path

import scr.admin as admin


def _stub(monkeypatch, pids, killed):
    calls = []
    monkeypatch.setattr(admin, "pids_matching", lambda pat: list(pids))
    monkeypatch.setattr(admin, "kill", lambda pid: calls.append(pid) or killed)
    return calls


def test_admin_stop_reports_a_failed_kill(monkeypatch):
    calls = _stub(monkeypatch, [111], False)
    lines = admin.stop(Path("."), "watch")
    assert calls == [111]
    assert any("FAILED" in l and "111" in l for l in lines)
    assert not any(l.startswith("stopped") for l in lines)


def test_admin_stop_reports_a_made_stop(monkeypatch):
    calls = _stub(monkeypatch, [111], True)
    assert admin.stop(Path("."), "watch") == ["stopped watch pid 111"]
    assert calls == [111]


def test_admin_stop_with_no_pids_kills_nothing(monkeypatch):
    calls = _stub(monkeypatch, [], True)
    assert admin.stop(Path("."), "watch") == ["no watch running"]
    assert calls == []


class _Timer:
    started = []

    def __init__(self, delay, fn):
        self.fn = fn

    def start(self):
        _Timer.started.append(self.fn)


def _stub_quit(monkeypatch, lines):
    import scr.ui as ui
    _Timer.started = []
    monkeypatch.setattr(admin, "stop", lambda root, what: list(lines))
    monkeypatch.setattr(ui.threading, "Timer", _Timer)
    monkeypatch.setattr(ui.os, "_exit", lambda code: (_ for _ in ()).throw(AssertionError("os._exit called")))
    return ui


def test_ui_quit_keeps_ui_up_when_watch_stop_failed(monkeypatch):
    failed = "FAILED to stop watch pid 111 (it may still be running; stop it by hand)"
    ui = _stub_quit(monkeypatch, [failed])
    res = ui._quit(Path("."))
    assert res["ok"] is False
    assert _Timer.started == []
    assert failed in res["output"]
    assert "UI stays up" in res["output"] and "scr admin stop watch" in res["output"]


def test_ui_quit_exits_when_watch_stopped(monkeypatch):
    ui = _stub_quit(monkeypatch, ["stopped watch pid 111"])
    res = ui._quit(Path("."))
    assert res["ok"] is True
    assert len(_Timer.started) == 1
    assert "UI shutting down" in res["output"]


# ---- T-0156: one FAILED contract, shared by ui._quit and `scr up --stop` ----------------

def test_stop_failed_reads_the_stop_lines():
    assert admin.stop_failed(["stopped ui pid 1", "FAILED to stop watch pid 111 (it may still be running; stop it by hand)"])
    assert not admin.stop_failed(["stopped ui pid 1", "stopped watch pid 111"])
    assert not admin.stop_failed(["no ui running", "no watch running"])
    assert not admin.stop_failed([])


def _up_stop(monkeypatch, capsys, lines_for, root):
    import argparse
    import pytest
    import scr.cli as cli
    monkeypatch.setattr(admin, "stop", lambda root, what: list(lines_for[what]))
    for d in ("tasks", "config"):                     # a minimal state root: the code repo is not one any more
        (root / d).mkdir(exist_ok=True)
    args = argparse.Namespace(stop=True, root=str(root), actor="omri")
    with pytest.raises(SystemExit) as e:
        cli.cmd_up(args)
    return e.value.code, capsys.readouterr().out


def test_up_stop_exits_nonzero_when_a_stop_failed(monkeypatch, capsys, tmp_path):
    failed = "FAILED to stop watch pid 111 (it may still be running; stop it by hand)"
    code, out = _up_stop(monkeypatch, capsys, {"ui": ["stopped ui pid 222"], "watch": [failed]}, tmp_path)
    assert code == 1
    assert "stopped ui pid 222" in out and failed in out


def test_up_stop_exits_zero_when_every_stop_was_made(monkeypatch, capsys, tmp_path):
    code, out = _up_stop(monkeypatch, capsys, {"ui": ["stopped ui pid 222"], "watch": ["no watch running"]}, tmp_path)
    assert code == 0
    assert "stopped ui pid 222" in out and "no watch running" in out
