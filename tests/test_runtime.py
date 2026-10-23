"""The release runtime (scr/runtime.py): `scr version`, `scr upgrade`, `scr rollback`, `scr release`, and the
read-only `scr watch --dry-run` smoke tick. Every external effect is faked through runtime.Ops."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scr import runtime
from scr.events import EventLog
from scr.run import scr_display, scr_exe
from test_journal_ops import git, scr, world  # noqa: F401  (the shared fixture)

posix_only = pytest.mark.skipif(os.name == "nt", reason="the checkout-venv fallback written here is the POSIX bin/scr")


class FakeOps(runtime.Ops):
    """Records every effect; `fail` maps a step name to the rc that step returns."""

    def __init__(self, fail: dict[str, int] | None = None):
        self.calls: list[tuple] = []
        self.fail = fail or {}

    def install(self, rt, tag, repo_url):
        self.calls.append(("install", tag, repo_url))
        if self.fail.get("install"):
            return False, "pip: no such tag"
        exe = runtime.venv_bin(rt / tag)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text(f"#!/bin/sh\necho {tag}\n", encoding="utf-8")
        return True, ""

    def run(self, argv, cwd, timeout=900):
        rel = Path(argv[0]).parent.parent.name                    # the release folder the exe lives in
        step = "migrate" if "migrate" in argv else ("verify" if "verify" in argv else "watch")
        phase = "post" if any(c[0] == "point" for c in self.calls) else "pre"
        self.calls.append(("run", rel, step, phase))
        rc = self.fail.get(f"{phase}-{step}", 0)
        return rc, f"{step} {'FAILED' if rc else 'ok'}"

    def stop_services(self, root):
        self.calls.append(("stop",))
        return ["stopped ui", "stopped watch"]

    def start_services(self, root):
        self.calls.append(("start", runtime.current_target(root).name if runtime.current_target(root) else "checkout"))
        return ["started ui", "started watch"]

    def point_current(self, root, target):
        self.calls.append(("point", target.name))
        super().point_current(root, target)


@pytest.fixture
def rt(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "scr-runtime"
    monkeypatch.setenv("SCR_RUNTIME", str(d))
    return d


def _events(root: Path, name: str) -> list[dict]:
    return [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "system" and e.get("name") == name]


# ---- layout ------------------------------------------------------------------------------------------------
def test_runtime_dir_defaults_beside_the_state_root(tmp_path, monkeypatch):
    monkeypatch.delenv("SCR_RUNTIME", raising=False)
    assert runtime.runtime_dir(tmp_path / "scriptorium") == tmp_path.resolve() / "scr-runtime"
    monkeypatch.setenv("SCR_RUNTIME", str(tmp_path / "elsewhere"))
    assert runtime.runtime_dir(tmp_path / "scriptorium") == tmp_path / "elsewhere"


@posix_only
def test_scr_exe_prefers_the_live_release_and_falls_back_to_the_checkout_venv(world, rt):
    root, _, _ = world
    assert scr_display(root) == "scr" and scr_exe(root)[1:] == ["-m", "scr"]      # neither exists: python -m scr
    venv_exe = root / ".venv" / "bin" / "scr"
    venv_exe.parent.mkdir(parents=True); venv_exe.write_text("", encoding="utf-8")
    assert scr_exe(root) == [str(venv_exe)]                                         # before the cut-over
    ops = FakeOps()
    ops.install(rt, "v0.2.0", "x")
    ops.point_current(root, rt / "v0.2.0")
    assert scr_exe(root) == [str(rt / "current" / "bin" / "scr")]                   # after it: the release, via `current`
    assert scr_display(root) == str(rt / "current" / "bin" / "scr")


# ---- upgrade -----------------------------------------------------------------------------------------------
def test_upgrade_installs_smokes_stops_migrates_switches_restarts_and_smokes_again(world, rt):
    root, _, _ = world
    ops = FakeOps()
    rc, out = runtime.upgrade(root, "v0.2.0", actor="omri", ops=ops)
    assert rc == 0 and out[-1] == "v0.2.0 is live", out
    assert ops.calls == [("install", "v0.2.0", runtime.REPO_URL),
                         ("run", "v0.2.0", "verify", "pre"), ("run", "v0.2.0", "watch", "pre"),
                         ("stop",), ("run", "v0.2.0", "migrate", "pre"),
                         ("point", "v0.2.0"), ("start", "v0.2.0"),
                         ("run", "current", "verify", "post"), ("run", "current", "watch", "post")]
    assert runtime.current_target(root).name == "v0.2.0" and runtime.releases(root) == ["v0.2.0"]
    assert [e["data"]["tag"] for e in _events(root, "upgraded")] == ["v0.2.0"]
    assert [e["data"]["step"] for e in _events(root, "upgrade_step")] == ["install", "smoke", "stop"]
    assert runtime.history(root)[-1] | {"ts": None} == {"ts": None, "op": "upgrade", "from": None, "to": "v0.2.0", "actor": "omri"}
    assert not EventLog(root / "events" / "events.jsonl").verify_chain()


def test_a_failed_pre_switch_smoke_changes_nothing(world, rt):
    root, _, _ = world
    ops = FakeOps(fail={"pre-watch": 1})
    rc, out = runtime.upgrade(root, "v0.2.0", ops=ops)
    assert rc == 1 and any("FAILED at pre-switch smoke" in l for l in out), out
    assert not any(c[0] in ("stop", "point", "start") for c in ops.calls)        # the live system was never touched
    assert runtime.current_target(root) is None
    ev = _events(root, "upgrade_failed")
    assert len(ev) == 1 and ev[0]["data"]["step"] == "pre-switch smoke"


def test_a_failed_migration_restarts_the_old_release(world, rt):
    root, _, _ = world
    ops = FakeOps()
    runtime.upgrade(root, "v0.2.0", ops=ops)
    ops = FakeOps(fail={"pre-migrate": 2})
    rc, out = runtime.upgrade(root, "v0.2.1", ops=ops)
    assert rc == 1 and runtime.current_target(root).name == "v0.2.0"
    assert ("stop",) in ops.calls and ops.calls[-1] == ("start", "v0.2.0")
    assert _events(root, "upgrade_failed")[-1]["data"] == {"tag": "v0.2.1", "step": "migrate", "back_to": "v0.2.0"}


def test_a_failed_post_switch_smoke_rolls_back_to_the_previous_release(world, rt):
    root, _, _ = world
    runtime.upgrade(root, "v0.2.0", ops=FakeOps())
    ops = FakeOps(fail={"post-verify": 1})
    rc, out = runtime.upgrade(root, "v0.2.1", ops=ops)
    assert rc == 1 and "current -> v0.2.0 again" in out, out
    assert runtime.current_target(root).name == "v0.2.0"
    assert [c for c in ops.calls if c[0] == "point"] == [("point", "v0.2.1"), ("point", "v0.2.0")]
    assert ops.calls[-1] == ("start", "v0.2.0")
    assert [h["op"] for h in runtime.history(root)] == ["upgrade", "upgrade", "auto-rollback"]
    assert len(_events(root, "upgrade_failed")) == 1 and not _events(root, "upgraded")[1:]


def test_a_failed_first_upgrade_puts_the_checkout_venv_back(world, rt):
    root, _, _ = world
    ops = FakeOps(fail={"post-watch": 1})
    rc, out = runtime.upgrade(root, "v0.2.0", ops=ops)
    assert rc == 1 and "current removed: the checkout .venv is live again" in out, out
    assert runtime.current_target(root) is None and not (rt / "current").is_symlink()
    assert ops.calls[-1] == ("start", "checkout")


def test_upgrade_refuses_a_non_tag_and_skips_the_live_one(world, rt):
    root, _, _ = world
    assert runtime.upgrade(root, "main", ops=FakeOps())[0] == 2
    runtime.upgrade(root, "v0.2.0", ops=FakeOps())
    ops = FakeOps()
    assert runtime.upgrade(root, "v0.2.0", ops=ops) == (0, [runtime.upgrade(root, "v0.2.0", ops=FakeOps())[1][0], "v0.2.0 is already live"])
    assert ops.calls == []


def test_upgrade_failed_is_a_needs_you_card(world, rt):
    from scr import ui as ui_mod
    root, _, _ = world
    runtime.upgrade(root, "v0.2.0", ops=FakeOps(fail={"install": 1}))
    cards = [c for c in ui_mod.needs_cards(root) if c.get("name") == "upgrade_failed"]
    assert len(cards) == 1 and "previous release" in cards[0]["title"]


# ---- rollback ----------------------------------------------------------------------------------------------
def test_rollback_returns_to_the_release_live_before_the_last_switch(world, rt):
    root, _, _ = world
    runtime.upgrade(root, "v0.2.0", ops=FakeOps())
    runtime.upgrade(root, "v0.2.1", ops=FakeOps())
    ops = FakeOps()
    rc, out = runtime.rollback(root, actor="omri", ops=ops)
    assert rc == 0 and runtime.current_target(root).name == "v0.2.0"
    assert ops.calls == [("stop",), ("point", "v0.2.0"), ("start", "v0.2.0")]
    assert _events(root, "rolled_back")[-1]["data"] == {"tag": "v0.2.0", "previous": "v0.2.1"}


def test_rollback_with_nothing_earlier_refuses(world, rt):
    root, _, _ = world
    assert runtime.rollback(root, ops=FakeOps())[0] == 2                          # no runtime at all
    runtime.upgrade(root, "v0.2.0", ops=FakeOps())
    rc, out = runtime.rollback(root, ops=FakeOps())
    assert rc == 2 and "no earlier installed release" in out[0]


# ---- version -----------------------------------------------------------------------------------------------
def test_version_lists_the_releases_and_marks_the_live_one(world, rt):
    root, _, _ = world
    runtime.upgrade(root, "v0.2.0", ops=FakeOps())
    runtime.upgrade(root, "v0.10.0", ops=FakeOps())
    out = scr(root, "version", journal_on=False).stdout
    assert "code schema version 1" in out and "state schema version 1" in out
    assert "v0.2.0, v0.10.0 (live)" in out                                          # numeric order, not text order


# ---- release -----------------------------------------------------------------------------------------------
def _code_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "code"; repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n"); git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "init")
    for n, title in ((5, "Package assets"), (6, "Known issues")):
        git(repo, "checkout", "-q", "-b", f"f{n}"); (repo / f"f{n}.txt").write_text("x\n")
        git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", title); git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", f"f{n}", "-m", f"Merge pull request #{n} from x/f{n}\n\n{title}")
    return repo


def test_release_tags_the_next_minor_with_the_merged_prs(tmp_path):
    repo = _code_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    rc, out = runtime.release(repo, "minor")
    assert rc == 0 and out[-1] == "  git push origin v0.2.0", out
    assert git(repo, "rev-parse", "HEAD") == head                                   # nothing committed: main stays PR-only
    notes = git(repo, "tag", "-l", "--format=%(contents)", "v0.2.0")
    assert "- #5 Package assets" in notes and "- #6 Known issues" in notes
    (repo / "b.txt").write_text("b\n"); git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "fix")
    rc, out = runtime.release(repo, "patch")
    assert rc == 0 and "v0.2.1" in out[0] and "no merged PRs since the last tag" in out[0]


def test_release_refuses_a_dirty_tree(tmp_path):
    repo = _code_repo(tmp_path)
    (repo / "a.txt").write_text("changed\n")
    assert runtime.release(repo)[0] == 2


# ---- the dry tick ------------------------------------------------------------------------------------------
def test_watch_dry_run_ticks_a_copy_and_leaves_the_live_state_alone(world):
    root, _, _ = world
    head, n = git(root, "rev-parse", "HEAD"), len(EventLog(root / "events" / "events.jsonl").read())
    status = git(root, "status", "--porcelain")
    r = scr(root, "watch", "--dry-run", journal_on=False, check=False)
    assert r.returncode == 0 and "dry tick: ok" in r.stdout, r.stdout + r.stderr
    assert git(root, "rev-parse", "HEAD") == head and git(root, "status", "--porcelain") == status
    assert len(EventLog(root / "events" / "events.jsonl").read()) == n
    assert not (root / "reports" / "steward_watch.log").exists() or "dry" not in (root / "reports" / "steward_watch.log").read_text(encoding="utf-8")


def test_watch_dry_run_fails_when_the_tick_crashes(world, monkeypatch):
    import scr.cli as cli
    import scr.watch as watch_mod
    root, _, _ = world

    def boom(self):
        raise RuntimeError("tick broke")
    monkeypatch.setattr(watch_mod.Watcher, "tick", boom)

    class A:
        pass
    assert cli._watch_dry_run(root, A()) == 1


def test_launch_events_record_the_running_release(world, monkeypatch):
    from scr.run import record_launch
    root, _, _ = world
    record_launch(root, "scriber", "T-0001", None, "sid-1", root)
    ev = [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("name") == "launch"][-1]
    assert ev["data"]["scr_version"] == runtime.installed_version()[0]


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="directory junctions (mklink /J)")
def test_current_is_a_junction_that_switches_and_rolls_back(world, rt):
    """The real point_current on Windows: `current` is a junction (not a copy), realpath follows it, a switch
    replaces it without touching the release behind it, and scr_exe resolves current\\Scripts\\scr.exe."""
    root, _, _ = world
    runtime.upgrade(root, "v0.2.0", ops=FakeOps())
    runtime.upgrade(root, "v0.2.1", ops=FakeOps())
    link = rt / "current"
    assert runtime.current_target(root).name == "v0.2.1" and link.is_dir()
    assert (rt / "v0.2.0" / "Scripts" / "scr.exe").exists()                      # the old release is intact
    assert scr_exe(root) == [str(link / "Scripts" / "scr.exe")]
    assert runtime.rollback(root, ops=FakeOps())[0] == 0
    assert runtime.current_target(root).name == "v0.2.0" and (rt / "v0.2.1" / "Scripts" / "scr.exe").exists()
