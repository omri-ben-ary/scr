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
