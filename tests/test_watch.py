"""T-0077: every Claude session the operation starts runs in one permission mode, the config default
(`auto`), and a session started in a worktree gets `--add-dir <scriptorium root>`. Covers the five headless
call sites (scriber launch, start_review, start_tooling_review, intake(), the watcher's auto-dispatch launch)
and the four Discuss item kinds (plan, ticket, alarm/event, general). No real claude is ever started: the
binary lookup and the process spawn are replaced and the argv recorded."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scr.ops import spawn
from scr.store import Store
from test_journal_ops import REPO, accepted_review, world, scr  # noqa: F401  (the shared fixture: scriptorium + workload repo + origin)

FAKE_CLAUDE = str(Path(sys.executable).with_name("claude.exe"))   # a .exe path: claude_argv must not wrap it in `cmd /c`


class _Proc:
    pid = 4242
    returncode = 0
    stdout = ""
    stderr = ""


class _Shim:
    """Stands in for a module imported by ONE scr module: the named attributes are fakes, everything else
    is the real module. (Setting `subprocess.Popen` itself would also break the scr subprocesses the
    watcher runs through subprocess.run.)"""
    def __init__(self, real, **fakes):
        self._real, self._fakes = real, fakes

    def __getattr__(self, name):
        if name in self._fakes:
            return self._fakes[name]
        return getattr(self._real, name)


def _record_launches(monkeypatch) -> list[dict]:
    """Replace the claude lookup and the two spawn paths (Popen in scr.run for detached sessions,
    subprocess.run in scr.launch for the synchronous Intake); return the list that collects the calls."""
    import scr.launch as launch_mod
    import scr.run as run_mod
    calls: list[dict] = []
    monkeypatch.setattr(run_mod, "claude_bin", lambda: FAKE_CLAUDE)

    def fake_popen(args, cwd=None, **kw):
        for k in ("stdin", "stdout", "stderr"):
            f = kw.get(k)
            if hasattr(f, "close"):
                f.close()
        calls.append({"argv": list(args), "cwd": Path(cwd)})
        return _Proc()

    def fake_run(args, cwd=None, **kw):
        calls.append({"argv": list(args), "cwd": Path(cwd)})
        return _Proc()

    monkeypatch.setattr(run_mod, "subprocess", _Shim(subprocess, Popen=fake_popen))
    monkeypatch.setattr(launch_mod, "subprocess", _Shim(subprocess, run=fake_run))
    return calls


def _mode(argv: list[str]) -> str:
    return argv[argv.index("--permission-mode") + 1]


def _add_dirs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]


def _set_limit(root: Path, key: str, value) -> None:
    p = root / "config" / "limits.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg[key] = value
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _five_call_sites(world, monkeypatch) -> dict[str, dict]:
    """Drive each headless launch path once and return {site: {"argv", "cwd"}}."""
    from scr.launch import intake, launch
    from scr.watch import Watcher
    root, repo, _ = world
    calls = _record_launches(monkeypatch)
    out: dict[str, dict] = {}

    # (5) watcher auto-dispatch: `scr dispatch` picks the ready T-0001, then launch() starts its scriber
    Watcher(root, quiet=True, auto_dispatch=True).tick()
    assert calls, "auto-dispatch started no session"
    out["watch_dispatch"] = calls.pop()
    assert Store(root).get("T-0001").status == "in_progress"
    wt = repo / ".worktrees" / "T-0001"
    assert wt.exists()

    # (1) the scriber launcher on its own (relaunch of the running task)
    launch(root, ["T-0001"], mode="headless", relaunch=True)
    out["launch"] = calls.pop()

    # (2) a Steward review of a submitted workload task, in its worktree
    scr(root, "--actor", "s", "task", "set", "T-0001", "submitted")
    w = Watcher(root, quiet=True, auto_review=True)
    w.start_review(Store(root).get("T-0001"))
    out["start_review"] = calls.pop()

    # (3) a tooling review runs in the checkout itself
    w.start_tooling_review(Store(root).get("T-0001"))
    out["start_tooling_review"] = calls.pop()

    # (4) Intake, synchronous, in the root
    intake(root)
    out["intake"] = calls.pop()
    assert not calls, [c["argv"] for c in calls]
    return out


def test_headless_sessions_default_to_auto_mode(world, monkeypatch):
    root, _, _ = world
    sites = _five_call_sites(world, monkeypatch)
    assert set(sites) == {"launch", "start_review", "start_tooling_review", "intake", "watch_dispatch"}
    for name, c in sites.items():
        assert c["argv"][0] == FAKE_CLAUDE and c["argv"][1] == "-p", (name, c["argv"])
        assert _mode(c["argv"]) == "auto", (name, c["argv"])
        assert "--output-format" in c["argv"], (name, c["argv"])


def test_config_permission_mode_reaches_every_call_site(world, monkeypatch):
    root, _, _ = world
    _set_limit(root, "claude_permission_mode", "bypassPermissions")
    sites = _five_call_sites(world, monkeypatch)
    for name, c in sites.items():
        assert _mode(c["argv"]) == "bypassPermissions", (name, c["argv"])
    # the launcher's log line names the mode the session really got
    log = (Store(root).get("T-0001").dir / "log.md").read_text(encoding="utf-8")
    assert "(permission-mode bypassPermissions)" in log and "acceptEdits" not in log


def test_sessions_in_a_worktree_get_add_dir_root(world, monkeypatch):
    root, repo, _ = world
    sites = _five_call_sites(world, monkeypatch)
    wt = (repo / ".worktrees" / "T-0001").resolve()
    for name in ("launch", "start_review", "watch_dispatch"):          # cwd = the task worktree
        c = sites[name]
        assert c["cwd"].resolve() == wt, (name, c["cwd"])
        assert _add_dirs(c["argv"]) == [str(root)], (name, c["argv"])
        assert (Path(_add_dirs(c["argv"])[0]) / "tasks").is_dir() and (Path(_add_dirs(c["argv"])[0]) / "agents" / "steward.md").exists()
    for name in ("start_tooling_review", "intake"):                    # cwd = the scriptorium root: nothing to add
        c = sites[name]
        assert c["cwd"].resolve() == root.resolve(), (name, c["cwd"])
        assert _add_dirs(c["argv"]) == [], (name, c["argv"])


def test_explicit_override_beats_the_config_default(world, monkeypatch):
    """The Watcher keeps a permission_mode parameter (tests, `scr watch --permission-mode`); a value there
    wins over limits.yaml, None means the config default."""
    from scr.run import DEFAULT_PERMISSION_MODE, permission_mode_for, session_argv
    from scr.watch import Watcher
    root, _, _ = world
    assert DEFAULT_PERMISSION_MODE == "auto" and permission_mode_for(root) == "auto" and permission_mode_for(None) == "auto"
    _set_limit(root, "claude_permission_mode", "acceptEdits")
    assert permission_mode_for(root) == "acceptEdits"
    calls = _record_launches(monkeypatch)
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched"); spawn(root, ["T-0001"], lake=False)
    for st in ("in_progress", "submitted"):
        scr(root, "--actor", "s", "task", "set", "T-0001", st)
    Watcher(root, quiet=True, permission_mode="plan").start_review(Store(root).get("T-0001"))
    assert _mode(calls[-1]["argv"]) == "plan"
    Watcher(root, quiet=True).start_review(Store(root).get("T-0001"))
    assert _mode(calls[-1]["argv"]) == "acceptEdits"
    assert _mode(session_argv(root, root=root, permission_mode="dontAsk")) == "dontAsk"


def _discuss_lines(root: Path, items: list[str], monkeypatch) -> dict[str, str]:
    """Run discuss() for each item without opening a terminal or touching ~/.claude; return the claude
    command line it built (the .launch.ps1's last line on Windows, the printed line elsewhere)."""
    import scr.discuss as d
    lines: dict[str, str] = {}
    popen: list[list[str]] = []
    monkeypatch.setattr(d, "claude_bin", lambda: FAKE_CLAUDE)
    monkeypatch.setattr(d, "install_agents", lambda root, agents_dir=None: [])
    monkeypatch.setattr(d, "shutil", _Shim(shutil, which=lambda name: None))      # no wt, no x-terminal: powershell file / printed line
    monkeypatch.setattr(d, "subprocess", _Shim(subprocess, Popen=lambda args, **kw: popen.append(list(args)) or _Proc()))
    for item in items:
        out = d.discuss(root, item)
        if os.name == "nt":
            ps1 = Path(popen[-1][popen[-1].index("-File") + 1])
            lines[item] = [l for l in ps1.read_text(encoding="utf-8-sig").splitlines() if l.startswith("claude ")][-1]
        else:
            lines[item] = next(l for l in out if l.startswith("no terminal emulator found; run: "))
    return lines


DISCUSS_ITEMS = ["plan", "T-0001", "events:T-0001:stale:2099-01-01T00:00:00", "general"]


def test_discuss_sessions_default_to_auto_mode(world, monkeypatch):
    root, repo, _ = world
    lines = _discuss_lines(root, DISCUSS_ITEMS, monkeypatch)
    roles = {"plan": "intake", "T-0001": "steward", DISCUSS_ITEMS[2]: "steward", "general": "operator"}
    for item, line in lines.items():
        assert "--permission-mode auto " in line, (item, line)
        assert f"--agent scriptorium-{roles[item]} " in line, (item, line)
        assert "--add-dir" in line and str(root) in line and str(repo) in line, (item, line)
        assert "acceptEdits" not in line and "bypassPermissions" not in line


def test_discuss_sessions_take_the_config_override(world, monkeypatch):
    root, _, _ = world
    _set_limit(root, "claude_permission_mode", "bypassPermissions")
    for item, line in _discuss_lines(root, DISCUSS_ITEMS, monkeypatch).items():
        assert "--permission-mode bypassPermissions " in line, (item, line)


def test_no_other_permission_mode_is_hard_coded():
    """The DoD grep: `acceptEdits` / `bypassPermissions` appear in scr/ only where the default is defined and documented."""
    src = Path(__file__).resolve().parents[1] / "scr"
    hits = [(p.name, i) for p in src.glob("*.py") for i, l in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if "acceptEdits" in l or "bypassPermissions" in l]
    assert hits and {h[0] for h in hits} == {"run.py"}, hits


# ---- T-0078: a spawned scriptorium task lands (commit, merge, tests, merged) ----------------------------
# The checkout is its own workload (test_scriptorium.self_world: repo `.`, linked venv, a bare origin). The
# workload's `tests:` command is a one-line script that passes iff tool.txt (the file the task edits) says
# "ok": whether the tests pass is then a property of the tree being landed, exactly as in the real case.
from test_scriptorium import git as sgit, scr as sscr, self_world  # noqa: E402

CHECK = "import sys; sys.exit(0 if open('tool.txt', encoding='utf-8').read().strip() == 'ok' else 1)\n"


def _events(root: Path, name: str, task: str) -> list[dict]:
    from scr.events import EventLog
    return [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "system" and e.get("name") == name and e.get("task") == task]


def _land_world(tmp_path: Path, monkeypatch, journal: bool = False) -> tuple[Path, Path, Path]:
    """self_world on `main` with T-0007 (workload scriptorium, scope tool.txt) spawned into
    ../scriptorium-worktrees/T-0007 and then accepted. Returns (root, origin, worktree)."""
    if journal:
        monkeypatch.delenv("SCR_NO_JOURNAL", raising=False)
    else:
        monkeypatch.setenv("SCR_NO_JOURNAL", "1")
    root, origin = self_world(tmp_path)
    sscr(root, "--actor", "omri", "ensure-main", "--push")
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text(encoding="utf-8"))
    wl["scriptorium"]["tests"] = f"{sys.executable} check.py"
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl), encoding="utf-8")
    (root / "check.py").write_text(CHECK, encoding="utf-8")
    (root / "tool.txt").write_text("base\n", encoding="utf-8")
    sscr(root, "--quiet", "task", "new", "Tooling", "--id", "T-0007", "--workload", "scriptorium", "--files", "tool.txt", "--origin", "omri")
    for st in ("triaged", "ready", "dispatched"):
        sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0007", st)
    sgit(root, "add", "-A"); sgit(root, "commit", "-q", "-m", "[journal] record, tool, check"); sgit(root, "push", "-q", "origin", "main")
    spawn(root, ["T-0007"])
    wt = tmp_path / "scriptorium-worktrees" / "T-0007"
    assert wt.is_dir() and sgit(wt, "rev-parse", "--abbrev-ref", "HEAD") == "task/T-0007"
    for st in ("in_progress", "submitted"):
        sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0007", st)
    (Store(root).get("T-0007").dir / "review.md").write_text(accepted_review(), encoding="utf-8")   # T-0081: the review comes first
    sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0007", "accepted")
    sgit(root, "add", "-A"); sgit(root, "commit", "-q", "-m", "[journal] accepted")     # the trunk moves after the spawn, as it always does
    return root, origin, wt


def _tick(root: Path) -> dict:
    from scr.watch import Watcher
    return Watcher(root, quiet=True, auto_land=True).tick()


def test_accepted_scriptorium_worktree_is_committed_merged_and_tested(tmp_path: Path, monkeypatch):
    """DoD 1: an accepted scriptorium task with a spawned worktree holding an UNCOMMITTED change; after the
    tick that change is a commit reachable from the trunk, the task is merged, and log.md names the commit.
    Journal on: the real tick (journal, verify, land, journal) against a checkout that commits itself."""
    root, origin, wt = _land_world(tmp_path, monkeypatch, journal=True)
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")                                   # the scriber's work, never committed
    main_before = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"], res
    t = Store(root).get("T-0007")
    assert t.status == "merged", (t.status, res["needs"])
    # the change is on the trunk: a commit reachable from main, and main's tree has it
    assert sgit(root, "show", "main:tool.txt").strip() == "ok"
    change = sgit(root, "log", "-1", "--format=%H", "main", "--", "tool.txt")
    assert change and sgit(root, "merge-base", "--is-ancestor", change, "main") == ""
    assert sgit(root, "merge-base", "--is-ancestor", "task/T-0007", "main") == ""
    assert sgit(root, "rev-parse", "main") != main_before
    assert sgit(root, "rev-parse", "--abbrev-ref", "HEAD") == "main"                          # the checkout stays on the trunk
    assert not (root / ".git" / "MERGE_HEAD").exists()
    # the record names the trunk commit: the state event, the landed event, and log.md
    ev = [e for e in _events(root, "landed", "T-0007")]
    assert len(ev) == 1 and "task/T-0007 -> main @" in ev[0]["note"] and "tests pass" in ev[0]["note"], ev
    sha = ev[0]["note"].split("@")[1].split()[0]
    assert sgit(root, "merge-base", "--is-ancestor", sha, "main") == ""
    assert sgit(root, "show", f"{sha}:tool.txt").strip() == "ok"
    log = (t.dir / "log.md").read_text(encoding="utf-8")
    assert f"landed: task/T-0007 -> main @ {sha}" in log, log
    assert not _events(root, "land_failed", "T-0007") and not res["needs"], res["needs"]
    # merged is journaled (the tick commits the record) and pushed; the worktree is retired (quarantined, not deleted)
    assert sgit(root, "status", "--porcelain") == ""
    assert sgit(root, "rev-parse", "main") == sgit(origin, "rev-parse", "main")
    assert not wt.exists() and list((tmp_path / "scriptorium-RETIRED").glob("*-T-0007/worktree/tool.txt"))


def test_accepted_hotfix_without_worktree_is_merged_without_a_merge(tmp_path: Path, monkeypatch):
    """DoD 2: the hotfix path is unchanged — a scriptorium task with no worktree is merged directly, the
    trunk untouched, with the note that says why."""
    root, _, _ = _land_world(tmp_path, monkeypatch)
    sscr(root, "--quiet", "task", "new", "Hotfix", "--id", "T-0008", "--workload", "scriptorium", "--files", "scr/x.py", "--origin", "omri")
    for st in ("triaged", "ready", "dispatched", "in_progress", "submitted"):
        sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0008", st)
    (Store(root).get("T-0008").dir / "review.md").write_text(accepted_review(), encoding="utf-8")   # T-0081
    sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0008", "accepted")
    (tmp_path / "scriptorium-worktrees" / "T-0007").rename(tmp_path / "T-0007-parked")   # keep this tick to the hotfix
    sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0007", "returned")
    main_before = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"] and Store(root).get("T-0008").status == "merged", res
    assert sgit(root, "rev-parse", "main") == main_before and not _events(root, "landed", "T-0008")
    from scr.events import EventLog
    st = [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "state" and e.get("task") == "T-0008" and e.get("to") == "merged"]
    assert len(st) == 1 and "already on main (journal)" in st[0]["note"], st


def test_merge_conflict_is_not_merged_and_raises_land_failed(tmp_path: Path, monkeypatch):
    """DoD 3a: the trunk changed the same line the worktree changed. The task stays accepted, one
    land_failed event names the file, the NEEDS-YOU line is there every tick, the worktree is left clean
    (merge aborted), the trunk untouched; the attempt is not repeated while the branch tip is the same, and
    is repeated — and lands — once the branch is fixed."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (root / "tool.txt").write_text("trunk edit\n", encoding="utf-8")
    sgit(root, "add", "-A"); sgit(root, "commit", "-q", "-m", "[journal] the trunk moved on tool.txt")
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    main_before = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"], res
    assert Store(root).get("T-0007").status == "accepted"
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and "conflict" in ev[0]["note"] and "tool.txt" in ev[0]["note"], ev
    assert any(n.startswith("T-0007 landing failed (conflict)") for n in res["needs"]), res["needs"]
    assert sgit(root, "rev-parse", "main") == main_before and sgit(root, "show", "main:tool.txt").strip() == "trunk edit"
    assert not (root / ".git" / "worktrees" / "T-0007" / "MERGE_HEAD").exists()
    assert sgit(wt, "status", "--porcelain") == ""                                            # the work was committed on task/T-0007 ...
    assert sgit(wt, "show", "HEAD:tool.txt").strip() == "ok" and not _events(root, "landed", "T-0007")
    tip = sgit(wt, "rev-parse", "HEAD")
    # second tick: same tip -> no second attempt, no second event, still a NEEDS-YOU line
    res2 = _tick(root)
    assert Store(root).get("T-0007").status == "accepted" and len(_events(root, "land_failed", "T-0007")) == 1
    assert any("landing failed at " + tip[:8] in n for n in res2["needs"]), res2["needs"]
    assert sgit(wt, "rev-parse", "HEAD") == tip
    # the fix: resolve on the branch by hand (merge main in, keep "ok", commit: a new tip) -> the next tick lands it
    m = subprocess.run(["git", "merge", "main"], cwd=wt, capture_output=True, text=True)
    assert m.returncode != 0 and "CONFLICT" in m.stdout + m.stderr
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    sgit(wt, "add", "tool.txt"); sgit(wt, "commit", "-q", "-m", "resolve: keep ok")
    res3 = _tick(root)
    assert Store(root).get("T-0007").status == "merged", res3["needs"]
    assert sgit(root, "show", "main:tool.txt").strip() == "ok" and len(_events(root, "landed", "T-0007")) == 1
    assert len(_events(root, "land_failed", "T-0007")) == 1
    assert not any(k.startswith("land_failed:T-0007:") for k in (root / ".scr" / "watch.state.txt").read_text(encoding="utf-8").split())


def test_failing_tests_are_not_merged_and_raise_land_failed(tmp_path: Path, monkeypatch):
    """DoD 3b: the merge is clean but the workload's tests fail on the result: not merged, land_failed says
    `tests`, the trunk untouched. The tests ran in the worktree, on task/<id> with the trunk merged in, so
    the failing tree never reached the checkout. A fix on the branch (still uncommitted) lands next tick."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (wt / "tool.txt").write_text("broken\n", encoding="utf-8")
    main_before = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"] and Store(root).get("T-0007").status == "accepted", res
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and ev[0]["note"].startswith("FAILED (tests)") and "check.py" in ev[0]["note"], ev
    assert any("landing failed (tests)" in n for n in res["needs"]), res["needs"]
    assert sgit(root, "rev-parse", "main") == main_before and sgit(root, "show", "main:tool.txt").strip() == "base"
    assert sgit(wt, "status", "--porcelain") == "" and sgit(wt, "merge-base", "--is-ancestor", "main", "HEAD") == ""   # trunk merged INTO the branch
    log = (Store(root).get("T-0007").dir / "log.md").read_text(encoding="utf-8")
    assert "landed:" not in log
    # the scriber (or Omri) fixes the branch; the fix is uncommitted again, the next tick commits and lands it
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    res2 = _tick(root)
    assert Store(root).get("T-0007").status == "merged", res2["needs"]
    assert sgit(root, "show", "main:tool.txt").strip() == "ok" and len(_events(root, "land_failed", "T-0007")) == 1


def test_checkout_off_the_trunk_is_a_landing_failure_not_a_guess(tmp_path: Path, monkeypatch):
    """The trunk is config `main_branch`, and the checkout must be on it (T-0061 ensure_main's rule); a
    checkout on a side branch fails the landing with a pointer to `scr ensure-main`, touching nothing."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    sgit(root, "checkout", "-q", "-b", "side")
    res = _tick(root)
    assert Store(root).get("T-0007").status == "accepted"
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and "trunk" in ev[0]["note"] and "ensure-main" in ev[0]["note"], ev
    assert sgit(root, "show", "main:tool.txt").strip() == "base" and sgit(root, "rev-parse", "--abbrev-ref", "HEAD") == "side"


def test_running_scriptorium_worktree_is_checkpointed_each_tick(tmp_path: Path, monkeypatch):
    """The checkpoint step no longer skips a scriptorium task that has a worktree: its dirty tree is
    committed on task/<id> every tick (nothing may exist only in a worktree); a task without one is
    still flagged `tooling`."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0007", "returned")
    sscr(root, "--quiet", "task", "new", "Tooling 2", "--id", "T-0009", "--workload", "scriptorium", "--files", "tool.txt", "--origin", "omri")
    for st in ("triaged", "ready", "dispatched"):
        sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0009", st)
    sgit(root, "add", "-A"); sgit(root, "commit", "-q", "-m", "[journal] T-0009")
    spawn(root, ["T-0009"])
    wt9 = tmp_path / "scriptorium-worktrees" / "T-0009"
    sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0009", "in_progress")
    (wt9 / "tool.txt").write_text("work in progress\n", encoding="utf-8")
    from scr.watch import Watcher
    res = Watcher(root, quiet=True).tick()
    row = next(r for r in res["rows"] if r["task"] == "T-0009")
    assert "ckpt:" in row["flags"] and "tooling" not in row["flags"], row
    assert sgit(wt9, "status", "--porcelain") == "" and "[checkpoint] T-0009" in sgit(wt9, "log", "-1", "--format=%s")
    assert "drift" not in row["flags"]                                                        # base = the local trunk, not origin/main
    # a scriptorium task with no worktree is a hotfix in the checkout: flagged tooling, nothing to checkpoint
    sscr(root, "--quiet", "task", "new", "Hotfix", "--id", "T-0010", "--workload", "scriptorium", "--files", "scr/x.py", "--origin", "omri")
    for st in ("triaged", "ready", "dispatched", "in_progress"):
        sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0010", st)
    res = Watcher(root, quiet=True).tick()
    assert next(r for r in res["rows"] if r["task"] == "T-0010")["flags"] == "tooling"


# ---- T-0091: the watcher applies a Decision written in review.md ----------------------------------------
# T-0061's review wrote `Decision: escalated (...)` on 2026-09-19 with `scr` denied, and the task sat
# `submitted` for 35 h. `decision_of` is a pure function of review.md's text; `apply_decision` runs it for
# every submitted task on a tick with auto_decide on and applies the verdict as steward:<id>.
from scr.watch import DECISION_NOTE, decision_of, read_decision  # noqa: E402

TEMPLATE = (REPO / "templates" / "review.md").read_text(encoding="utf-8")
LOG = Path("reports") / "steward_watch.log"


def _review(**sections: str) -> str:
    """The template with `## Decision` and/or `## Feedback / diagnosis` bodies replaced."""
    text = TEMPLATE
    if "decision" in sections:
        text = text.replace("accepted | returned (feedback below) | escalated (Gate N, reason)", sections["decision"])
    if "mechanical" in sections:                                # a row under the table's rule (T-0081's guard needs one to accept)
        text = text.replace("|---|---|\n", "|---|---|\n" + sections["mechanical"] + "\n")
    if "feedback" in sections:
        text = text.rstrip("\n") + "\n" + sections["feedback"] + "\n"
    return text


def test_decision_of_reads_the_explicit_line_for_each_verdict():
    for v in ("accepted", "returned", "escalated"):
        assert decision_of(_review(feedback=f"Decision: {v}")) == v
        assert decision_of(_review(feedback=f"Decision: {v} (Gate 2, the reason)")) == v
        assert decision_of(f"# Review\n\nsome prose\n\nDecision: {v}\n\nmore prose\n") == v
    # the first `Decision:` line at column zero wins; an indented or bulleted one is not the explicit form
    assert decision_of("Decision: returned (feedback)\nDecision: accepted\n") == "returned"
    assert decision_of("  Decision: accepted\n- Decision: accepted\n") is None
    # T-0061's review.md: the template form and the explicit form, agreeing
    assert decision_of(_review(decision="escalated", feedback="Decision: escalated (fix uncommitted in an unreadable worktree)")) == "escalated"


def test_decision_of_reads_the_template_form_under_the_heading():
    """T-0062's review wrote its verdict exactly as the template lays it out: `## Decision` then the verdict
    as the next line, with NO `Decision:` line at column zero."""
    assert decision_of(_review(decision="escalated (landing path: spawned scriptorium task, uncommitted in its worktree)")) == "escalated"
    assert decision_of(_review(decision="accepted")) == "accepted"
    assert decision_of(_review(decision="returned (feedback below)")) == "returned"
    assert decision_of(_review(decision="**accepted** — statement diff empty")) == "accepted"
    assert decision_of(_review(decision="\n\nescalated\n")) == "escalated"                    # blank lines first
    assert decision_of("## Decision\n\n## Feedback / diagnosis\naccepted is not a decision\n") is None   # an empty section


def test_decision_of_ignores_the_placeholder_line():
    assert decision_of(TEMPLATE) is None
    assert read_decision(TEMPLATE) == (None, None)
    assert decision_of(TEMPLATE.strip()) is None
    # the brief's own wording copied as an explicit line is the placeholder too, not `accepted`
    assert read_decision(_review(feedback="Decision: accepted | returned (feedback) | escalated (Gate N, reason)")) == (None, None)
    # a half-edited placeholder names two verdicts: unreadable, never `accepted`
    v, problem = read_decision(_review(decision="accepted | returned (feedback below)"))
    assert v is None and problem and "unreadable" in problem


def test_decision_of_ignores_a_decision_inside_a_code_fence():
    """T-0050's transcript quoted a draft review.md in a ``` block, `Decision: PENDING` inside it."""
    fenced = _review(feedback="**Draft review.md** (provisional):\n\n```\n# Review — T-0050\nDecision: PENDING — run pytest;\n  if both pass, accepted.\n```\n")
    assert read_decision(fenced) == (None, None)
    fenced_verdict = _review(feedback="```\nDecision: accepted\n```\n")
    assert read_decision(fenced_verdict) == (None, None)
    # ~~~ fences and a fenced `## Decision` heading are ignored the same way; the real line after the fence counts
    assert decision_of("~~~\n## Decision\naccepted\n~~~\nDecision: returned\n") == "returned"
    assert decision_of("```md\nDecision: accepted\n```\n\nDecision: escalated (Gate 1)\n") == "escalated"


def test_read_decision_names_a_malformed_or_ambiguous_line():
    v, problem = read_decision(_review(feedback="Decision: maybe"))
    assert v is None and problem == "unreadable Decision line 'Decision: maybe'"
    v, problem = read_decision(_review(feedback="Decision: PENDING — run the checks; if both pass, accepted."))
    assert v is None and "unreadable" in problem
    v, problem = read_decision(_review(decision="Not yet: the checks did not run."))
    assert v is None and "unreadable" in problem
    # the two forms disagreeing is ambiguous
    v, problem = read_decision(_review(decision="accepted", feedback="Decision: returned (see above)"))
    assert v is None and "disagree" in problem
    # a malformed explicit line is not rescued by a readable section
    v, problem = read_decision(_review(decision="accepted", feedback="Decision: see above"))
    assert v is None and "unreadable" in problem


def _submit(root: Path, tid: str = "T-0001") -> None:
    for st in ("dispatched", "in_progress", "submitted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", tid, st)


def _state_events(root: Path, tid: str) -> list[dict]:
    from scr.events import EventLog
    return [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "state" and e.get("task") == tid]


def _write_review(root: Path, tid: str, text: str) -> None:
    (Store(root).get(tid).dir / "review.md").write_text(text, encoding="utf-8")


def test_written_decision_is_applied_as_steward(world):
    """DoD: submitted task, review.md with `Decision: escalated (reason)` -> after one tick the task is
    escalated, one state event with actor steward:<id> and the note, the log says `decision applied`."""
    from scr.watch import Watcher
    root, _, _ = world
    _submit(root)
    _write_review(root, "T-0001", _review(decision="escalated", feedback="Decision: escalated (Gate 2, no landing path)"))
    n_before = len(_state_events(root, "T-0001"))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"], res
    assert Store(root).get("T-0001").status == "escalated"
    new = _state_events(root, "T-0001")[n_before:]
    assert len(new) == 1 and new[0]["from"] == "submitted" and new[0]["to"] == "escalated", new
    assert new[0]["actor"] == "steward:T-0001" and new[0]["note"].startswith(DECISION_NOTE), new[0]
    assert res["decided"] == [{"task": "T-0001", "decision": "escalated"}] and res["review"] == []
    assert "T-0001 decision applied: escalated" in (root / LOG).read_text(encoding="utf-8")
    assert not any("T-0001" in n for n in res["needs"]), res["needs"]
    # nothing to do next tick: the status has moved
    res2 = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "escalated" and len(_state_events(root, "T-0001")) == n_before + 1 and not res2["decided"]


def test_template_form_decision_is_applied(world):
    from scr.watch import Watcher
    root, _, _ = world
    _submit(root)
    _write_review(root, "T-0001", _review(decision="returned (feedback below)", feedback="The tests are red; see the log."))
    Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "returned"
    assert _state_events(root, "T-0001")[-1]["actor"] == "steward:T-0001"


def test_blank_template_review_is_no_decision(world):
    from scr.watch import Watcher
    root, _, _ = world
    _submit(root)
    _write_review(root, "T-0001", TEMPLATE)
    n_before = len(_state_events(root, "T-0001"))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "submitted" and res["review"] == ["T-0001"] and not res["decided"]
    assert len(_state_events(root, "T-0001")) == n_before
    assert not res["needs"], res["needs"]
    assert "decision" not in (root / LOG).read_text(encoding="utf-8")
    # no review.md at all is the same
    (Store(root).get("T-0001").dir / "review.md").unlink()
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "submitted" and not res["needs"] and len(_state_events(root, "T-0001")) == n_before


def test_malformed_decision_is_left_for_omri(world):
    from scr.watch import Watcher
    root, _, _ = world
    _submit(root)
    _write_review(root, "T-0001", _review(feedback="Decision: maybe"))
    n_before = len(_state_events(root, "T-0001"))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "submitted" and len(_state_events(root, "T-0001")) == n_before
    assert not res["decided"]
    needs = [n for n in res["needs"] if n.startswith("T-0001 ")]
    assert len(needs) == 1 and "Decision" in needs[0] and "'Decision: maybe'" in needs[0] and "task set T-0001" in needs[0], res["needs"]
    assert "T-0001 decision NOT applied: unreadable Decision line 'Decision: maybe'" in (root / LOG).read_text(encoding="utf-8")


def test_auto_decide_off_applies_nothing(world):
    from scr.watch import Watcher
    root, _, _ = world
    _submit(root)
    _write_review(root, "T-0001", _review(feedback="Decision: escalated (Gate 2)"))
    n_before = len(_state_events(root, "T-0001"))
    res = Watcher(root, quiet=True).tick()
    assert Store(root).get("T-0001").status == "submitted" and len(_state_events(root, "T-0001")) == n_before
    assert not res["decided"] and res["review"] == ["T-0001"] and not res["needs"]
    assert "decision" not in (root / LOG).read_text(encoding="utf-8")


def test_decided_task_gets_no_review_session(world, monkeypatch):
    """auto_review and auto_decide together: a written Decision is applied and no review is launched for
    it (the `review:<id>` flag stays clear); a submitted task without one still gets its review."""
    from scr.watch import Watcher
    root, repo, _ = world
    calls = _record_launches(monkeypatch)
    _submit(root)
    _write_review(root, "T-0001", _review(feedback="Decision: escalated (Gate 2)"))
    res = Watcher(root, quiet=True, auto_review=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "escalated" and not calls
    assert "review:T-0001" not in (root / ".scr" / "watch.state.txt").read_text(encoding="utf-8").split()
    # a second submitted task, no decision: reviewed as before
    scr(root, "--quiet", "task", "new", "Prove b", "--id", "T-0002", "--files", "QuantumInsertion/A.lean", "--origin", "omri")
    for st in ("triaged", "ready", "dispatched"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0002", st)
    spawn(root, ["T-0002"], lake=False)
    for st in ("in_progress", "submitted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0002", st)
    Watcher(root, quiet=True, auto_review=True, auto_decide=True).tick()
    assert Store(root).get("T-0002").status == "submitted" and len(calls) == 1 and calls[0]["argv"][1] == "-p"
    assert "review:T-0002" in (root / ".scr" / "watch.state.txt").read_text(encoding="utf-8").split()


def test_same_review_is_never_applied_twice(world):
    """Idempotent: the note names the review text by digest; a task moved back to `submitted` with the
    old review.md still in place gets a NEEDS-YOU line, not a second application. A fresh review (new
    text) is applied."""
    from scr.watch import Watcher
    root, _, _ = world
    _submit(root)
    stale = _review(feedback="Decision: returned (the tests are red)")
    _write_review(root, "T-0001", stale)
    Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "returned"
    first = _state_events(root, "T-0001")[-1]
    assert "[review.md " in first["note"]
    # the task runs again and is submitted again; the old review.md is still there
    for st in ("ready", "dispatched", "in_progress", "submitted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    n_before = len(_state_events(root, "T-0001"))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "submitted" and len(_state_events(root, "T-0001")) == n_before
    assert not res["decided"] and res["review"] == ["T-0001"]
    needs = [n for n in res["needs"] if n.startswith("T-0001 ")]
    assert len(needs) == 1 and "already applied" in needs[0] and "returned" in needs[0] and "fresh review" in needs[0], res["needs"]
    # Omri (or the Steward) rewrites the review: applied
    _write_review(root, "T-0001", _review(decision="escalated (Gate 1, the statement changed)"))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "escalated" and res["decided"] == [{"task": "T-0001", "decision": "escalated"}]
    assert not [n for n in res["needs"] if n.startswith("T-0001 ")]


def test_accepted_decision_becomes_the_status(world):
    """`accepted` written in review.md becomes the status through the same `scr task set` every other actor
    uses, so T-0081's guard (no acceptance without a written review) applies to this path once it lands.
    Landing is not driven here (auto_land off); the tick reloads tasks after a decision so auto-land sees it."""
    from scr.watch import Watcher
    root, _, _ = world
    _submit(root)
    _write_review(root, "T-0001", _review(decision="accepted", mechanical="| tests | pass |", feedback="Decision: accepted"))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "accepted" and res["decided"] == [{"task": "T-0001", "decision": "accepted"}]


def test_verify_failure_holds_the_decision(world):
    """A tick whose verify fails touches no records: the decision waits (like every other transition)."""
    from scr.watch import Watcher
    root, _, _ = world
    _submit(root)
    _write_review(root, "T-0001", _review(feedback="Decision: escalated (Gate 2)"))
    import json
    from scr.canon import canon
    p = root / "events" / "events.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    ev = json.loads(lines[1]); ev["actor"] = "mallory"; lines[1] = canon(ev)            # test_scriptorium's tamper
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert not res["ok"] and Store(root).get("T-0001").status == "submitted" and not res["decided"]


def test_unattended_watch_turns_auto_decide_on(world, monkeypatch):
    """`scr watch --unattended` implies auto_decide like auto_review / auto_dispatch / auto_land / auto_intake;
    the flag on its own works too; a bare `scr watch --once` leaves it off."""
    import scr.cli as cli
    import scr.watch as watch_mod
    root, _, _ = world
    seen: list[dict] = []

    class FakeWatcher:
        def __init__(self, root, **kw):
            seen.append(kw)

        def loop(self, interval, once=False):
            pass

    monkeypatch.setattr(watch_mod, "Watcher", FakeWatcher)
    cli.main(["--root", str(root), "watch", "--once", "--unattended"])
    cli.main(["--root", str(root), "watch", "--once", "--auto-decide"])
    cli.main(["--root", str(root), "watch", "--once"])
    assert [kw["auto_decide"] for kw in seen] == [True, True, False]
    assert seen[0]["auto_review"] and seen[0]["auto_dispatch"] and seen[0]["auto_land"] and seen[0]["auto_intake"]


# ---- T-0092: a stuck task leaves the slot ----------------------------------------------------------------
# 2026-09-19..21: T-0050 (review died, review.md blank) and T-0061 (verdict written, never applied) held both
# slots for 35 h with ~30 tasks ready. `submitted_stale` / `help_stale` are pure functions of the record's
# texts, the events and `now`; `escalation_checks` runs them on a tick with auto_decide on, after T-0091's
# Decision application and before auto-dispatch, so the freed slot is filled on the same tick.
import os  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from scr.watch import HELP_STALE_HOURS, REVIEW_STALE_HOURS, SLOT_STATUSES, Watcher, help_stale, submitted_since, submitted_stale  # noqa: E402

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)
FLAGS = Path(".scr") / "watch.state.txt"


def _spy_scr(monkeypatch) -> list[tuple[str, tuple]]:
    """Record every `scr` call the watcher makes (actor, args), in order, and still run it."""
    calls: list[tuple[str, tuple]] = []
    real = Watcher.scr

    def spy(self, actor, *args):
        calls.append((actor, args))
        return real(self, actor, *args)

    monkeypatch.setattr(Watcher, "scr", spy)
    return calls


def _ready_task(root: Path, tid: str, path: str) -> None:
    scr(root, "--quiet", "task", "new", f"Prove {tid}", "--id", tid, "--files", path, "--origin", "omri")
    for st in ("triaged", "ready"):
        scr(root, "--quiet", "--actor", "s", "task", "set", tid, st)


def _running(root: Path, tid: str = "T-0001") -> None:
    for st in ("dispatched", "in_progress"):
        scr(root, "--quiet", "--actor", "s", "task", "set", tid, st)


def _flags(root: Path) -> set[str]:
    return set((root / FLAGS).read_text(encoding="utf-8").split()) if (root / FLAGS).exists() else set()


def test_stuck_submitted_stale_is_a_pure_function_of_review_and_time():
    since = NOW - timedelta(hours=3)
    assert submitted_stale("submitted", TEMPLATE, since, 2.0, NOW) == {"since": since, "age_h": 3.0}
    assert submitted_stale("submitted", "", since, 2.0, NOW)["age_h"] == 3.0                    # no review.md at all
    assert submitted_stale("submitted", TEMPLATE, NOW - timedelta(hours=1), 2.0, NOW) is None    # not yet
    assert submitted_stale("submitted", TEMPLATE, None, 2.0, NOW) is None                        # undated
    assert submitted_stale("in_progress", TEMPLATE, since, 2.0, NOW) is None
    # a written Decision wins over the timeout, readable (T-0091 applies it) or not (T-0091 asks Omri)
    for text in (_review(decision="escalated"), _review(feedback="Decision: accepted"), _review(feedback="Decision: maybe")):
        assert submitted_stale("submitted", text, since, 2.0, NOW) is None, text
    assert submitted_stale("submitted", "```\nDecision: accepted\n```\n", since, 2.0, NOW) is not None   # fenced = not written


def test_stuck_help_stale_reads_the_newest_blocking_line_and_its_answer():
    old = NOW - timedelta(hours=4)
    log = "- 2026-09-20T19:00:00 [launcher] pid 1, transcript x\n- HELP[blocks: Phase C]: which lemma closes the goal?\n"
    seen = {"- HELP[blocks: Phase C]: which lemma closes the goal?": old}
    row = help_stale(log, 3.0, NOW, seen)
    assert row and row["line"] == 2 and row["since"] == old and row["age_h"] == 4.0 and row["text"].startswith("- HELP[blocks")
    assert help_stale(log, 5.0, NOW, seen) is None                                                # not yet
    assert help_stale(log, 3.0, NOW, {}) is None                                                  # undatable: never early
    assert help_stale(log, 3.0, NOW, {}, fallback=old)["since"] == old                            # the caller's mtime dates it
    # an answer anywhere after it — the Steward's, the UI's answer box, Omri's — means it is not stuck
    for ans in ("- 2026-09-20T21:00:00 [steward] (omri, via UI) use foo_bar\n", "[omri] use foo_bar\n", "  [steward:T-0001] see docs\n"):
        assert help_stale(log + ans, 3.0, NOW, seen) is None, ans
    # a `HELP[proceeding: …]` line is not a block; a plain `HELP:` line is
    assert help_stale("- HELP[proceeding: assume N=2]: ok?\n", 0.0, NOW, {}, fallback=old) is None
    plain = "HELP: please checkpoint\n"
    assert help_stale(plain, 3.0, NOW, {"HELP: please checkpoint": old})["line"] == 1
    # the newest blocking line is the one that counts: an old answered one above does not, a new one does
    two = log + "[steward] use foo_bar\n- HELP: and now this?\n"
    assert help_stale(two, 3.0, NOW, {**seen, "- HELP: and now this?": NOW - timedelta(minutes=5)}) is None
    assert help_stale(two, 3.0, NOW, {**seen, "- HELP: and now this?": old})["line"] == 4
    # a timestamp inside the line dates it when no event does
    assert help_stale("- HELP: stuck since 2026-09-20T19:30:00Z on X\n", 3.0, NOW, {})["age_h"] == 4.5
    assert help_stale("", 3.0, NOW, {}) is None


def test_stuck_submitted_is_escalated_and_the_slot_refilled_on_the_same_tick(world, monkeypatch):
    """DoD 1: submitted task older than review_stale_hours, review.md without Decision, cap 2, one ready task
    -> after one tick() the task is escalated (actor steward:<id>), one slot_freed event, and the dispatch call
    was made in the same tick — after the escalation — and launched the ready task."""
    from scr.ui import state
    root, repo, _ = world
    _set_limit(root, "concurrency", 2); _set_limit(root, "review_stale_hours", 0)
    _submit(root)
    _write_review(root, "T-0001", TEMPLATE)                                                  # the review died before writing anything
    (Store(root).get("T-0001").dir / "review-session-20260919-161106.txt").write_text("killed\n", encoding="utf-8")
    _ready_task(root, "T-0002", "QuantumInsertion/B.lean")
    launches = _record_launches(monkeypatch)
    calls = _spy_scr(monkeypatch)
    n_before = len(_state_events(root, "T-0001"))
    res = Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()
    assert res["ok"], res
    assert Store(root).get("T-0001").status == "escalated"
    new = _state_events(root, "T-0001")[n_before:]
    assert len(new) == 1 and (new[0]["from"], new[0]["to"], new[0]["actor"]) == ("submitted", "escalated", "steward:T-0001"), new
    assert "never held" in new[0]["note"] and "no Decision written" in new[0]["note"], new[0]["note"]
    assert "tasks/T-0001-prove-a/review-session-20260919-161106.txt" in new[0]["note"] and "task/T-0001" in new[0]["note"]
    ev = _events(root, "slot_freed", "T-0001")
    assert len(ev) == 1 and ev[0]["actor"] == "steward:T-0001", ev
    assert ev[0]["note"].startswith("ACTION for Omri: read the review transcript") and "review-session-20260919-161106.txt" in ev[0]["note"]
    assert res["escalated"] == [{"task": "T-0001", "since": res["escalated"][0]["since"], "reason": res["escalated"][0]["reason"]}] and res["review"] == []
    assert "T-0001 stuck -> escalated" in (root / LOG).read_text(encoding="utf-8")
    # the same tick: the dispatch call came AFTER the escalation and the slot went to T-0002
    i_esc = next(i for i, (_, a) in enumerate(calls) if a[:2] == ("task", "set") and a[2:4] == ("T-0001", "escalated"))
    i_disp = next(i for i, (_, a) in enumerate(calls) if a[0] == "dispatch")
    assert i_esc < i_disp, calls
    assert Store(root).get("T-0002").status == "in_progress" and len(launches) == 1
    # one needs-you card for it, of the alarm kind, whose text is the action
    cards = [c for c in state(root)["needs"] if c.get("id") == "T-0001" and c.get("name") == "slot_freed"]
    assert len(cards) == 1 and cards[0]["text"].startswith("ACTION for Omri"), cards
    # one escalation per episode: the flag is persisted, a second tick adds nothing
    assert any(k.startswith("slot_freed:T-0001:") for k in _flags(root)) and "review:T-0001" not in _flags(root)
    res2 = Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()
    assert not res2["escalated"] and len(_events(root, "slot_freed", "T-0001")) == 1
    assert Store(root).get("T-0001").status == "escalated" and len(_state_events(root, "T-0001")) == n_before + 1


def test_stuck_escalation_frees_a_full_cap(world, monkeypatch):
    """Cap 1: before the escalation nothing could be dispatched; the escalated task no longer counts, so the
    ready task is dispatched on the very tick that escalated it."""
    root, _, _ = world
    _set_limit(root, "concurrency", 1); _set_limit(root, "review_stale_hours", 0)
    _submit(root)
    _ready_task(root, "T-0002", "QuantumInsertion/B.lean")
    _record_launches(monkeypatch)
    assert "running: 1, cap 1" in scr(root, "dispatch", "--dry-run").stdout
    Watcher(root, quiet=True, auto_dispatch=True).tick()                                    # auto_decide off: held, nothing dispatched
    assert Store(root).get("T-0001").status == "submitted" and Store(root).get("T-0002").status == "ready"
    Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()
    assert Store(root).get("T-0001").status == "escalated" and Store(root).get("T-0002").status == "in_progress"


def test_stuck_submitted_with_a_decision_is_left_to_t0091(world):
    """DoD 2: a submitted task with a Decision: line is untouched by the clock — T-0091 applies a readable one
    and reports an unreadable one; neither is an escalation-by-timeout."""
    root, _, _ = world
    _set_limit(root, "review_stale_hours", 0)
    _submit(root)
    _write_review(root, "T-0001", _review(feedback="Decision: maybe"))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "submitted" and not res["escalated"] and not _events(root, "slot_freed", "T-0001")
    assert res["review"] == ["T-0001"] and any("cannot read" in n for n in res["needs"])
    _write_review(root, "T-0001", _review(decision="returned (feedback below)", feedback="Decision: returned (tests red)"))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "returned" and res["decided"] and not res["escalated"]
    assert not _events(root, "slot_freed", "T-0001") and _state_events(root, "T-0001")[-1]["note"].startswith(DECISION_NOTE)
    # and a fresh submission is not stale while it is younger than the limit
    for st in ("ready", "dispatched", "in_progress", "submitted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    _set_limit(root, "review_stale_hours", 2)
    _write_review(root, "T-0001", TEMPLATE)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "submitted" and not res["escalated"]


def test_stuck_unanswered_blocking_help_is_escalated(world):
    """DoD 3: in_progress task whose log.md has an unanswered `HELP[blocks: …]` older than help_stale_hours
    -> escalated; the same with a [steward] answer after it -> untouched. The bracket form raises no `help`
    event yet (that regex is T-0064/T-0068's), so the line is dated by log.md's mtime, backdated 4 h here."""
    root, _, _ = world
    _running(root)
    t = Store(root).get("T-0001")
    log = t.dir / "log.md"
    log.write_text(log.read_text(encoding="utf-8") + "- HELP[blocks: Phase C]: which lemma closes the goal?\n", encoding="utf-8")
    old = time.time() - 4 * 3600
    os.utime(log, (old, old))
    assert float(yaml.safe_load((root / "config" / "limits.yaml").read_text(encoding="utf-8"))["help_stale_hours"]) == HELP_STALE_HOURS == 3.0
    n_before = len(_state_events(root, "T-0001"))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and Store(root).get("T-0001").status == "escalated", res
    ev = _events(root, "slot_freed", "T-0001")
    assert len(ev) == 1 and "answer log.md:" in ev[0]["note"] and "which lemma closes the goal?" in ev[0]["note"], ev
    last = _state_events(root, "T-0001")[-1]
    assert last["to"] == "escalated" and last["actor"] == "steward:T-0001" and "HELP line unanswered for 4.0 h" in last["note"], last
    assert len(_state_events(root, "T-0001")) == n_before + 3                                 # checkpoint, in_progress, escalated
    assert next(r for r in res["rows"] if r["task"] == "T-0001")["flags"].endswith("ESCALATED")
    # the answered twin: untouched
    _ready_task(root, "T-0002", "QuantumInsertion/B.lean")
    _running(root, "T-0002")
    log2 = Store(root).get("T-0002").dir / "log.md"
    log2.write_text(log2.read_text(encoding="utf-8") + "- HELP[blocks: Phase C]: which lemma?\n- 2026-09-21T00:00:00 [steward] (omri, via UI) use foo_bar\n", encoding="utf-8")
    os.utime(log2, (old, old))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0002").status == "in_progress" and not res["escalated"] and not _events(root, "slot_freed", "T-0002")


def test_stuck_plain_help_is_dated_by_its_help_event(world):
    """A plain `HELP:` line gets a `help` event the first time the watcher sees it; that timestamp dates the
    wait, so the tick that first sees it never escalates (age 0) whatever log.md's mtime says."""
    root, _, _ = world
    _running(root)
    log = Store(root).get("T-0001").dir / "log.md"
    log.write_text(log.read_text(encoding="utf-8") + "HELP: please checkpoint\n", encoding="utf-8")
    old = time.time() - 4 * 3600
    os.utime(log, (old, old))                                    # mtime says 4 h; the help event says now (default limit 3 h)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "in_progress" and not res["escalated"] and len(_events(root, "help", "T-0001")) == 1
    _set_limit(root, "help_stale_hours", 0)                      # any wait counts; the event's second-truncated ts is now behind us
    time.sleep(1.1)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "escalated" and len(_events(root, "slot_freed", "T-0001")) == 1
    assert len(_events(root, "help", "T-0001")) == 1


def test_stuck_auto_decide_off_or_verify_failed_holds(world):
    root, _, _ = world
    _set_limit(root, "review_stale_hours", 0)
    _submit(root)
    res = Watcher(root, quiet=True).tick()
    assert Store(root).get("T-0001").status == "submitted" and not res["escalated"] and not _events(root, "slot_freed", "T-0001")
    import json
    from scr.canon import canon
    p = root / "events" / "events.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    ev = json.loads(lines[1]); ev["actor"] = "mallory"; lines[1] = canon(ev)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert not res["ok"] and Store(root).get("T-0001").status == "submitted" and not res["escalated"]


def test_stuck_escalated_and_returned_never_count_against_the_cap(world, monkeypatch):
    """DoD 4: neither counter — the watcher's n_running nor cmd_dispatch's running — counts escalated or
    returned; with cap 1, one escalated and one returned task, the ready task is dispatched."""
    root, _, _ = world
    assert SLOT_STATUSES == {"dispatched", "in_progress", "checkpoint", "submitted"}
    assert "escalated" not in SLOT_STATUSES and "returned" not in SLOT_STATUSES
    _set_limit(root, "concurrency", 1)
    _running(root)
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", "escalated")
    _ready_task(root, "T-0002", "QuantumInsertion/B.lean")
    _running(root, "T-0002")
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0002", "returned")
    _ready_task(root, "T-0003", "QuantumInsertion/C.lean")
    out = scr(root, "dispatch", "--dry-run").stdout
    assert "would dispatch: T-0003 (running: 0, cap 1)" in out, out
    _record_launches(monkeypatch)
    calls = _spy_scr(monkeypatch)
    Watcher(root, quiet=True, auto_dispatch=True).tick()
    assert any(a[0] == "dispatch" for _, a in calls) and Store(root).get("T-0003").status == "in_progress"
    assert Store(root).get("T-0001").status == "escalated" and Store(root).get("T-0002").status == "returned"
    # and a task in any slot status fills the cap
    for st in ("ready", "dispatched", "in_progress", "submitted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0002", st)
    assert "(running: 2, cap 1)" in scr(root, "dispatch", "--dry-run").stdout


def test_stuck_rule_is_written_in_the_agent_docs():
    for name in ("_common.md", "steward.md"):
        text = (REPO / "agents" / name).read_text(encoding="utf-8")
        assert "never held" in text and "same tick" in text, name
    assert submitted_since([{"kind": "state", "task": "T-1", "to": "submitted", "ts": "2026-09-19T16:11:02Z"}], "T-1") == datetime(2026, 9, 19, 16, 11, 2, tzinfo=timezone.utc)
    assert REVIEW_STALE_HOURS == 2.0
