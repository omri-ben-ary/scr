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
from _paths import CONFIG  # noqa: E402

FAKE_CLAUDE = str(Path(sys.executable).with_name("claude.exe"))   # a .exe path: claude_argv must not wrap it in `cmd /c`


class _Proc:
    pid = 4242
    returncode = 0
    stdout = ""
    stderr = ""

    def communicate(self, input=None, timeout=None):      # Intake waits on its Popen (T-0247)
        return "", ""

    def kill(self):
        pass


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
    """Replace the claude lookup and the spawn paths (Popen in scr.run for detached sessions, Popen and
    subprocess.run in scr.launch for the synchronous Intake); return the list that collects the calls."""
    import scr.launch as launch_mod
    import scr.run as run_mod
    calls: list[dict] = []
    monkeypatch.setattr(run_mod, "claude_bin", lambda: FAKE_CLAUDE)
    import scr.answers as answers_mod
    monkeypatch.setattr(launch_mod, "claude_bin", lambda: FAKE_CLAUDE)   # launch.py and answers.py bind the name at import: patch it
    monkeypatch.setattr(answers_mod, "claude_bin", lambda: FAKE_CLAUDE)
    # there too, or the tests pass only where a real `claude` is on PATH (the PC, not CI)

    def fake_popen(args, cwd=None, **kw):
        stdin_is_file = hasattr(kw.get("stdin"), "read")          # T-0330: Intake hands its brief in as a file, never a pipe
        for k in ("stdin", "stdout", "stderr"):
            f = kw.get(k)
            if hasattr(f, "close"):
                f.close()
        calls.append({"argv": list(args), "cwd": Path(cwd), "env": kw.get("env"), "stdin_is_file": stdin_is_file})
        return _Proc()

    def fake_run(args, cwd=None, **kw):
        calls.append({"argv": list(args), "cwd": Path(cwd)})
        return _Proc()

    monkeypatch.setattr(run_mod, "subprocess", _Shim(subprocess, Popen=fake_popen))
    monkeypatch.setattr(launch_mod, "subprocess", _Shim(subprocess, run=fake_run, Popen=fake_popen))
    return calls


def _mode(argv: list[str]) -> str:
    return argv[argv.index("--permission-mode") + 1]


def _add_dirs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]


def _set_limit(root: Path, key: str, value) -> None:
    """Edit config/limits.yaml and commit it, like the fixture's seed. Uncommitted, the next journaling scr
    command would see a tooling path (`config/` is in hotfix.TOOLING) changed outside a task and open a hotfix
    record — whose id collides with the test's own `task new --id`. Git.dirty() used to mangle the FIRST
    porcelain line (T-0079 fixed it), which is the only reason this went unnoticed."""
    p = root / "config" / "limits.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg[key] = value
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    if (root / ".git").exists():
        sgit(root, "add", "config/limits.yaml"); sgit(root, "commit", "-q", "-m", f"[test] limits: {key}={value}")


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


def test_review_brief_uses_default_base_and_fetch(world, monkeypatch):
    """T-0117 (T-0060 R1 after T-0111): the workload review brief tells the Steward to run the checkers with
    --fetch and their default base (merge-base of origin/<main> and HEAD), never with a --base flag."""
    from scr.watch import Watcher
    root, _, _ = world
    _record_launches(monkeypatch)
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched"); spawn(root, ["T-0001"], lake=False)
    for st in ("in_progress", "submitted"):
        scr(root, "--actor", "s", "task", "set", "T-0001", st)
    t = Store(root).get("T-0001")
    Watcher(root, quiet=True).start_review(t)
    brief = (t.dir / "review-brief.txt").read_text(encoding="utf-8")
    assert "--fetch" in brief and "merge-base" in brief and "origin/" in brief, brief
    assert "--base" not in brief, brief


def test_workloads_yaml_checker_comment_has_no_bare_base():
    text = (CONFIG / "workloads.yaml").read_text(encoding="utf-8")
    assert "--base master" not in text
    line = next(l for l in text.splitlines() if l.strip().startswith("done_when_checker:"))
    assert "--fetch" in line and "merge-base" in line, line
    assert isinstance(yaml.safe_load(text)["quantum-insertion"]["done_when_checker"], str)


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


def _tick(root: Path, wait: float = 180.0) -> dict:
    """One auto-land tick — and, since 2026-10-14 landings run as detached jobs, the wait for any landing job the
    tick submitted plus the tick that applies its result, so a test sees the landing's outcome as one step."""
    import time
    from scr import jobs
    from scr.watch import Watcher
    res = Watcher(root, quiet=True, auto_land=True).tick()
    t0 = time.time()
    while True:
        mine = [j for j in jobs.list_jobs(root) if str(j.get("name") or "").startswith("land-")]
        if not mine:
            return res
        if any(j["status"] in jobs.ACTIVE for j in mine):
            assert time.time() - t0 < wait, f"landing job still running after {wait}s: {mine}"
            time.sleep(1)
            continue
        if all(j.get("applied") for j in mine):
            return res
        res = Watcher(root, quiet=True, auto_land=True).tick()


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
    # merged is journaled (the tick commits the record) and pushed; the worktree is deleted (T-0321), the branch stays
    assert sgit(root, "status", "--porcelain") == ""
    assert sgit(root, "rev-parse", "main") == sgit(origin, "rev-parse", "main")
    assert not wt.exists() and not list((tmp_path / "scriptorium-RETIRED").glob("*-T-0007/worktree"))
    gone = _events(root, "worktree_deleted", "T-0007")
    assert len(gone) == 1 and gone[0]["data"]["path"] == str(wt) and gone[0]["data"]["bytes"] > 0, gone
    assert str(wt.resolve()).replace("\\", "/").lower() not in sgit(root, "worktree", "list").replace("\\", "/").lower()
    assert sgit(root, "rev-parse", "--verify", "task/T-0007")


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


def _resubmit_fixed(root: Path, tid: str = "T-0007") -> None:
    """The scriber's side of an auto-fix: the task went ready; it is run, resubmitted with a fresh accepting review."""
    for st in ("dispatched", "in_progress", "submitted"):
        sscr(root, "--quiet", "--actor", "s", "task", "set", tid, st)
    (Store(root).get(tid).dir / "review.md").write_text(accepted_review("| `pytest tests -q` | passed again |"), encoding="utf-8")
    sscr(root, "--quiet", "--actor", "s", "task", "set", tid, "accepted")


def _flags(root: Path) -> set[str]:
    p = root / ".scr" / "watch.state.txt"
    return set(p.read_text(encoding="utf-8").split()) if p.exists() else set()


def test_merge_conflict_goes_back_to_the_scriber_not_to_omri(tmp_path: Path, monkeypatch):
    """2026-10-14 (Omri: "I don't want to be resolving merges"). The trunk changed the same line the worktree
    changed: the landing fails (one land_failed event, the merge aborted, the trunk untouched) and the task is
    sent back to its scriber — accepted -> returned -> ready, an auto_fix event, the fix note in log.md — with no
    NEEDS-YOU line. Once the scriber resolves and resubmits, the next tick lands it."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (root / "tool.txt").write_text("trunk edit\n", encoding="utf-8")
    sgit(root, "add", "-A"); sgit(root, "commit", "-q", "-m", "[journal] the trunk moved on tool.txt")
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    main_before = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"], res
    t = Store(root).get("T-0007")
    assert t.status == "ready", t.status
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and "conflict" in ev[0]["note"] and "tool.txt" in ev[0]["note"], ev
    fx = _events(root, "auto_fix", "T-0007")
    assert len(fx) == 1 and fx[0]["note"].startswith("auto-fix 1/2 (land)"), fx
    assert "auto-fix 1/2 (land)" in (t.dir / "log.md").read_text(encoding="utf-8")
    assert not any("landing failed" in n for n in res["needs"]), res["needs"]
    assert sgit(root, "rev-parse", "main") == main_before and sgit(root, "show", "main:tool.txt").strip() == "trunk edit"
    assert not (root / ".git" / "worktrees" / "T-0007" / "MERGE_HEAD").exists()
    assert sgit(wt, "status", "--porcelain") == ""
    # the scriber resolves on the branch (merge main in, keep "ok", commit) and resubmits -> the next tick lands it
    m = subprocess.run(["git", "merge", "main"], cwd=wt, capture_output=True, text=True)
    assert m.returncode != 0 and "CONFLICT" in m.stdout + m.stderr
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    sgit(wt, "add", "tool.txt"); sgit(wt, "commit", "-q", "-m", "resolve: keep ok")
    _resubmit_fixed(root)
    res3 = _tick(root)
    assert Store(root).get("T-0007").status == "merged", res3["needs"]
    assert sgit(root, "show", "main:tool.txt").strip() == "ok" and len(_events(root, "landed", "T-0007")) == 1
    assert len(_events(root, "land_failed", "T-0007")) == 1
    assert not any(k.startswith("land_failed:T-0007:") for k in _flags(root))


def test_third_landing_failure_is_omris(tmp_path: Path, monkeypatch):
    """After AUTOFIX_MAX send-backs the landing failure is a NEEDS-YOU line and the task stays accepted."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (root / ".scr").mkdir(exist_ok=True)
    (root / ".scr" / "watch.state.txt").write_text("autofix:T-0007:land:1\nautofix:T-0007:land:2\n", encoding="utf-8")
    (wt / "tool.txt").write_text("broken\n", encoding="utf-8")
    res = _tick(root)
    assert res["ok"] and Store(root).get("T-0007").status == "accepted", res
    assert any("landing failed (tests)" in n for n in res["needs"]), res["needs"]
    assert not _events(root, "auto_fix", "T-0007")


def test_landing_test_timeout_comes_from_limits_yaml(tmp_path: Path, monkeypatch):
    """T-0186: land_scriptorium runs the tests within limits.yaml land_test_timeout_seconds (it was a
    hardcoded 1800 s in ops._run_tests). With the key at 5 and a test command that sleeps 10 s, the landing
    fails as `tests`, and the land_failed event names the 5 s and the key to raise."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    lim = yaml.safe_load((root / "config" / "limits.yaml").read_text(encoding="utf-8")) or {}
    lim["land_test_timeout_seconds"] = 5
    (root / "config" / "limits.yaml").write_text(yaml.safe_dump(lim), encoding="utf-8")
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text(encoding="utf-8"))
    wl["scriptorium"]["tests"] = f'{sys.executable} -c "import time; time.sleep(10)"'
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl), encoding="utf-8")
    sscr(root, "--quiet", "snapshot")                                   # the config hash moved: keep verify green
    sgit(root, "add", "-A"); sgit(root, "commit", "-q", "-m", "[journal] 5 s landing timeout, 10 s tests")
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    main_before = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"], res
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and "tests" in ev[0]["note"], ev
    assert "timed out after 5s" in ev[0]["note"] and "land_test_timeout_seconds" in ev[0]["note"], ev
    assert Store(root).get("T-0007").status == "ready" and sgit(root, "rev-parse", "main") == main_before   # back to the scriber (2026-10-14)


# T-0382: a pytest-shaped fake runner. A test id fails in a tree when mainfails.txt or tool.txt lists it; ids
# given on the command line run alone (the landing's main-side rerun), otherwise one passing test + every failure.
FAKE_PYTEST = """\
import sys
from pathlib import Path
fails = [l.strip() for f in ("mainfails.txt", "tool.txt") if Path(f).exists() for l in Path(f).read_text(encoding="utf-8").splitlines() if "::" in l]
ids = [a for a in sys.argv[1:] if "::" in a]
assert not ids or "tests" not in sys.argv[1:], sys.argv      # the rerun drops the suite path
run = ids or ["tests/test_a.py::test_ok"] + fails
bad = [i for i in run if i in fails]
for i in bad:
    print(f"FAILED {i} - AssertionError: fake")
print(f"{len(bad)} failed, {len(run) - len(bad)} passed in 0.01s" if bad else f"{len(run)} passed in 0.01s")
sys.exit(1 if bad else 0)
"""
MAIN_A, MAIN_B, NEW_C = "tests/test_ui.py::test_ui_http_and_reload", "tests/test_ui.py::test_ui_state_carries_watch_cards", "tests/test_x.py::test_c"


def _main_failing_world(tmp_path: Path, monkeypatch, branch_tool: str) -> tuple[Path, Path]:
    """_land_world whose trunk fails MAIN_A and MAIN_B (mainfails.txt, committed on main after the spawn, so the
    landing's trunk merge brings it to the branch) and whose worktree's tool.txt is `branch_tool`."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text(encoding="utf-8"))
    wl["scriptorium"]["tests"] = f"{sys.executable} fake_pytest.py tests -q"
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl), encoding="utf-8")
    (root / "fake_pytest.py").write_text(FAKE_PYTEST, encoding="utf-8")
    (root / "mainfails.txt").write_text(f"{MAIN_A}\n{MAIN_B}\n", encoding="utf-8")
    sscr(root, "--quiet", "snapshot")                                   # the config hash moved: keep verify green
    sgit(root, "add", "-A"); sgit(root, "commit", "-q", "-m", "[journal] fake pytest; the trunk fails A and B")
    (wt / "tool.txt").write_text(branch_tool, encoding="utf-8")
    return root, wt


def test_land_failing_only_on_main_failing_tests_is_held_not_returned(tmp_path: Path, monkeypatch):
    """T-0382 DoD: the landing's tests fail on A and B only, and A and B also fail on main (the rerun of just those
    ids in the checkout): the task stays accepted, no auto_fix, the land_failed event names A and B, and ONE
    NEEDS-YOU line names them — on this tick and the next."""
    root, _ = _main_failing_world(tmp_path, monkeypatch, "ok\n")
    main_before = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"], res
    assert Store(root).get("T-0007").status == "accepted"
    assert not _events(root, "auto_fix", "T-0007")
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and MAIN_A in ev[0]["note"] and MAIN_B in ev[0]["note"] and "also fail on the trunk" in ev[0]["note"], ev
    for r in (res, _tick(root)):
        lines = [n for n in r["needs"] if "T-0007" in n]
        assert len(lines) == 1 and MAIN_A in lines[0] and MAIN_B in lines[0] and "also fail on main" in lines[0], r["needs"]
    assert Store(root).get("T-0007").status == "accepted" and not _events(root, "auto_fix", "T-0007")
    assert sgit(root, "rev-parse", "main") == main_before and len(_events(root, "land_failed", "T-0007")) == 1


def test_land_failure_new_on_the_branch_still_goes_back_to_the_scriber(tmp_path: Path, monkeypatch):
    """T-0382 DoD: the same world, but the branch also fails C, which passes on main: today's path — auto_fix,
    the task ready again — and the land_failed event names A, B and C."""
    root, _ = _main_failing_world(tmp_path, monkeypatch, f"{NEW_C}\n")
    res = _tick(root)
    assert res["ok"], res
    assert Store(root).get("T-0007").status == "ready"
    assert len(_events(root, "auto_fix", "T-0007")) == 1
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and all(i in ev[0]["note"] for i in (MAIN_A, MAIN_B, NEW_C)) and "also fail on the trunk" not in ev[0]["note"], ev
    assert not any("also fail on main" in n for n in res["needs"]), res["needs"]
    assert not any(k.startswith("landmain:T-0007:") for k in _flags(root))


def _land_job_count(root: Path) -> int:
    from scr import jobs
    return len([j for j in jobs.list_jobs(root) if str(j.get("name") or "").startswith(jobs.LAND_PREFIX)])


def _trunk_commit(root: Path, path: str, text: str, msg: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text, encoding="utf-8")
    sgit(root, "add", path); sgit(root, "commit", "-q", "-m", msg)


def _hold_flags(root: Path) -> set[str]:
    """The watcher's flags as it reads them: one per line (a landmain value holds ", ")."""
    p = root / ".scr" / "watch.state.txt"
    return {l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()} if p.exists() else set()


def test_held_landing_is_retried_once_per_trunk_code_commit(tmp_path: Path, monkeypatch):
    """T-0404 DoD 1: a held landing (landhold + landmain) is resubmitted as a land job on the first tick after the
    trunk head moves by a code commit, and not again on later ticks while the head stays; a record-only trunk commit
    (the journal's) does not retry. The trunk still red: the retry re-holds with the same ids, one more land_failed."""
    root, _ = _main_failing_world(tmp_path, monkeypatch, "ok\n")
    _tick(root)
    assert "landhold:T-0007" in _hold_flags(root) and any(k.startswith("landretry:T-0007:") for k in _hold_flags(root))
    assert _land_job_count(root) == 1
    _trunk_commit(root, "tasks/note.md", "a record\n", "[journal] records only")
    _tick(root)
    assert _land_job_count(root) == 1                                       # records cannot fix a test: still held
    _trunk_commit(root, "other.txt", "unrelated code\n", "trunk: a code commit")
    head = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert _land_job_count(root) == 2, res["needs"]                         # retried once
    assert f"landretry:T-0007:{head}" in _hold_flags(root)
    assert Store(root).get("T-0007").status == "accepted" and not _events(root, "auto_fix", "T-0007")
    assert len(_events(root, "land_failed", "T-0007")) == 2
    for _ in range(2):
        r = _tick(root)
        lines = [n for n in r["needs"] if "T-0007" in n]
        assert len(lines) == 1 and "--skip-main-failing" in lines[0] and MAIN_A in lines[0], r["needs"]
    assert _land_job_count(root) == 2 and len(_events(root, "land_failed", "T-0007")) == 2


def test_held_landing_retry_lands_once_the_trunk_is_fixed(tmp_path: Path, monkeypatch):
    """T-0404 DoD 2: the trunk's fix of A and B (a code commit) retries the held landing; it lands, and the hold flags
    and the NEEDS-YOU line are gone."""
    root, _ = _main_failing_world(tmp_path, monkeypatch, "ok\n")
    _tick(root)
    assert "landhold:T-0007" in _hold_flags(root)
    _trunk_commit(root, "mainfails.txt", "", "trunk: fix A and B")
    res = _tick(root)
    assert Store(root).get("T-0007").status == "merged", res["needs"]
    assert not any(k.startswith(("landhold:T-0007", "landmain:T-0007:", "landretry:T-0007:")) for k in _hold_flags(root)), _hold_flags(root)
    assert not any("T-0007" in n for n in res["needs"]) and not any("T-0007" in n for n in _tick(root)["needs"])


def test_held_landing_retry_rehold_with_new_ids(tmp_path: Path, monkeypatch):
    """T-0404 DoD 2: the trunk fixes B only: the retry fails on A alone, still main-only: held again, landmain names A."""
    root, _ = _main_failing_world(tmp_path, monkeypatch, "ok\n")
    _tick(root)
    assert f"landmain:T-0007:{MAIN_A}, {MAIN_B}" in _hold_flags(root)
    _trunk_commit(root, "mainfails.txt", f"{MAIN_A}\n", "trunk: fix B")
    res = _tick(root)
    flags = _hold_flags(root)
    assert f"landmain:T-0007:{MAIN_A}" in flags and "landhold:T-0007" in flags, flags
    assert [k for k in flags if k.startswith("landmain:T-0007:")] == [f"landmain:T-0007:{MAIN_A}"]
    assert Store(root).get("T-0007").status == "accepted" and not _events(root, "auto_fix", "T-0007")
    lines = [n for n in res["needs"] if "T-0007" in n]
    assert len(lines) == 1 and MAIN_A in lines[0] and MAIN_B not in lines[0], res["needs"]


def test_held_landing_retry_with_a_branch_new_failure_goes_to_autofix(tmp_path: Path, monkeypatch):
    """T-0404 DoD 2: the trunk's new code makes the branch fail C, which passes on the trunk: the retry takes the
    auto-fix path (the task ready again, an auto_fix event) and the hold is dropped."""
    root, _ = _main_failing_world(tmp_path, monkeypatch, "ok\n")
    _tick(root)
    assert "landhold:T-0007" in _hold_flags(root)
    extra = FAKE_PYTEST.replace('run = ids', f'fails += ["{NEW_C}"] if Path("tool.txt").read_text(encoding="utf-8").strip() == "ok" else []\nrun = ids')
    _trunk_commit(root, "fake_pytest.py", extra, "trunk: a test the branch breaks")
    _tick(root)
    assert Store(root).get("T-0007").status == "ready"
    assert len(_events(root, "auto_fix", "T-0007")) == 1
    assert not any(k.startswith(("landhold:T-0007", "landmain:T-0007:", "landretry:T-0007:")) for k in _hold_flags(root)), _hold_flags(root)


def test_held_landing_from_before_the_retry_mark_is_still_honoured(tmp_path: Path, monkeypatch):
    """T-0404 judgment: a landmain flag persisted by a T-0382 watcher (no landretry mark) still holds: the first tick
    starts the clock at the trunk head (no retry), the next code commit retries."""
    root, _ = _main_failing_world(tmp_path, monkeypatch, "ok\n")
    _tick(root)
    p = root / ".scr" / "watch.state.txt"
    p.write_text("\n".join(k for k in p.read_text(encoding="utf-8").split() if not k.startswith("landretry:")) + "\n", encoding="utf-8")
    _trunk_commit(root, "other.txt", "code\n", "trunk: a code commit")
    res = _tick(root)
    assert _land_job_count(root) == 1 and any(MAIN_A in n for n in res["needs"]), res["needs"]
    assert f"landretry:T-0007:{sgit(root, 'rev-parse', 'main')}" in _hold_flags(root)
    _trunk_commit(root, "other.txt", "more code\n", "trunk: another code commit")
    _tick(root)
    assert _land_job_count(root) == 2


def test_failing_tests_go_back_to_the_scriber_not_to_omri(tmp_path: Path, monkeypatch):
    """DoD 3b, 2026-10-14 reading: the merge is clean but the workload's tests fail on the result: not merged,
    land_failed says `tests`, the trunk untouched — and the task is sent back to its scriber (ready, auto_fix event,
    the failure tail in log.md), not to Omri. The scriber's fix, resubmitted, lands next tick."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (wt / "tool.txt").write_text("broken\n", encoding="utf-8")
    main_before = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"], res
    t = Store(root).get("T-0007")
    assert t.status == "ready", t.status
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and ev[0]["note"].startswith("FAILED (tests)") and "check.py" in ev[0]["note"], ev
    assert not any("landing failed" in n for n in res["needs"]), res["needs"]
    log = (t.dir / "log.md").read_text(encoding="utf-8")
    assert "auto-fix 1/2 (land)" in log and "landing failed (tests)" in log and "landed:" not in log
    assert sgit(root, "rev-parse", "main") == main_before and sgit(root, "show", "main:tool.txt").strip() == "base"
    assert sgit(wt, "status", "--porcelain") == "" and sgit(wt, "merge-base", "--is-ancestor", "main", "HEAD") == ""   # trunk merged INTO the branch
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    _resubmit_fixed(root)
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


# ---- T-0120: record paths on task/<id> follow the trunk; one landing per task at a time ---------------------
# A scriber that runs `scr` with its worktree as root journals there as [omri] and can open a phantom hotfix
# task on the branch (T-0059: b4a70dc, phantom T-0108 59a57e2): events.jsonl, STATE.json, a tasks/ folder.
def _phantom_journal_commit(wt: Path) -> list[str]:
    """The shape of a worktree journal commit, committed on task/T-0007: an event appended to the branch's
    events.jsonl, STATE.json rewritten, a phantom task folder. Returns the three paths."""
    from scr.events import EventLog
    EventLog(wt / "events" / "events.jsonl").append("system", name="phantom", task="T-0099", actor="omri", note="journaled in a worktree")
    (wt / "state").mkdir(exist_ok=True)
    (wt / "state" / "STATE.json").write_text('{"phantom": "T-0099"}\n', encoding="utf-8")
    (wt / "tasks" / "T-0099-phantom").mkdir(parents=True)
    (wt / "tasks" / "T-0099-phantom" / "task.yaml").write_text("id: T-0099\ntitle: phantom hotfix\n", encoding="utf-8")
    paths = ["events/events.jsonl", "state/STATE.json", "tasks/T-0099-phantom/task.yaml"]
    sgit(wt, "add", "--", *paths); sgit(wt, "commit", "-q", "-m", "[omri] phantom journal commit in the worktree")
    return paths


def _trunk_journals(root: Path) -> str:
    """The checkout journals after the spawn: its own event appended (the same last line the branch appended
    to — a conflict). Returns main's events.jsonl as committed."""
    sscr(root, "--quiet", "--actor", "steward", "event", "system", "--name", "trunk_moved", "--note", "a journal commit on the trunk")
    sgit(root, "add", "-A"); sgit(root, "commit", "-q", "-m", "[journal] the trunk journals")
    return sgit(root, "show", "main:events/events.jsonl")


def test_records_follow_the_trunk_on_a_phantom_journal_commit(tmp_path: Path, monkeypatch):
    """T-0120 (a): the branch carries a phantom journal commit and the trunk journals too, so events.jsonl
    conflicts. The conflict is on record paths only: they take the trunk's side, the leftovers (STATE.json,
    the phantom task folder) are reverted before the tests, the task lands with its tool change, and the
    log.md landing line lists every record path that followed the trunk. No land_failed, no scope refusal."""
    root, _, wt = _land_world(tmp_path, monkeypatch, journal=True)
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    paths = _phantom_journal_commit(wt)
    trunk_events = _trunk_journals(root)
    res = _tick(root)
    assert res["ok"], res
    t = Store(root).get("T-0007")
    assert t.status == "merged", (t.status, res["needs"])
    assert sgit(root, "show", "main:tool.txt").strip() == "ok"
    assert "T-0099-phantom" not in sgit(root, "ls-tree", "--name-only", "main", "tasks/")
    main_events = sgit(root, "show", "main:events/events.jsonl")
    assert main_events.startswith(trunk_events) and '"phantom"' not in main_events       # the trunk's chain, plus the landing's own events
    from scr.events import EventLog
    assert EventLog(root / "events" / "events.jsonl").verify_chain() == []
    landed = [l for l in (t.dir / "log.md").read_text(encoding="utf-8").splitlines() if "landed: task/T-0007 -> main @" in l]
    assert len(landed) == 1 and "records follow the trunk" in landed[0], landed
    assert all(p in landed[0] for p in paths), landed[0]
    # both halves ran on task/T-0007: the conflicted events.jsonl took the trunk's side in the step-3 merge
    # commit, the clean leftovers were reverted by the step-3b commit
    msgs = sgit(root, "log", "--format=%B", "main")
    assert "records follow the trunk: " in msgs and "events/events.jsonl" in msgs.split("records follow the trunk: ")[1].splitlines()[0], msgs[:800]
    assert "[land] T-0007: record paths follow the trunk (" in msgs and "tasks/T-0099-phantom/task.yaml" in msgs
    assert not _events(root, "land_failed", "T-0007") and not res["needs"], res["needs"]


def test_record_conflict_next_to_a_tool_conflict_still_aborts(tmp_path: Path, monkeypatch):
    """T-0120 (b): the same phantom commit, but the branch's tool.txt also conflicts with the trunk. The
    trunk's side is taken by path, never for the whole merge: a non-record conflict aborts as before —
    land_failed (conflict) naming tool.txt, the worktree clean, the trunk untouched."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    _phantom_journal_commit(wt)
    (root / "tool.txt").write_text("trunk edit\n", encoding="utf-8")
    _trunk_journals(root)
    main_before = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"], res
    assert Store(root).get("T-0007").status == "ready"               # back to the scriber with the conflict (2026-10-14)
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and "conflict" in ev[0]["note"] and "tool.txt" in ev[0]["note"], ev
    assert len(_events(root, "auto_fix", "T-0007")) == 1 and not any("landing failed" in n for n in res["needs"]), res["needs"]
    assert sgit(root, "rev-parse", "main") == main_before and sgit(root, "show", "main:tool.txt").strip() == "trunk edit"
    assert not (root / ".git" / "worktrees" / "T-0007" / "MERGE_HEAD").exists()
    assert sgit(wt, "status", "--porcelain") == "" and not _events(root, "landed", "T-0007")


def test_record_report_written_in_worktree_fails_the_landing_by_name(tmp_path: Path, monkeypatch):
    """T-0120 (c): the task's own report.md was written in the worktree, not the live root. It is never
    dropped: one land_failed (records) names report.md and the one action, the trunk is untouched; once the
    branch takes main's report.md back, the next tick lands."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    rel = f"tasks/{Store(root).get('T-0007').dir.name}/report.md"
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    (wt / rel).write_text("# Report\n\nwritten in the worktree by mistake\n", encoding="utf-8")
    main_before = sgit(root, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"] and Store(root).get("T-0007").status == "ready", res      # back to the scriber (2026-10-14)
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and "records" in ev[0]["note"] and "report.md" in ev[0]["note"] and "live root" in ev[0]["note"], ev
    assert not any("landing failed" in n for n in res["needs"]), res["needs"]
    assert sgit(root, "rev-parse", "main") == main_before and not _events(root, "landed", "T-0007")
    # the scriber moved the text to the live root; the branch takes main's copy back, resubmits -> lands
    sgit(wt, "checkout", "main", "--", rel)
    _resubmit_fixed(root)
    res2 = _tick(root)
    assert Store(root).get("T-0007").status == "merged", res2["needs"]
    assert sgit(root, "show", "main:tool.txt").strip() == "ok" and len(_events(root, "land_failed", "T-0007")) == 1


def test_land_lock_held_makes_a_second_landing_busy(tmp_path: Path, monkeypatch):
    """T-0120 (from T-0119's review): while one landing of T-0007 holds .scr/land-T-0007.lock, a second
    land_scriptorium returns busy and touches nothing; a lock whose holder is dead is taken over, and a
    landing releases its lock."""
    from scr.ops import _LandLock, land_scriptorium
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    status, head, main_before = sgit(wt, "status", "--porcelain"), sgit(wt, "rev-parse", "HEAD"), sgit(root, "rev-parse", "main")
    lock = _LandLock(root, "T-0007")
    assert lock.acquire() and lock.path == root / ".scr" / "land-T-0007.lock"
    try:
        r = land_scriptorium(root, "T-0007")
    finally:
        lock.release()
    assert not r["ok"] and r["reason"] == "busy" and any("FAILED (busy)" in l for l in r["out"]), r
    assert sgit(wt, "status", "--porcelain") == status and sgit(wt, "rev-parse", "HEAD") == head
    assert sgit(root, "rev-parse", "main") == main_before and Store(root).get("T-0007").status == "accepted"
    assert not lock.path.exists()
    # a dead holder's lock is stale: taken over, the landing runs, the lock is gone afterwards
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
    lock.path.write_text(f"{p.pid} 0\n", encoding="utf-8")
    r2 = land_scriptorium(root, "T-0007")
    assert r2["ok"], r2["out"]
    assert Store(root).get("T-0007").status == "merged" and not lock.path.exists()


def test_land_lock_older_than_the_journal_stale_limit_is_taken_over(tmp_path: Path):
    """A lock held by a live pid is busy until it is older than LOCK_STALE_S (the holder's heartbeat keeps
    a real landing fresh); then it is stale."""
    from scr.journal import LOCK_STALE_S
    from scr.ops import _LandLock
    lock = _LandLock(tmp_path, "T-0007")
    lock.path.parent.mkdir(parents=True)
    lock.path.write_text(f"{os.getpid()} 0\n", encoding="utf-8")
    assert not lock.stale() and not _LandLock(tmp_path, "T-0007").acquire()
    old = lock.path.stat().st_mtime - LOCK_STALE_S - 10
    os.utime(lock.path, (old, old))
    assert lock.stale() and lock.acquire()
    lock.release()
    assert not lock.path.exists()


# ---- T-0119: `scr land <id>` routes a scriptorium task to land_scriptorium ---------------------------------
# Until T-0119 cmd_land and land_all_submitted called the QI land() for every id: on a scriptorium task that
# pushed task/<id> to origin and opened a PR on the scriptorium repo. ops.land_any decides, by repo identity.
def test_scr_land_scriptorium_task_lands_like_the_watcher_tick(tmp_path: Path, monkeypatch):
    """`scr land T-0007` on the accepted scriptorium task: committed, trunk merged in, tests, merged into the
    trunk, `merged` with one landed event and a log.md line naming the trunk commit — and nothing pushed,
    no PR (origin untouched). --merge is accepted and ignored with one line."""
    root, origin, wt = _land_world(tmp_path, monkeypatch)
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    origin_before = sgit(origin, "rev-parse", "main")
    r = sscr(root, "--actor", "omri", "land", "T-0007", "--merge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[0] == "scriptorium task: --merge is implied, --no-pr/--draft do not apply", r.stdout
    assert "(scriptorium)" in r.stdout and "pushed" not in r.stdout and "PR" not in r.stdout, r.stdout
    t = Store(root).get("T-0007")
    assert t.status == "merged"
    assert sgit(root, "show", "main:tool.txt").strip() == "ok" and sgit(root, "merge-base", "--is-ancestor", "task/T-0007", "main") == ""
    subject = sgit(root, "log", "-1", "--format=%s", "main")
    assert subject.startswith("[land] T-0007:") or subject.startswith("[T-0007]"), subject
    ev = _events(root, "landed", "T-0007")
    assert len(ev) == 1 and "task/T-0007 -> main @" in ev[0]["note"], ev
    sha = ev[0]["note"].split("@")[1].split()[0]
    assert sgit(root, "rev-parse", "--short", "main") == sha
    assert f"landed: task/T-0007 -> main @ {sha}" in (t.dir / "log.md").read_text(encoding="utf-8")
    # the QI path's traces are absent: origin's main unmoved, no task branch on origin, no PR body written
    assert sgit(origin, "rev-parse", "main") == origin_before
    assert sgit(origin, "branch", "--list", "task/T-0007") == "" and not (t.dir / "pr-body.md").exists()
    assert not _events(root, "land_failed", "T-0007") and not _events(root, "pr", "T-0007")


def test_scr_land_dry_run_scriptorium_changes_nothing(tmp_path: Path, monkeypatch):
    """`scr land T-0007 --dry-run` prints the plan (branch, trunk, dirty-file count, containment) and stops
    before step 1: trunk sha, task status, branch tip and the worktree's dirty file are as they were."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    main_before, tip_before, dirty_before = sgit(root, "rev-parse", "main"), sgit(wt, "rev-parse", "HEAD"), sgit(wt, "status", "--porcelain")
    r = sscr(root, "--actor", "omri", "land", "T-0007", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "task/T-0007 -> main (scriptorium, dry run" in r.stdout and "1 uncommitted file(s)" in r.stdout, r.stdout
    assert "would merge main into task/T-0007" in r.stdout and "check.py" in r.stdout, r.stdout
    assert sgit(root, "rev-parse", "main") == main_before and sgit(wt, "rev-parse", "HEAD") == tip_before
    assert sgit(wt, "status", "--porcelain") == dirty_before != ""
    assert Store(root).get("T-0007").status == "accepted" and not _events(root, "landed", "T-0007")


def test_scr_land_qi_task_still_reaches_ops_land(tmp_path: Path, monkeypatch):
    """A quantum-insertion task in the same world goes to ops.land with the QI flags; a task naming an
    unregistered workload falls back to the first entry (QI) as workload_for does; land_all_submitted routes
    the accepted scriptorium task to land_scriptorium, never to land()."""
    import scr.ops as ops
    from scr.cli import main
    root, _, wt = _land_world(tmp_path, monkeypatch)
    sscr(root, "--quiet", "task", "new", "QI", "--id", "T-0011", "--workload", "quantum-insertion", "--files", "Foo.lean", "--origin", "omri")
    calls: list[tuple] = []
    monkeypatch.setattr(ops, "land", lambda root, tid, **kw: calls.append((tid, kw)) or {"ok": True, "url": None, "out": [f"fake land {tid}"]})
    main(["land", "T-0011", "--root", str(root), "--no-pr", "--merge"])
    assert calls == [("T-0011", {"no_pr": True, "draft": False, "dry_run": False, "merge": True})], calls
    store = Store(root)
    t = store.get("T-0011")
    t.rec["workload"] = "nonesuch"
    assert not ops.is_scriptorium_task(root, store, t) and ops.is_scriptorium_task(root, store, store.get("T-0007"))
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    out = ops.land_all_submitted(root)
    assert len(calls) == 1 and Store(root).get("T-0007").status == "merged", out
    assert any("(scriptorium)" in l for l in out) and len(_events(root, "landed", "T-0007")) == 1, out


def test_scr_land_scriptorium_refuses_submitted_and_names_the_next_step(tmp_path: Path, monkeypatch):
    """The hand path is post-acceptance: a submitted scriptorium task is refused before anything moves. A
    landing failure exits 1 with the watcher's reason (tests) and the next action on one line, and raises
    no land_failed event (that event is the watcher's)."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (wt / "tool.txt").write_text("broken\n", encoding="utf-8")
    main_before = sgit(root, "rev-parse", "main")
    r = sscr(root, "--actor", "omri", "land", "T-0007", check=False)
    assert r.returncode == 1, r.stdout + r.stderr
    last = r.stdout.strip().splitlines()[-1]
    assert last.startswith("T-0007: NOT landed (tests) — next: fix task/T-0007") and "scr land T-0007" in last, r.stdout
    assert sgit(root, "rev-parse", "main") == main_before and Store(root).get("T-0007").status == "accepted"
    assert not _events(root, "land_failed", "T-0007")
    t = Store(root).get("T-0007")
    t.rec["status"] = "submitted"; t.save()                     # accepted -> submitted is no transition: set the record
    r = sscr(root, "--actor", "omri", "land", "T-0007", check=False)
    assert r.returncode == 2 and "T-0007 is submitted, not accepted — write the Decision first (T-0081)" in r.stderr, r.stdout + r.stderr
    assert sgit(root, "rev-parse", "main") == main_before and Store(root).get("T-0007").status == "submitted"


# ---- T-0154: `scr land <id>` on a checkout hotfix (accepted scriptorium task, no worktree) -----------------
# land_any sent it to land_scriptorium, which raised `worktree missing`; the watcher records it merged.
def _hotfix(root: Path, tid: str = "T-0008", status: str = "accepted") -> None:
    """The hotfix test's task: workload scriptorium, never spawned, walked to `status` (review first)."""
    sscr(root, "--quiet", "task", "new", "Hotfix", "--id", tid, "--workload", "scriptorium", "--files", "scr/x.py", "--origin", "omri")
    for st in ("triaged", "ready", "dispatched", "in_progress", "submitted"):
        sscr(root, "--quiet", "--actor", "s", "task", "set", tid, st)
    if status == "accepted":
        (Store(root).get(tid).dir / "review.md").write_text(accepted_review(), encoding="utf-8")   # T-0081
        sscr(root, "--quiet", "--actor", "s", "task", "set", tid, "accepted")


def _merged_states(root: Path, tid: str) -> list[dict]:
    from scr.events import EventLog
    return [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "state" and e.get("task") == tid and e.get("to") == "merged"]


def test_scr_land_hotfix_no_worktree_is_merged_like_the_watcher(tmp_path: Path, monkeypatch):
    """`scr land T-0008` on the hotfix: rc 0, merged, one state event with the watcher's note and actor, the
    trunk untouched, no landed event — the record test_accepted_hotfix_without_worktree_is_merged_without_a_merge
    pins for the watcher. The note is one constant, and it is the watcher's word for word."""
    import scr.ops as ops
    root, _, _ = _land_world(tmp_path, monkeypatch)
    _hotfix(root)
    assert not (tmp_path / "scriptorium-worktrees" / "T-0008").exists()
    main_before = sgit(root, "rev-parse", "main")
    r = sscr(root, "--actor", "omri", "land", "T-0008")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "worktree missing" not in r.stdout + r.stderr and "T-0008 marked merged" in r.stdout, r.stdout
    assert Store(root).get("T-0008").status == "merged"
    assert sgit(root, "rev-parse", "main") == main_before and not _events(root, "landed", "T-0008")
    st = _merged_states(root, "T-0008")
    assert len(st) == 1 and "already on main (journal)" in st[0]["note"], st
    assert st[0]["note"] == ops.HOTFIX_MERGED_NOTE and st[0]["actor"] == "steward:T-0008" and st[0]["from"] == "accepted", st
    assert "change already on main (journal)" not in (REPO / "scr" / "watch.py").read_text(encoding="utf-8")   # T-0158: one owner, ops
    assert Store(root).get("T-0007").status == "accepted"                                       # the spawned task is not touched


def test_scr_land_hotfix_no_worktree_dry_run_changes_nothing(tmp_path: Path, monkeypatch):
    """`scr land T-0008 --dry-run`: rc 0, the plan says `no worktree`, and nothing moved (status, trunk, events)."""
    root, _, _ = _land_world(tmp_path, monkeypatch)
    _hotfix(root)
    main_before = sgit(root, "rev-parse", "main")
    r = sscr(root, "--actor", "omri", "land", "T-0008", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no worktree: record merged only (change already on main via the journal)" in r.stdout and "dry run" in r.stdout, r.stdout
    assert Store(root).get("T-0008").status == "accepted" and not _merged_states(root, "T-0008")
    assert sgit(root, "rev-parse", "main") == main_before and not _events(root, "landed", "T-0008")


def test_scr_land_hotfix_no_worktree_submitted_still_refuses(tmp_path: Path, monkeypatch):
    """No worktree does not skip the Decision: a submitted hotfix is refused as T-0119 made it, dry run too."""
    root, _, _ = _land_world(tmp_path, monkeypatch)
    _hotfix(root, status="submitted")
    for extra in ([], ["--dry-run"]):
        r = sscr(root, "--actor", "omri", "land", "T-0008", *extra, check=False)
        assert r.returncode == 2 and "T-0008 is submitted, not accepted — write the Decision first (T-0081)" in r.stderr, r.stdout + r.stderr
    assert Store(root).get("T-0008").status == "submitted" and not _merged_states(root, "T-0008")


# ---- T-0158: one owner for the no-worktree landing; a lost worktree is refused, not merged ----------------
# land_any and the watcher both recorded "no worktree" as a merged hotfix. A spawned task whose worktree was
# removed while task/<id> still carries commits not on the trunk lost its work that way; now ops.land_no_worktree
# decides for both callers: merged only with no task/<id> or task/<id> already on the trunk, else lost_worktree.
def _lost_worktree(tmp_path: Path, monkeypatch, ahead: bool = True) -> tuple[Path, str]:
    """_land_world's T-0007 with (ahead) one commit on task/T-0007 that is not on main, then its worktree
    removed. Returns (root, main sha)."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    if ahead:
        (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
        sgit(wt, "commit", "-q", "-am", "the scriber's work")
    sgit(root, "worktree", "remove", "--force", str(wt))
    assert not wt.exists() and sgit(root, "rev-parse", "--verify", "task/T-0007")
    on_main = subprocess.run(["git", "merge-base", "--is-ancestor", "task/T-0007", "main"], cwd=root).returncode == 0
    assert on_main is not ahead
    return root, sgit(root, "rev-parse", "main")


def _no_merge(root: Path, main_before: str) -> None:
    assert Store(root).get("T-0007").status == "accepted" and not _merged_states(root, "T-0007")
    assert sgit(root, "rev-parse", "main") == main_before and not _events(root, "landed", "T-0007")


def test_scr_land_lost_worktree_branch_not_on_main_is_refused(tmp_path: Path, monkeypatch):
    """`scr land T-0007` with the worktree gone and task/T-0007 ahead of main: rc 1, the reason and the one
    next action, the task still accepted, no merged event, the trunk unmoved. `--dry-run` says the same."""
    root, main_before = _lost_worktree(tmp_path, monkeypatch)
    r = sscr(root, "--actor", "omri", "land", "T-0007", "--dry-run", check=False)
    assert r.returncode == 1 and "would refuse" in r.stdout and "not on main" in r.stdout and "dry run" in r.stdout, r.stdout + r.stderr
    _no_merge(root, main_before)
    r = sscr(root, "--actor", "omri", "land", "T-0007", check=False)
    assert r.returncode == 1 and "not on main" in r.stdout, r.stdout + r.stderr
    assert "FAILED (lost_worktree): worktree missing, branch task/T-0007 not on main" in r.stdout, r.stdout
    nxt = [l for l in r.stdout.splitlines() if "NOT landed (lost_worktree)" in l]
    assert len(nxt) == 1 and "git worktree add" in nxt[0] and "task/T-0007" in nxt[0] and "`scr land T-0007` again" in nxt[0], r.stdout
    _no_merge(root, main_before)


def test_watcher_lost_worktree_branch_not_on_main_stays_accepted_one_land_failed(tmp_path: Path, monkeypatch):
    """Same world, the watcher: accepted after the tick, ONE land_failed event, a NEEDS-YOU line naming the
    reason; the second tick adds a NEEDS-YOU line but no event and still does not record merged."""
    root, main_before = _lost_worktree(tmp_path, monkeypatch)
    for n in (1, 2):
        res = _tick(root)
        _no_merge(root, main_before)
        ev = _events(root, "land_failed", "T-0007")
        assert len(ev) == 1 and "lost_worktree" in ev[0]["note"] and "not on main" in ev[0]["note"], (n, ev)
        need = [l for l in res["needs"] if l.startswith("T-0007 landing failed (lost_worktree)")]
        assert len(need) == 1 and "git worktree add" in need[0], (n, res["needs"])


def test_scr_land_no_worktree_branch_already_on_main_is_merged(tmp_path: Path, monkeypatch):
    """task/T-0007 exists but is contained in main (nothing to lose): `scr land` records it merged exactly as
    a hotfix — HOTFIX_MERGED_NOTE, actor steward:T-0007, no merge, no landed event."""
    import scr.ops as ops
    root, main_before = _lost_worktree(tmp_path, monkeypatch, ahead=False)
    r = sscr(root, "--actor", "omri", "land", "T-0007", "--dry-run")
    assert "record merged only" in r.stdout and "would refuse" not in r.stdout, r.stdout
    r = sscr(root, "--actor", "omri", "land", "T-0007")
    assert r.returncode == 0 and "T-0007 marked merged" in r.stdout, r.stdout + r.stderr
    st = _merged_states(root, "T-0007")
    assert len(st) == 1 and st[0]["note"] == ops.HOTFIX_MERGED_NOTE and st[0]["actor"] == "steward:T-0007", st
    assert sgit(root, "rev-parse", "main") == main_before and not _events(root, "landed", "T-0007")


def test_watcher_no_worktree_branch_already_on_main_is_merged_like_scr_land(tmp_path: Path, monkeypatch):
    """The watcher on the same world: merged with the same record `scr land` writes — note, actor, and the
    cascade actor of the dependent it unblocks (the old `scr task set` subprocess cascaded as `scr`) — and
    queues/STATE.json follow the merge (the refresh the subprocess did)."""
    import scr.ops as ops
    root, main_before = _lost_worktree(tmp_path, monkeypatch, ahead=False)
    sscr(root, "--quiet", "task", "new", "After", "--id", "T-0009", "--workload", "scriptorium", "--files", "other.txt", "--origin", "omri", "--blocked-by", "T-0007")
    sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0009", "triaged")
    res = _tick(root)
    assert res["ok"] and Store(root).get("T-0007").status == "merged", res
    st = _merged_states(root, "T-0007")
    assert len(st) == 1 and st[0]["note"] == ops.HOTFIX_MERGED_NOTE and st[0]["actor"] == "steward:T-0007", st
    from scr.events import EventLog
    up = [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "state" and e.get("task") == "T-0009" and e.get("to") == "ready"]
    assert len(up) == 1 and up[0]["actor"] == "steward:T-0007" and up[0]["note"] == "unblocked by T-0007", up
    from scr.state import State
    snap = State(root).read_snapshot()
    assert snap["state_root"] == State(root).compute()["state_root"], "STATE.json does not follow the merge"
    assert sgit(root, "rev-parse", "main") == main_before and not _events(root, "landed", "T-0007") and not _events(root, "land_failed", "T-0007")


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


def _exhaust_review_retries(root: Path, tid: str = "T-0001") -> None:
    """2026-10-14: a stale review is re-run twice before the task is escalated; these tests are about the
    escalation itself, so the two re-runs are recorded as already spent (old stamps: the clock is the submission)."""
    p = root / ".scr" / "watch.state.txt"
    p.parent.mkdir(exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"review_retry:{tid}:1:20200101T000000Z\nreview_retry:{tid}:2:20200101T000000Z\n")


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
    # 2026-10-14: the stale review.md is archived and the review re-run; no card for Omri
    d = Store(root).get("T-0001").dir
    assert not (d / "review.md").exists() and list(d.glob("review-stale-1-*.md")), sorted(x.name for x in d.iterdir())
    assert len([e for e in _events(root, "review_retry", "T-0001")]) == 1
    assert not [n for n in res["needs"] if n.startswith("T-0001 ")], res["needs"]
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
    _exhaust_review_retries(root)
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
    _exhaust_review_retries(root)
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


# ---- T-0122: dispatch_timeout_minutes is enforced ---------------------------------------------------------
# limits.yaml carried the key and nothing read it (T-0092 review, item 2): a dispatch whose launch failed after
# `scr dispatch` sat in `dispatched` (a slot status) forever. `dispatch_stale` is a pure function of the log and
# `now`; `dispatch_checks` runs it on a tick with auto_decide on, before the stuck checks and auto-dispatch, so
# the slot is refilled on the same tick. The dispatch is backdated by rewriting its event's `ts` and re-chaining
# the log (then `scr snapshot`, so `scr verify` stays green), which keeps the default limit in force.
import json  # noqa: E402

from scr.canon import ZERO, canon  # noqa: E402
from scr.events import event_hash  # noqa: E402
from scr.watch import DISPATCH_TIMEOUT_MINUTES, dispatch_stale  # noqa: E402


def _dispatched(root: Path, tid: str = "T-0001") -> None:
    scr(root, "--quiet", "--actor", "prioritizer", "task", "set", tid, "dispatched")


def _launched(root: Path, tid: str = "T-0001", pid: int = 4242, created: int | None = None) -> None:
    """The launcher's `launch` event, pid recorded in `data` as scr/launch.py writes it (`created` only when
    given: without it the event has the pre-T-0147 shape)."""
    data = {"pid": pid, "transcript": "session-20260923-000000.txt"}
    if created is not None:
        data["created"] = created
    scr(root, "--quiet", "--actor", f"steward:{tid}", "event", "system", "--name", "launch", "--task", tid,
        "--note", f"headless pid {pid}", "--data", json.dumps(data))


def _backdate(root: Path, tid: str, to: str, minutes: float) -> None:
    """Move the newest `to` state event of `tid` `minutes` into the past and re-chain every later event."""
    p = root / "events" / "events.jsonl"
    evs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    i = max(i for i, e in enumerate(evs) if e.get("kind") == "state" and e.get("task") == tid and e.get("to") == to)
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0)
    evs[i]["ts"] = ts.isoformat().replace("+00:00", "Z")
    prev = evs[i - 1]["h"] if i else ZERO
    for e in evs[i:]:
        e["prev"] = prev
        e["h"] = prev = event_hash(e)
    p.write_text("\n".join(canon(e) for e in evs) + "\n", encoding="utf-8")
    scr(root, "--quiet", "snapshot")


def _state_event_count(root: Path, tid: str) -> int:
    return len(_state_events(root, tid))


def test_dispatch_stale_is_a_pure_function_of_the_log_and_time():
    old = (NOW - timedelta(minutes=45)).isoformat().replace("+00:00", "Z")
    young = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    disp = {"kind": "state", "task": "T-1", "from": "ready", "to": "dispatched", "ts": old}
    launch = {"kind": "system", "name": "launch", "task": "T-1", "ts": old}
    other = {"kind": "system", "name": "launch", "task": "T-2", "ts": old}
    row = dispatch_stale([disp], "T-1", 30, NOW)
    assert row == {"since": NOW - timedelta(minutes=45), "age_min": 45.0}
    assert dispatch_stale([{**disp, "ts": young}], "T-1", 30, NOW) is None                   # not yet
    assert dispatch_stale([disp], "T-1", 45, NOW) is None                                    # at the limit: not over it
    assert dispatch_stale([disp, launch], "T-1", 30, NOW) is None                            # the launcher has it
    assert dispatch_stale([disp, other], "T-1", 30, NOW)["age_min"] == 45.0                  # another task's launch
    assert dispatch_stale([], "T-1", 30, NOW) is None and dispatch_stale([launch], "T-1", 30, NOW) is None
    assert dispatch_stale([{**disp, "ts": "not a time"}], "T-1", 30, NOW) is None            # undatable: never early
    # only the newest dispatch counts: the first run's launch event is history once the task is dispatched again
    run1 = [disp, launch, {"kind": "state", "task": "T-1", "from": "dispatched", "to": "in_progress", "ts": old},
            {"kind": "state", "task": "T-1", "from": "in_progress", "to": "returned", "ts": old},
            {"kind": "state", "task": "T-1", "from": "returned", "to": "ready", "ts": old}]
    assert dispatch_stale(run1 + [disp], "T-1", 30, NOW)["age_min"] == 45.0
    assert dispatch_stale(run1 + [disp, launch], "T-1", 30, NOW) is None
    assert dispatch_stale(run1 + [{**disp, "ts": young}], "T-1", 30, NOW) is None


def test_dispatched_without_a_session_times_out_to_ready(world, monkeypatch):
    """DoD 1: dispatched, the state event older than dispatch_timeout_minutes, no launch event, auto_decide on
    -> after one tick() the task is ready (actor steward:<id>), exactly one dispatch_timeout event; a second
    tick adds no second event."""
    root, _, _ = world
    assert float(yaml.safe_load((root / "config" / "limits.yaml").read_text(encoding="utf-8"))["dispatch_timeout_minutes"]) == DISPATCH_TIMEOUT_MINUTES == 30
    _dispatched(root)
    _backdate(root, "T-0001", "dispatched", 45)
    assert scr(root, "verify", "--quiet").returncode == 0
    n_before = _state_event_count(root, "T-0001")
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"], res
    assert Store(root).get("T-0001").status == "ready"
    new = _state_events(root, "T-0001")[n_before:]
    assert len(new) == 1 and (new[0]["from"], new[0]["to"], new[0]["actor"]) == ("dispatched", "ready", "steward:T-0001"), new
    assert new[0]["note"].startswith("dispatch timed out: waited 45.0 min in dispatched (dispatch_timeout_minutes 30)"), new[0]["note"]
    ev = _events(root, "dispatch_timeout", "T-0001")
    assert len(ev) == 1 and ev[0]["actor"] == "steward:T-0001", ev
    assert ev[0]["note"] == "waited 45.0 min in dispatched (dispatch_timeout_minutes 30); no launch event; redispatch is automatic"
    assert res["timed_out"] == [{"task": "T-0001", "since": res["timed_out"][0]["since"], "age_min": 45.0}] and not res["escalated"]
    assert next(r for r in res["rows"] if r["task"] == "T-0001")["flags"].endswith("DISPATCH-TIMEOUT")
    assert "T-0001 dispatch timed out -> ready" in (root / LOG).read_text(encoding="utf-8")
    assert scr(root, "verify", "--quiet").returncode == 0
    res2 = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res2["ok"] and not res2["timed_out"]
    assert Store(root).get("T-0001").status == "ready" and len(_events(root, "dispatch_timeout", "T-0001")) == 1
    assert _state_event_count(root, "T-0001") == n_before + 1


def test_dispatched_younger_than_the_limit_is_left_alone(world, monkeypatch):
    """DoD 2: the same task younger than the limit -> still dispatched, no event; with auto_decide off an old
    one is held too (the check is an automatic transition like the others)."""
    root, _, _ = world
    _dispatched(root)
    _backdate(root, "T-0001", "dispatched", 20)
    calls = _spy_scr(monkeypatch)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and not res["timed_out"], res
    assert Store(root).get("T-0001").status == "dispatched" and not _events(root, "dispatch_timeout", "T-0001")
    assert not any(a[:2] == ("task", "set") for _, a in calls), calls
    _backdate(root, "T-0001", "dispatched", 45)
    res = Watcher(root, quiet=True).tick()                                                  # auto_decide off: held
    assert Store(root).get("T-0001").status == "dispatched" and not res["timed_out"] and not _events(root, "dispatch_timeout", "T-0001")


def test_dispatched_with_a_launch_event_is_left_to_the_launcher(world, monkeypatch):
    """DoD 3: a dispatched task with a launch event (pid recorded) older than the limit -> still dispatched, no
    event: the launcher owns the in_progress transition, and a dead launched session is T-0063's business."""
    root, _, _ = world
    _dispatched(root)
    _launched(root)
    _backdate(root, "T-0001", "dispatched", 45)
    calls = _spy_scr(monkeypatch)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and not res["timed_out"], res
    assert Store(root).get("T-0001").status == "dispatched" and not _events(root, "dispatch_timeout", "T-0001")
    assert not any(a[:2] == ("task", "set") for _, a in calls), calls


def test_dispatch_timeout_frees_the_slot_for_the_next_ready_task(world, monkeypatch):
    """DoD 4: cap 1, the timed-out task plus one ready task -> on the same tick the ready task is dispatched
    (and launched: start_claude is mocked), after the timeout; the timed-out task is ready, not dispatched."""
    root, _, _ = world
    _set_limit(root, "concurrency", 1)
    _dispatched(root)
    _backdate(root, "T-0001", "dispatched", 45)
    _ready_task(root, "T-0002", "QuantumInsertion/B.lean")
    # T-0127: the bounced dispatch no longer costs T-0001 an attempt, so w_retry no longer ranks it below T-0002 and
    # the two tie; T-0002 is made the better-ranked ready task, the premise of this test (the assertions are T-0122's)
    scr(root, "--quiet", "--actor", "omri", "task", "field", "T-0002", "priority.omri_weight", "1")
    assert "(running: 1, cap 1)" in scr(root, "dispatch", "--dry-run").stdout
    launches = _record_launches(monkeypatch)
    calls = _spy_scr(monkeypatch)
    res = Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()
    assert res["ok"] and res["timed_out"][0]["task"] == "T-0001", res
    assert Store(root).get("T-0001").status == "ready"
    assert Store(root).get("T-0002").status == "in_progress" and len(launches) == 1
    assert [e["to"] for e in _state_events(root, "T-0002")][-2:] == ["dispatched", "in_progress"]
    i_out = next(i for i, (_, a) in enumerate(calls) if a[:2] == ("task", "set") and a[2:4] == ("T-0001", "ready"))
    i_disp = next(i for i, (_, a) in enumerate(calls) if a[0] == "dispatch")
    assert i_out < i_disp, calls
    assert len(_events(root, "dispatch_timeout", "T-0001")) == 1


def test_dispatch_timeout_key_is_read_and_mirrored():
    src = (REPO / "scr" / "watch.py").read_text(encoding="utf-8")
    assert "dispatch_timeout_minutes" in src and "dispatch_timeout" in src
    limits = yaml.safe_load((CONFIG / "limits.yaml").read_text(encoding="utf-8"))
    assert float(limits["dispatch_timeout_minutes"]) == DISPATCH_TIMEOUT_MINUTES == 30
    # T-0127: the rounds limit, read by dispatch_rounds_needs, mirrored the same way
    assert "dispatch_timeout_rounds" in src
    from scr.watch import DISPATCH_TIMEOUT_ROUNDS
    assert int(limits["dispatch_timeout_rounds"]) == DISPATCH_TIMEOUT_ROUNDS == 3


# ---- T-0124: escalating a running task stops its session ---------------------------------------------------
# `_escalate` left the launched scriber running (T-0092 review, item 3): auto-dispatch refilled the slot on the
# same tick, so cap + 1 claude sessions ran, and the old one's `task set submitted` was later refused unexplained.
# Now an alive session_of pid is checkpoint-committed, then stopped with run.kill; no real process is touched:
# pid_alive and kill are replaced in scr.watch and every call recorded.
from scr.gitops import Git  # noqa: E402
from scr.ops import task_worktree, workload_for  # noqa: E402


def _stuck_running(root: Path, tid: str = "T-0001", pid: int = 4242, created: int | None = None) -> Path:
    """An in_progress task with a worktree, a launch event (pid recorded) and a blocking HELP line 4 h old."""
    _dispatched(root, tid)
    spawn(root, [tid], lake=False)
    scr(root, "--quiet", "--actor", f"steward:{tid}", "task", "set", tid, "in_progress")
    _launched(root, tid, pid, created)
    t = Store(root).get(tid)
    log = t.dir / "log.md"
    log.write_text(log.read_text(encoding="utf-8") + "- HELP[blocks: Phase C]: which lemma closes the goal?\n", encoding="utf-8")
    old = time.time() - 4 * 3600
    os.utime(log, (old, old))
    return task_worktree(workload_for(root, Store(root), t), t)


def _mock_session(monkeypatch, alive, kill_result=True, wt: Path | None = None) -> list[tuple]:
    """Replace pid_alive / kill / checkpoint in scr.watch; return the ordered calls. A live pid_alive also
    writes to the worktree, as a running scriber would, so a checkpoint made after it has something to commit."""
    import scr.watch as watch_mod
    calls: list[tuple] = []
    real_ckpt = watch_mod.checkpoint

    def fake_alive(pid):
        calls.append(("alive", pid))
        if alive and wt is not None:
            (wt / "QuantumInsertion" / "A.lean").write_text(f"theorem a : True := trivial -- {len(calls)}\n", encoding="utf-8")
        return alive

    def fake_kill(pid):
        calls.append(("kill", pid))
        if isinstance(kill_result, Exception):
            raise kill_result
        return kill_result

    def spy_ckpt(root, tid, **kw):
        c = real_ckpt(root, tid, **kw)
        calls.append(("checkpoint", tid, c.head if c else None))
        return c

    monkeypatch.setattr(watch_mod, "pid_alive", fake_alive)
    monkeypatch.setattr(watch_mod, "kill", fake_kill)
    monkeypatch.setattr(watch_mod, "checkpoint", spy_ckpt)
    return calls


def test_escalating_a_running_task_checkpoints_then_stops_its_session(world, monkeypatch):
    """DoD 2: pid recorded and alive, a stale blocking HELP line -> after one tick() escalated; the checkpoint
    made inside _escalate (it commits what the session wrote after the tick's own pass) precedes the one kill,
    of that pid, and the slot_freed note says the session was stopped after that checkpoint, on the branch."""
    root, _, _ = world
    wt = _stuck_running(root)
    calls = _mock_session(monkeypatch, alive=True, wt=wt)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and Store(root).get("T-0001").status == "escalated", res
    kills = [c for c in calls if c[0] == "kill"]
    assert kills == [("kill", 4242)], calls
    i_kill = calls.index(("kill", 4242))
    ckpts = [(i, c) for i, c in enumerate(calls) if c[0] == "checkpoint"]
    assert len(ckpts) == 2 and ckpts[-1][0] < i_kill, calls                  # the tick's pass, then _escalate's own
    sha = ckpts[-1][1][2]
    assert sha and sha != ckpts[0][1][2] and Git(wt).head().startswith(sha), calls   # a new commit: the work after the pass
    assert not Git(wt).dirty()
    ev = _events(root, "slot_freed", "T-0001")
    assert len(ev) == 1 and "session pid 4242 stopped after checkpoint " + sha in ev[0]["note"], ev
    assert "the work is on task/T-0001" in ev[0]["note"] and "left running" not in ev[0]["note"]
    last = _state_events(root, "T-0001")[-1]
    assert last["to"] == "escalated" and "stopped after checkpoint" in last["note"], last
    # one escalation per episode: a second tick neither escalates nor kills again
    Watcher(root, quiet=True, auto_decide=True).tick()
    assert [c for c in calls if c[0] == "kill"] == [("kill", 4242)] and len(_events(root, "slot_freed", "T-0001")) == 1


def test_escalating_a_task_whose_session_is_gone_kills_nothing(world, monkeypatch):
    """DoD 3: the same task with pid_alive False -> no kill, the note says the session was already gone. (On a
    whole tick the dead check returns such a task first, so the escalation pass is driven directly: the pid
    can die between the two.)"""
    root, _, _ = world
    _stuck_running(root)
    calls = _mock_session(monkeypatch, alive=False)
    w = Watcher(root, quiet=True, auto_decide=True)
    assert w.escalation_checks(datetime.now(timezone.utc), Store(root).load_all()) == 1
    assert Store(root).get("T-0001").status == "escalated"
    assert not [c for c in calls if c[0] in {"kill", "checkpoint"}], calls
    ev = _events(root, "slot_freed", "T-0001")
    assert len(ev) == 1 and "session pid 4242 was already gone" in ev[0]["note"], ev


def test_escalating_a_submitted_task_never_kills(world, monkeypatch):
    """DoD 4: a stuck `submitted` task has no scriber session (its review session is T-0075's): kill is never
    called, even with a launch event on record and every pid alive."""
    root, _, _ = world
    _set_limit(root, "review_stale_hours", 0)
    _submit(root)
    _exhaust_review_retries(root)
    _launched(root)
    calls = _mock_session(monkeypatch, alive=True)
    Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "escalated"
    assert not [c for c in calls if c[0] == "kill"], calls
    assert "stopped" not in _events(root, "slot_freed", "T-0001")[0]["note"]


@pytest.mark.parametrize("result", [False, OSError("taskkill missing")])
def test_escalation_does_not_depend_on_the_kill(world, monkeypatch, result):
    """DoD 5: kill returning False or raising -> the task is still escalated and the note says the stop failed,
    naming the pid."""
    root, _, _ = world
    wt = _stuck_running(root)
    calls = _mock_session(monkeypatch, alive=True, kill_result=result, wt=wt)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and Store(root).get("T-0001").status == "escalated", res
    assert [c for c in calls if c[0] == "kill"] == [("kill", 4242)]
    ev = _events(root, "slot_freed", "T-0001")
    assert len(ev) == 1 and "stopping session pid 4242 FAILED" in ev[0]["note"] and "may still be running" in ev[0]["note"], ev


@pytest.mark.skipif(os.name != "nt", reason="taskkill is the Windows path")
def test_run_kill_reports_a_refused_taskkill(monkeypatch):
    """run.kill's return value is what the note trusts: a non-zero taskkill exit (no such pid) is False."""
    import scr.run as run_mod

    class R:
        def __init__(self, rc):
            self.returncode = rc
    for rc, want in ((0, True), (128, False)):
        monkeypatch.setattr(run_mod, "subprocess", _Shim(subprocess, run=lambda *a, rc=rc, **k: R(rc)))
        assert run_mod.kill(99999) is want


# ---- T-0147: a reused pid is never stopped -------------------------------------------------------------------
# pid_alive counts a pid Windows reused for another process as alive, so a silently dead scriber's pid could be
# `taskkill /T /F`-ed by the escalation above. The launcher now records the process creation time (`created`), and
# session_dead / stop_session compare it with the live process's (run.process_created, replaced in scr.watch).
def _mock_created(monkeypatch, value) -> list[int]:
    """Replace process_created in scr.watch with a constant; return the pids it was asked about."""
    import scr.watch as watch_mod
    asked: list[int] = []
    monkeypatch.setattr(watch_mod, "process_created", lambda pid: (asked.append(pid), value)[1])
    return asked


def test_stop_session_skips_a_reused_pid(world, monkeypatch):
    """Launch recorded created X, the pid is alive but its process was created at Y != X, a stale blocking HELP ->
    escalated, kill never called (nor a checkpoint for a session that is gone), the note says the pid was reused.
    (On a whole tick the dead check returns such a task first, so the escalation pass is driven directly.)"""
    root, _, _ = world
    _stuck_running(root, created=132000000000000001)
    calls = _mock_session(monkeypatch, alive=True)
    asked = _mock_created(monkeypatch, 132000000000000002)
    w = Watcher(root, quiet=True, auto_decide=True)
    assert w.escalation_checks(datetime.now(timezone.utc), Store(root).load_all()) == 1
    assert Store(root).get("T-0001").status == "escalated"
    assert not [c for c in calls if c[0] in {"kill", "checkpoint"}], calls
    assert asked == [4242]
    ev = _events(root, "slot_freed", "T-0001")
    assert len(ev) == 1 and "session pid 4242 was already gone (pid reused by another process)" in ev[0]["note"], ev
    assert "stopped after" not in ev[0]["note"]


def test_stop_session_kills_when_creation_time_matches(world, monkeypatch):
    """The same with Y == X: it is the launched session, so T-0124's behaviour: one kill, of that pid."""
    root, _, _ = world
    wt = _stuck_running(root, created=132000000000000001)
    calls = _mock_session(monkeypatch, alive=True, wt=wt)
    _mock_created(monkeypatch, 132000000000000001)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and Store(root).get("T-0001").status == "escalated", res
    assert [c for c in calls if c[0] == "kill"] == [("kill", 4242)], calls
    assert "session pid 4242 stopped after checkpoint " in _events(root, "slot_freed", "T-0001")[0]["note"]


@pytest.mark.parametrize("live", [132000000000000002, None])
def test_stop_session_kills_on_an_old_launch_event(world, monkeypatch, live):
    """A launch event without `created` (recorded before T-0147): the pid is trusted exactly as today, whatever
    the live creation time is; the helper is not even asked."""
    root, _, _ = world
    wt = _stuck_running(root)
    calls = _mock_session(monkeypatch, alive=True, wt=wt)
    asked = _mock_created(monkeypatch, live)
    assert Watcher(root, quiet=True).session_of(Store(root).get("T-0001")).get("created") is None
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and Store(root).get("T-0001").status == "escalated", res
    assert [c for c in calls if c[0] == "kill"] == [("kill", 4242)], calls
    assert asked == []


def test_stop_session_kills_when_the_live_creation_time_is_unreadable(world, monkeypatch):
    """created recorded, but the live process's cannot be read (access denied): today's behaviour, one kill."""
    root, _, _ = world
    wt = _stuck_running(root, created=132000000000000001)
    calls = _mock_session(monkeypatch, alive=True, wt=wt)
    _mock_created(monkeypatch, None)
    Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "escalated"
    assert [c for c in calls if c[0] == "kill"] == [("kill", 4242)], calls


def test_session_dead_on_reused_pid(world, monkeypatch):
    """session_dead names the reuse when the creation times differ, and only then; on a tick the dead-session
    card fires (task returned) instead of the stale one, and nothing is killed."""
    import scr.watch as watch_mod
    root, _, _ = world
    monkeypatch.setattr(watch_mod, "pid_alive", lambda pid: True)
    t = Store(root).get("T-0001")
    sess = {"pid": 4242, "transcript": None, "created": 132000000000000001}
    _mock_created(monkeypatch, 132000000000000002)
    assert Watcher.session_dead(t, sess) == "pid 4242 was reused by another process"
    _mock_created(monkeypatch, 132000000000000001)
    assert Watcher.session_dead(t, sess) is None
    _mock_created(monkeypatch, None)
    assert Watcher.session_dead(t, sess) is None
    assert Watcher.session_dead(t, {"pid": 4242, "transcript": None}) is None

    _stuck_running(root, created=132000000000000001)
    calls = _mock_session(monkeypatch, alive=True)
    _mock_created(monkeypatch, 132000000000000002)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and Store(root).get("T-0001").status == "ready", res        # returned, then relaunched (2026-10-14)
    dead = _events(root, "session_dead", "T-0001")
    assert len(dead) == 1 and "pid 4242 was reused by another process" in dead[0]["note"], dead
    assert len(_events(root, "auto_fix", "T-0001")) == 1
    assert not _events(root, "stale", "T-0001") and not [c for c in calls if c[0] == "kill"], calls


def test_launch_records_the_process_creation_time(world, monkeypatch):
    """The headless launcher reads the creation time of the pid start_claude returned and puts it in the
    `launch` event's data next to pid and transcript; session_of hands it on."""
    import scr.launch as launch_mod
    from scr.launch import launch
    root, _, _ = world
    _record_launches(monkeypatch)
    monkeypatch.setattr(launch_mod, "claude_bin", lambda: FAKE_CLAUDE)
    asked: list[int] = []
    monkeypatch.setattr(launch_mod, "process_created", lambda pid: (asked.append(pid), 132000000000000007)[1])
    _dispatched(root)
    launch(root, ["T-0001"], mode="headless")
    assert asked == [4242]
    ev = _events(root, "launch", "T-0001")[-1]
    assert ev["data"]["pid"] == 4242 and ev["data"]["created"] == 132000000000000007, ev
    assert ev["data"]["transcript"].startswith("session-"), ev
    sess = Watcher(root, quiet=True).session_of(Store(root).get("T-0001"))
    assert sess["pid"] == 4242 and sess["created"] == 132000000000000007, sess


def test_process_created_identifies_a_real_child_process():
    """run.process_created on a real process: the same value on every read while it lives, None (or another
    value) once it has exited and been reaped, None for a pid that cannot exist."""
    import gc

    from scr.run import process_created
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        first = process_created(p.pid)
        assert first is not None and process_created(p.pid) == first
        assert process_created(os.getpid()) not in (None, first)
    finally:
        p.kill()
        p.wait()
    pid = p.pid
    del p
    gc.collect()                                   # the Popen's process handle closes: the pid is released
    for _ in range(40):                            # (the venv launcher's own handle goes a moment later)
        if process_created(pid) != first:
            break
        time.sleep(0.25)
    assert process_created(pid) != first
    assert process_created(0) is None and process_created(None) is None


# ---- T-0153: the watcher routes by ops.is_scriptorium_task (repo identity), not the `workload:` string --------
# The hand path (T-0119, land_any) and the watcher's land step, review launch and status flag share one decider.
def _rename_workload(root: Path, tid: str = "T-0007") -> None:
    """Register the checkout a second time, as `tooling` (`repo: .`), and point the task's record at that key."""
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text(encoding="utf-8"))
    wl["tooling"] = dict(wl["scriptorium"])
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(wl), encoding="utf-8")
    sscr(root, "--quiet", "--actor", "s", "task", "field", tid, "workload", "tooling")
    sgit(root, "add", "-A"); sgit(root, "commit", "-q", "-m", "[journal] second key for the checkout")


def test_watch_land_routes_by_is_scriptorium_task_repo_identity(tmp_path: Path, monkeypatch):
    """An accepted worktree task whose `workload:` is `tooling` (the checkout under another key) lands through
    land_scriptorium on the tick: landed event, a `[land]` commit on the trunk, and the QI land() (push/PR)
    is never called."""
    import scr.ops as ops
    import scr.watch as watch
    root, origin, wt = _land_world(tmp_path, monkeypatch)
    _rename_workload(root)
    store = Store(root)
    assert ops.is_scriptorium_task(root, store, store.get("T-0007"))
    calls: list[str] = []
    monkeypatch.setattr(watch, "land", lambda root, tid, **kw: calls.append(tid) or {"ok": False, "url": None, "out": ["QI land called"]})
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    origin_before = sgit(origin, "rev-parse", "main")
    res = _tick(root)
    assert res["ok"] and not calls, (calls, res)
    assert Store(root).get("T-0007").status == "merged", res["needs"]
    ev = _events(root, "landed", "T-0007")
    assert len(ev) == 1 and "task/T-0007 -> main @" in ev[0]["note"], ev
    assert sgit(root, "show", "main:tool.txt").strip() == "ok"
    assert any(s.startswith("[land] T-0007") for s in sgit(root, "log", "--format=%s", "main").splitlines())
    assert sgit(origin, "rev-parse", "main") == origin_before and sgit(origin, "branch", "--list", "task/T-0007") == ""


def test_watch_review_routes_by_is_scriptorium_task_repo_identity(tmp_path: Path, monkeypatch):
    """A submitted task under the renamed key gets the tooling review, not the Lean review brief; the same
    rule flags a worktree-less task under that key `tooling` rather than `no-worktree`."""
    import scr.watch as watch
    from scr.watch import Watcher
    root, _, _ = _land_world(tmp_path, monkeypatch)
    sscr(root, "--quiet", "task", "new", "Tooling 2", "--id", "T-0012", "--workload", "scriptorium", "--files", "tool.txt", "--origin", "omri")
    for st in ("triaged", "ready", "dispatched", "in_progress", "submitted"):
        sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0012", st)
    _rename_workload(root, "T-0012")
    tooling: list[str] = []
    lean: list[tuple] = []
    monkeypatch.setattr(Watcher, "start_tooling_review", lambda self, t: tooling.append(t.id))
    monkeypatch.setattr(watch, "start_claude", lambda *a, **kw: lean.append(a))
    res = Watcher(root, quiet=True, auto_review=True).tick()
    assert res["ok"] and "T-0012" in res["review"], res
    assert tooling == ["T-0012"] and not lean, (tooling, lean)
    # the status flag: a worktree-less task under `tooling` is a hotfix in the checkout
    sscr(root, "--quiet", "task", "new", "Hotfix", "--id", "T-0010", "--workload", "scriptorium", "--files", "scr/x.py", "--origin", "omri")
    for st in ("triaged", "ready", "dispatched", "in_progress"):
        sscr(root, "--quiet", "--actor", "s", "task", "set", "T-0010", st)
    _rename_workload(root, "T-0010")
    res = Watcher(root, quiet=True).tick()
    assert next(r for r in res["rows"] if r["task"] == "T-0010")["flags"] == "tooling"


# ---- T-0090: out-of-band push of needs-you cards -------------------------------------------------------------
import scr.notify as notify  # noqa: E402
import scr.ui as ui_mod  # noqa: E402


def _card(key: str, tid: str, title: str) -> dict:
    return {"key": key, "id": tid, "type": "system", "kind": "system", "title": title, "options": [], "text": "note"}


CARD_A = _card("events:T-0001:session_dead:2026-09-24T01:00:00Z", "T-0001", "scriber session died")
CARD_B = _card("decision:T-0002", "T-0002", "Pick a direction")
CARD_C = _card("chore:push-main", "push-main", "Push main to origin")


def _notify_cfg(root: Path, cfg: dict | None) -> None:
    """Write (or, with None, delete) config/notify.yaml and commit it, like _set_limit (config/ is a tooling path)."""
    p = root / "config" / "notify.yaml"
    if cfg is None:
        if p.exists():
            p.unlink()
    else:
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    sgit(root, "add", "-A", "config"); sgit(root, "commit", "-q", "--allow-empty", "-m", "[test] notify config")


def _fake_channel(monkeypatch, name: str = "toast", fail: bool = False) -> list[list[dict]]:
    calls: list[list[dict]] = []

    def fake(cards, ch, cfg):
        calls.append(list(cards))
        if fail:
            raise RuntimeError("smtp down")
        return "ok"
    monkeypatch.setitem(notify.CHANNELS, name, fake)
    return calls


def _cards(monkeypatch, cards: list[dict]) -> None:
    monkeypatch.setattr(ui_mod, "needs_cards", lambda root, tasks=None: list(cards))


def test_notify_pushes_each_new_card_once_and_never_an_acked_one(world, monkeypatch):
    """DoD: two new cards on tick 1 -> the channel is called once with both; the same cards on tick 2 -> not
    called; one card acked, one new -> called with the new one only. Every tick is a fresh Watcher, so the
    dedup state is the flags file, which survives a watcher restart."""
    root, _, _ = world
    _notify_cfg(root, {"toast": {"enabled": True}, "email": {"enabled": False}})
    calls = _fake_channel(monkeypatch)
    email_calls = _fake_channel(monkeypatch, "email")
    _cards(monkeypatch, [CARD_A, CARD_B])
    assert Watcher(root, quiet=True).tick()["ok"]
    assert calls == [[CARD_A, CARD_B]] and not email_calls
    assert {"notified:" + CARD_A["key"], "notified:" + CARD_B["key"]} <= _flags(root)
    Watcher(root, quiet=True).tick()
    assert len(calls) == 1
    scr(root, "--quiet", "--actor", "omri", "ack", CARD_B["key"])   # a decision card stays listed after an ack
    _cards(monkeypatch, [CARD_A, CARD_B, CARD_C])
    Watcher(root, quiet=True).tick()
    assert calls[1:] == [[CARD_C]]
    Watcher(root, quiet=True).tick()
    assert len(calls) == 2
    # a card that no longer stands leaves the flags file (it stays as long as the card list)
    _cards(monkeypatch, [CARD_C])
    Watcher(root, quiet=True).tick()
    assert "notified:" + CARD_A["key"] not in _flags(root) and "notified:" + CARD_C["key"] in _flags(root) and len(calls) == 2


def test_notify_channel_failure_is_a_log_line_not_a_crash(world, monkeypatch):
    root, _, _ = world
    _notify_cfg(root, {"toast": {"enabled": True}, "email": {"enabled": True}})
    toast_calls = _fake_channel(monkeypatch, "toast")
    _fake_channel(monkeypatch, "email", fail=True)
    _cards(monkeypatch, [CARD_A])
    res = Watcher(root, quiet=True).tick()
    assert res["ok"] and toast_calls == [[CARD_A]]
    log = (root / LOG).read_text(encoding="utf-8")
    assert "notify FAILED: email: RuntimeError: smtp down" in log and "notify toast: ok" in log
    # the card list itself failing is the same kind of line
    monkeypatch.setattr(ui_mod, "needs_cards", lambda root, tasks=None: 1 / 0)
    assert Watcher(root, quiet=True).tick()["ok"]
    assert "notify FAILED: ZeroDivisionError" in (root / LOG).read_text(encoding="utf-8")


@pytest.mark.parametrize("cfg", [None, {"toast": {"enabled": False}, "email": {"enabled": False}}, {}])
def test_notify_config_absent_or_all_off_calls_nothing(world, monkeypatch, cfg):
    root, _, _ = world
    _notify_cfg(root, cfg)
    calls = _fake_channel(monkeypatch) + _fake_channel(monkeypatch, "email")
    _cards(monkeypatch, [CARD_A, CARD_B])
    assert Watcher(root, quiet=True).tick()["ok"]
    assert not calls and not any(k.startswith("notified:") for k in _flags(root))
    assert "notify" not in (root / LOG).read_text(encoding="utf-8")


def test_toast_argv_names_the_card_title_and_task_id():
    card = _card("gate:T-0042", "T-0042", "Omri's gate: accept the <new> lemma & go")
    argv = notify.toast_argv([card, CARD_A])
    assert argv[0] == "powershell" and "-Command" in argv
    cmd = argv[-1]
    assert "T-0042" in cmd and "T-0001" in cmd and "scriber session died" in cmd
    # the title is XML-escaped and its quote doubled for the PowerShell single-quoted string
    assert "Omri''s gate: accept the &lt;new&gt; lemma &amp; go" in cmd
    assert "ToastNotificationManager" in cmd and notify.TOAST_APP_ID in cmd


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"), reason="needs Windows and powershell on PATH")
def test_toast_smoke_runs_the_real_script_through_powershell():
    """T-0172: toast() is skipped under pytest, so this is the one test that runs the WinRT script itself
    (dry: types, LoadXml, notifier and toast are built, `.Show` is not called: nothing reaches the screen).
    Deselect with -k "not toast_smoke"."""
    card = _card("chore:toast-smoke", "toast-smoke", "scr test suite: toast smoke (ignore)")
    argv = notify.toast_argv([card], shutil.which("powershell"), dry=True)
    assert ".Show(" not in argv[-1]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0 and not r.stderr.strip(), f"rc {r.returncode}: {r.stderr or r.stdout}"


def test_toast_without_windows_powershell_raises(monkeypatch):
    """T-0179: pwsh cannot load the WinRT types, so a box with only pwsh gets a clear error and no process."""
    monkeypatch.setattr(notify, "_under_pytest", lambda: False)
    monkeypatch.setattr(notify.shutil, "which", lambda name: None if name == "powershell" else rf"C:\fake\{name}.exe")

    def no_run(*a, **k):
        pytest.fail(f"subprocess.run called: {a}")
    monkeypatch.setattr(notify.subprocess, "run", no_run)
    with pytest.raises(RuntimeError, match=r"Windows PowerShell \(powershell\.exe\)") as e:
        notify.toast([CARD_A], {}, {})
    assert "pwsh" in str(e.value)


def test_email_password_comes_from_the_named_env_var_only(monkeypatch):
    """The message is built from the yaml; the channel refuses to run without the env var the yaml names."""
    ch = {"enabled": True, "host": "smtp.example", "from": "a@example", "to": ["b@example"], "user": "a", "password_env": "SCR_TEST_SMTP_PW"}
    msg = notify.email_message([CARD_A], ch, {})
    assert msg["To"] == "b@example" and CARD_A["key"] in msg.get_content() and "T-0001" in msg.get_content()
    monkeypatch.setattr(notify, "_under_pytest", lambda: False)
    monkeypatch.delenv("SCR_TEST_SMTP_PW", raising=False)
    with pytest.raises(RuntimeError, match="SCR_TEST_SMTP_PW"):
        notify.email([CARD_A], ch, {})
    live = yaml.safe_load((CONFIG / "notify.yaml").read_text(encoding="utf-8"))
    assert set(live["email"]) >= {"host", "port", "from", "to", "password_env"} and "password" not in live["email"]


def test_scr_notify_test_reports_every_channel(world, monkeypatch):
    root, _, _ = world
    _notify_cfg(root, {"toast": {"enabled": True}, "email": {"enabled": False}})
    out = scr(root, "notify", "--test").stdout
    assert "notify toast: skipped (pytest)" in out and "notify email: off" in out


# ---- T-0171: one bounded retry of a card whose push failed on every channel -------------------------------------
def _switch_channel(monkeypatch, name: str, state: dict) -> list[list[dict]]:
    """A fake channel that raises while state[name] is True; flip it between ticks."""
    calls: list[list[dict]] = []

    def fake(cards, ch, cfg):
        calls.append(list(cards))
        if state[name]:
            raise RuntimeError(f"{name} down")
        return "ok"
    monkeypatch.setitem(notify.CHANNELS, name, fake)
    return calls


def _push(root: Path, cards: list[dict], flags: set[str], now: float, acked: set[str] | None = None) -> list[str]:
    lines: list[str] = []
    notify.push(root, cards, flags, acked or set(), lines.append, now=now)
    return lines


def test_notify_failed_everywhere_is_retried_once_then_given_up(world, monkeypatch):
    """DoD: every channel raises on tick 1 -> sent once; tick 2 before retry_minutes -> no send; tick 3 after it
    (injected clock) -> sent exactly once more; that failing too -> one `notify GAVE UP:` line naming the key, and
    ticks 4 and 5 send nothing."""
    root, _, _ = world
    _notify_cfg(root, {"toast": {"enabled": True}, "email": {"enabled": True}, "retry_minutes": 10})
    down = {"toast": True, "email": True}
    toast_calls, email_calls = _switch_channel(monkeypatch, "toast", down), _switch_channel(monkeypatch, "email", down)
    flags: set[str] = set()
    t0 = 1_800_000_000
    log = _push(root, [CARD_A], flags, t0)
    assert toast_calls == [[CARD_A]] and email_calls == [[CARD_A]]
    assert flags == {f"notified-retry:{CARD_A['key']}:{t0}"}, flags
    assert "notify FAILED: toast: RuntimeError: toast down" in log and "notify FAILED: email: RuntimeError: email down" in log
    log += _push(root, [CARD_A], flags, t0 + 9 * 60 + 59)
    assert len(toast_calls) == 1 and len(email_calls) == 1
    log += _push(root, [CARD_A], flags, t0 + 10 * 60)
    assert toast_calls == [[CARD_A]] * 2 and email_calls == [[CARD_A]] * 2
    assert flags == {"notified:" + CARD_A["key"]}, flags
    for dt in (11, 60):
        log += _push(root, [CARD_A], flags, t0 + dt * 60)
    assert len(toast_calls) == 2 and len(email_calls) == 2
    gave_up = [ln for ln in log if ln.startswith("notify GAVE UP:")]
    assert gave_up == [f"notify GAVE UP: {CARD_A['key']}"], log


def test_notify_retry_that_reaches_a_channel_is_not_a_give_up(world, monkeypatch):
    root, _, _ = world
    _notify_cfg(root, {"toast": {"enabled": True}, "email": {"enabled": False}, "retry_minutes": 5})
    down = {"toast": True}
    calls = _switch_channel(monkeypatch, "toast", down)
    flags: set[str] = set()
    _push(root, [CARD_A], flags, 1000)
    down["toast"] = False
    log = _push(root, [CARD_A, CARD_B], flags, 1000 + 5 * 60)   # the retry rides with a new card: one channel call
    assert calls == [[CARD_A], [CARD_B, CARD_A]]
    assert flags == {"notified:" + CARD_A["key"], "notified:" + CARD_B["key"]}
    assert not any("GAVE UP" in ln for ln in log) and any(ln.startswith("notify: retrying 1 card(s)") for ln in log)
    _push(root, [CARD_A, CARD_B], flags, 1000 + 60 * 60)
    assert len(calls) == 2


def test_notify_one_channel_reached_is_never_retried(world, monkeypatch):
    """DoD: toast fails, email succeeds on tick 1 -> the card is `notified:` and never retried."""
    root, _, _ = world
    _notify_cfg(root, {"toast": {"enabled": True}, "email": {"enabled": True}})
    down = {"toast": True, "email": False}
    toast_calls, email_calls = _switch_channel(monkeypatch, "toast", down), _switch_channel(monkeypatch, "email", down)
    flags: set[str] = set()
    _push(root, [CARD_A], flags, 0)
    assert flags == {"notified:" + CARD_A["key"]}
    for dt in (10, 11, 120):   # retry_minutes absent from the yaml: the default 10
        _push(root, [CARD_A], flags, dt * 60)
    assert len(toast_calls) == 1 and len(email_calls) == 1


def test_notify_retry_skips_an_acked_card_and_a_gone_card_loses_its_flag(world, monkeypatch):
    """DoD: a card acked while it waits for its retry is not retried; a card that disappears loses its retry flag."""
    root, _, _ = world
    _notify_cfg(root, {"toast": {"enabled": True}, "email": {"enabled": False}})
    calls = _switch_channel(monkeypatch, "toast", {"toast": True})
    flags: set[str] = set()
    _push(root, [CARD_A, CARD_B], flags, 0)
    assert len(calls) == 1 and all(f.startswith("notified-retry:") for f in flags) and len(flags) == 2
    _push(root, [CARD_A, CARD_B], flags, 3600, acked={CARD_B["key"]})
    assert calls[1:] == [[CARD_A]]
    _push(root, [CARD_A, CARD_B], flags, 7200, acked={CARD_B["key"]})
    assert len(calls) == 2
    _push(root, [CARD_A], flags, 7300)                     # CARD_B no longer stands
    assert not any(CARD_B["key"] in f for f in flags) and flags == {"notified:" + CARD_A["key"]}
    _push(root, [CARD_C], flags, 7400)                     # a fresh failed card, then it disappears while waiting
    assert any(f.startswith("notified-retry:" + CARD_C["key"] + ":") for f in flags)
    _push(root, [], flags, 7500)
    assert flags == set() and len(calls) == 3


def test_notify_retry_flag_survives_a_watcher_restart(world, monkeypatch):
    """Through the Watcher: the retry flag is a line of the flags file; the next tick (a fresh Watcher) reads it
    back and does not resend before retry_minutes."""
    root, _, _ = world
    _notify_cfg(root, {"toast": {"enabled": True}, "email": {"enabled": False}, "retry_minutes": 10})
    calls = _switch_channel(monkeypatch, "toast", {"toast": True})
    _cards(monkeypatch, [CARD_A])
    assert Watcher(root, quiet=True).tick()["ok"]
    assert len(calls) == 1 and any(f.startswith("notified-retry:" + CARD_A["key"] + ":") for f in _flags(root))
    assert Watcher(root, quiet=True).tick()["ok"]
    assert len(calls) == 1


# ---- T-0146: a failed landing is a needs-you card while the task stays accepted ------------------------------
def _land_cards(root: Path, tid: str) -> list[dict]:
    return [c for c in ui_mod.needs_cards(root) if c["type"] == "system" and c.get("name") == "land_failed" and c["id"] == tid]


def _accepted(root: Path, tid: str = "T-0001") -> None:
    """The world's ready T-0001 through to accepted (review.md first, T-0081)."""
    for st in ("dispatched", "in_progress", "submitted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", tid, st)
    (Store(root).get(tid).dir / "review.md").write_text(accepted_review(), encoding="utf-8")
    scr(root, "--quiet", "--actor", "s", "task", "set", tid, "accepted")


def test_land_failed_conflict_is_one_card_with_the_note_until_acked(tmp_path: Path, monkeypatch):
    """DoD: an accepted task whose landing failed (the real watcher path, a conflict on tool.txt) -> exactly one
    land_failed needs card for it, carrying the event note and the next step; more ticks add no card; /api/state
    returns the same card; `scr ack <key>` dismisses it."""
    root, _, wt = _land_world(tmp_path, monkeypatch)
    (root / "tool.txt").write_text("trunk edit\n", encoding="utf-8")
    sgit(root, "add", "-A"); sgit(root, "commit", "-q", "-m", "[journal] the trunk moved on tool.txt")
    (wt / "tool.txt").write_text("ok\n", encoding="utf-8")
    (root / ".scr").mkdir(exist_ok=True)
    (root / ".scr" / "watch.state.txt").write_text("autofix:T-0007:land:1\nautofix:T-0007:land:2\n", encoding="utf-8")   # the two send-backs are spent (2026-10-14)
    _tick(root)
    ev = _events(root, "land_failed", "T-0007")
    assert len(ev) == 1 and Store(root).get("T-0007").status == "accepted"
    cards = _land_cards(root, "T-0007")
    assert len(cards) == 1, cards
    c = cards[0]
    assert ev[0]["note"] in c["text"] and "landing failed" in c["title"] and "not on main" in c["title"]
    assert "merge main into task/T-0007" in c["text"] and "task set T-0007 returned" in c["text"]
    _tick(root); _tick(root)                                                     # ticks: no second event, no second card
    assert [x["key"] for x in _land_cards(root, "T-0007")] == [c["key"]]
    assert [x["key"] for x in ui_mod.state(root)["needs"] if x.get("name") == "land_failed"] == [c["key"]]
    sscr(root, "--quiet", "--actor", "omri", "ack", c["key"])
    assert _land_cards(root, "T-0007") == []


def test_land_failed_card_goes_when_the_task_leaves_accepted_and_never_doubles(world):
    """DoD: two land_failed events on one accepted task -> ONE card (x2, both notes, the scope next step); the task
    returned -> no card, no ack; accepted again -> the old failures do not come back; a fresh failure does, alone."""
    root, _, _ = world
    _accepted(root)
    for note in ("FAILED (conflict): main does not merge into task/T-0001: QuantumInsertion/A.lean",
                 "FAILED (scope): task/T-0001 carries 1 path(s) outside T-0001's scope: state/STATE.json"):
        scr(root, "--quiet", "--actor", "steward:T-0001", "event", "system", "--name", "land_failed", "--task", "T-0001", "--note", note)
    cards = _land_cards(root, "T-0001")
    assert len(cards) == 1 and "×2" in cards[0]["title"], cards
    assert "(conflict)" in cards[0]["text"] and "(scope)" in cards[0]["text"] and "widen T-0001's scope.files" in cards[0]["text"]
    scr(root, "--quiet", "--actor", "omri", "task", "set", "T-0001", "returned", "--note", "rebase")
    assert _land_cards(root, "T-0001") == []
    for st in ("ready", "dispatched", "in_progress", "submitted", "accepted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    assert _land_cards(root, "T-0001") == []
    scr(root, "--quiet", "--actor", "steward:T-0001", "event", "system", "--name", "land_failed", "--task", "T-0001", "--note", "FAILED (tests): x")
    cards = _land_cards(root, "T-0001")
    assert len(cards) == 1 and cards[0]["text"].startswith("FAILED (tests): x") and "×" not in cards[0]["title"], cards


# ---- T-0173: every watcher NEEDS-YOU line is a needs-you card, live ----------------------------------------------
from scr.watch import NEEDS_FILE  # noqa: E402


def _watch_cards(root: Path, tid: str | None = None) -> list[dict]:
    return [c for c in ui_mod.needs_cards(root) if c["type"] == "watch" and (tid is None or c["id"] == tid)]


def _needs_file(root: Path) -> dict:
    return json.loads((root / NEEDS_FILE).read_text(encoding="utf-8"))


def _refuse_escalation(monkeypatch) -> None:
    real = Watcher.scr

    def scr_(self, actor, *args):
        if args[:2] == ("task", "set") and "escalated" in args:
            return None
        return real(self, actor, *args)

    monkeypatch.setattr(Watcher, "scr", scr_)


def test_watch_need_is_one_live_card_with_a_stable_key(world, monkeypatch):
    """DoD: a tick whose stuck task cannot be escalated (the `task set escalated` refused) leaves exactly one card
    naming the task and the line; a second tick with the condition keeps the SAME key (no spam); the next tick
    without the condition leaves none."""
    root, _, _ = world
    _set_limit(root, "review_stale_hours", 0)
    _submit(root)
    _write_review(root, "T-0001", TEMPLATE)
    _exhaust_review_retries(root)
    _refuse_escalation(monkeypatch)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "submitted"
    line = next(n for n in res["needs"] if n.startswith("T-0001 is stuck"))
    cards = _watch_cards(root)
    assert len(cards) == 1, cards
    c = cards[0]
    assert c["id"] == "T-0001" and c["text"] == line and "could not be escalated" in c["text"] and c["name"] == "escalate_failed"
    assert c["key"].startswith("need:escalate_failed:T-0001:") and "escalate" in c["title"]
    assert [n["key"] for n in _needs_file(root)["needs"]] == ["escalate_failed:T-0001"]
    Watcher(root, quiet=True, auto_decide=True).tick()
    assert [x["key"] for x in _watch_cards(root)] == [c["key"]]                              # same key across ticks
    monkeypatch.undo()                                                                        # the escalation goes through now
    Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "escalated"
    assert _watch_cards(root) == [] and _needs_file(root)["needs"] == []


def test_watch_need_card_is_acked_like_any_card(world, monkeypatch):
    root, _, _ = world
    _set_limit(root, "review_stale_hours", 0)
    _submit(root)
    _write_review(root, "T-0001", TEMPLATE)
    _exhaust_review_retries(root)
    _refuse_escalation(monkeypatch)
    Watcher(root, quiet=True, auto_decide=True).tick()
    [c] = _watch_cards(root)
    sscr(root, "--quiet", "--actor", "omri", "ack", c["key"])
    Watcher(root, quiet=True, auto_decide=True).tick()
    assert _watch_cards(root) == []


def test_watch_help_need_is_the_help_card_only(world):
    """DoD: a HELP line the tick raises as a need appears once in needs_cards: the help:<id>:<line> card."""
    root, _, _ = world
    _running(root)
    log = Store(root).get("T-0001").dir / "log.md"
    log.write_text(log.read_text(encoding="utf-8") + "- HELP: which lemma closes the goal?\n", encoding="utf-8")
    res = Watcher(root, quiet=True).tick()
    assert any("HELP" in n and n.startswith("T-0001") for n in res["needs"]), res["needs"]
    assert any(n["key"].startswith("help:T-0001:") for n in _needs_file(root)["needs"])
    cards = [c for c in ui_mod.needs_cards(root) if c["id"] == "T-0001"]
    assert [c["type"] for c in cards] == ["help"] and "which lemma" in cards[0]["text"], cards


def test_watch_land_failed_need_is_the_land_failed_card_only(world):
    """DoD: the watcher's own land_failed path (need line + event) -> one card, the land_failed system card."""
    root, _, _ = world
    _accepted(root)
    w = Watcher(root, quiet=True)
    w.flags |= {"autofix:T-0001:land:1", "autofix:T-0001:land:2"}      # 2026-10-14: the first two go back to the scriber
    w.land_failed(Store(root).get("T-0001"), {"ok": False, "reason": "conflict", "out": ["FAILED (conflict): A.lean"], "tip": "abc123"}, set(), "0000")
    w.publish_needs(datetime.now(timezone.utc))
    assert [n["key"] for n in _needs_file(root)["needs"]] == ["land_failed:T-0001"]
    cards = [c for c in ui_mod.needs_cards(root) if c["id"] == "T-0001"]
    assert [(c["type"], c.get("name")) for c in cards] == [("system", "land_failed")], cards


def test_watch_needs_file_is_written_empty_and_when_paused(world):
    """DoD: a tick that finds nothing still writes the file (empty list), so a cleared condition clears its card;
    the paused early return publishes too (its line is the page's paused banner, not a card)."""
    root, _, _ = world
    assert not (root / NEEDS_FILE).exists()
    res = Watcher(root, quiet=True).tick()
    doc = _needs_file(root)
    assert res["needs"] == [] and doc["needs"] == [] and doc["ts"] and doc["interval_min"] > 0
    (root / ".scr" / "paused").write_text("", encoding="utf-8")
    res = Watcher(root, quiet=True).tick()
    assert res["paused"] and res["needs"] == ["watcher paused"]
    assert [n["key"] for n in _needs_file(root)["needs"]] == ["paused"] and _watch_cards(root) == []


def test_watch_needs_older_than_two_ticks_show_nothing(world):
    """A watcher that stopped ticking leaves no cards up: a list older than WATCH_STALE_TICKS intervals is ignored."""
    root, _, _ = world
    w = Watcher(root, quiet=True)
    w.interval_min = 30
    w.need("checkpoint_failed:T-0001", "T-0001 checkpoint commit failed: boom", "T-0001")
    now = datetime.now(timezone.utc)
    w.publish_needs(now - timedelta(minutes=61))
    assert _watch_cards(root) == []
    w.publish_needs(now - timedelta(minutes=59))                     # same key: `since` is carried over
    assert _needs_file(root)["needs"][0]["since"] < _needs_file(root)["ts"]
    assert len(_watch_cards(root)) == 1


def test_watch_retire_refused_stands_while_the_worktree_does(world):
    """A refused retire happens once (the task is merged after it); the line stays raised while the worktree exists."""
    root, _, _ = world
    w = Watcher(root, quiet=True)
    t = Store(root).get("T-0001")
    t.rec["status"] = "merged"; t.save()                     # T-0209: the line (and the retry) are for a merged task only
    wt = task_worktree(workload_for(root, w.store, t), t)
    w.retire_refused("T-0001", "dirty")
    w.retire_refused_needs(w.store.load_all())
    assert w.needs == [] and not any(k.startswith("retire_refused:") for k in w.flags)       # no worktree: nothing to say
    wt.mkdir(parents=True)
    w.retire_refused("T-0001", "dirty")
    for _ in range(2):
        w.needs, w.need_items = [], []
        w.retire_refused_needs(w.store.load_all())
        assert [n["key"] for n in w.need_items] == ["retire_refused:T-0001"] and "(dirty)" in w.needs[0]


# Card types whose cards carry no `name`: their kind is the type itself (one chore, one decision, one HELP line, the
# open hotfix). `name` is the sub-kind of a system alarm, a watcher need or an escalated (gate) card (T-0341).
NAMELESS_CARD_TYPES = {"chore", "decision", "help", "hotfix"}


def assert_cards_named(cards: list[dict]) -> None:
    """Every needs card has a `name`, except the documented NAMELESS_CARD_TYPES — a clear failure, not a KeyError."""
    assert set(ui_mod.CARD_ORDER) >= NAMELESS_CARD_TYPES, "a nameless card type is no longer a card type"
    bad = [c for c in cards if c["type"] not in NAMELESS_CARD_TYPES and not c.get("name")]
    assert not bad, f"needs cards without a `name` (types not in NAMELESS_CARD_TYPES): {bad}"


def test_ui_state_carries_watch_cards_and_nothing_else_changes(world, monkeypatch):
    """DoD: ui.state() returns the watcher-need cards in `needs`; the rest of its JSON is unchanged field for field.
    T-0341: state() also reads two things outside the fixture root, pinned here. The chore cards come from a
    cache (ui._CHORE_CACHE, 120 s; keyed by root since T-0401, so an earlier test's root cannot leak in):
    on Windows one state() call takes up to a minute (a PowerShell process scan per call), so the cache expired
    between the two calls and `after` gained this root's chore cards, which carry no `name` (KeyError in
    J-0009/J-0015). `watcher_running` scans the machine's processes, so a live watcher starting or stopping
    flipped it (J-0014)."""
    root, _, _ = world
    monkeypatch.setattr("scr.run.pids_matching", lambda pattern: [])
    (root / ".gitignore").write_text(".scr/\n", encoding="utf-8")      # as in the live root: the file is never a journal change
    sgit(root, "add", ".gitignore"); sgit(root, "commit", "-q", "-m", "ignore .scr")
    before = ui_mod.state(root)
    w = Watcher(root, quiet=True)
    w.need("decision_unapplied:T-0001", "T-0001 decision accepted from review.md could not be applied — see log", "T-0001")
    w.publish_needs(datetime.now(timezone.utc))
    ui_mod.invalidate_chores()                                          # computed again, however long the first call took
    after = ui_mod.state(root)
    assert any(c["type"] == "chore" for c in after["needs"])           # the nameless type really is on the page
    assert_cards_named(before["needs"] + after["needs"])
    assert set(after) == set(before)
    for k in before:
        if k not in {"generated", "needs"}:
            assert after[k] == before[k], k
    added = [c for c in after["needs"] if c not in before["needs"]]
    assert [(c["type"], c["id"], c.get("name")) for c in added] == [("watch", "T-0001", "decision_unapplied")], added
    assert [c for c in after["needs"] if c["type"] != "watch"] == before["needs"]


def test_every_needs_card_has_a_name_or_is_a_documented_nameless_type(world):
    """T-0341: one card of each kind the fixture can raise cheaply (decision, help, system alarm, watch need, chore);
    every card has a `name` unless its type is in NAMELESS_CARD_TYPES, and a nameless card fails with a message."""
    from scr.events import EventLog
    root, _, _ = world
    scr(root, "--quiet", "task", "new", "Pick a way", "--id", "T-0002", "--queue", "omri", "--kind", "decide", "--origin", "omri", journal_on=False)
    scr(root, "--quiet", "task", "set", "T-0001", "dispatched", journal_on=False)
    (Store(root).get("T-0001").dir / "log.md").write_text("HELP: which lemma?\n", encoding="utf-8")
    # one card per task and quiet alarms raise none (2026-10-14): the alarm and the watch need go on tasks with no other card
    EventLog(root / "events" / "events.jsonl").append("system", name="verify_failed", task="T-0003", actor="steward", note="verify")
    w = Watcher(root, quiet=True)
    w.need("decision_unapplied:T-0004", "could not be applied", "T-0004")
    w.publish_needs(datetime.now(timezone.utc))
    ui_mod.invalidate_chores()
    cards = ui_mod.needs_cards(root)
    assert {"decision", "help", "system", "watch", "chore"} <= {c["type"] for c in cards}, cards
    assert_cards_named(cards)
    with pytest.raises(AssertionError, match="without a `name`"):
        assert_cards_named(cards + [{"type": "system", "id": "T-0001", "key": "events:x"}])


def _fake_chore_status(calls: list):
    """chores_mod.status stand-in: each root's chores are the ids in its chores.txt, minus the acked ones."""
    def status(root, acked):
        calls.append(Path(root).name)
        ids = (Path(root) / "chores.txt").read_text(encoding="utf-8").split()
        return [{"id": i, "title": i, "why": "", "manual": True, "link": None, "runnable": False}
                for i in ids if f"chore:{i}" not in acked]
    return status


def test_chore_cards_are_cached_per_root(tmp_path, monkeypatch):
    """T-0401: two roots within max_age, no invalidate_chores() between them: each gets its own chores, and a
    changed root (or a dismissed chore) is not served the other root's cached list."""
    calls: list = []
    monkeypatch.setattr(ui_mod.chores_mod, "status", _fake_chore_status(calls))
    monkeypatch.setattr(ui_mod, "_CHORE_CACHE", {})
    a, b = tmp_path / "a", tmp_path / "b"
    for r, ids in ((a, "ca1 ca2"), (b, "cb1")):
        r.mkdir(); (r / "chores.txt").write_text(ids, encoding="utf-8")
    assert [c["id"] for c in ui_mod.chore_cards(a, set())] == ["ca1", "ca2"]
    assert [c["id"] for c in ui_mod.chore_cards(b, set())] == ["cb1"]
    assert [c["id"] for c in ui_mod.chore_cards(a, set())] == ["ca1", "ca2"] and calls == ["a", "b"]
    assert [c["id"] for c in ui_mod.chore_cards(a, {"chore:ca1"})] == ["ca2"]       # a dismissal recomputes
    assert [c["id"] for c in ui_mod.chore_cards(a, {"chore:ca1", "help:T-0001"})] == ["ca2"] and calls == ["a", "b", "a"]
    assert len(ui_mod._CHORE_CACHE) == 2                                            # one entry per root


def test_chore_cards_cache_hits_then_invalidate_recomputes(tmp_path, monkeypatch):
    """T-0401: the same root twice within max_age computes chores once; invalidate_chores() clears every root's
    entry, so the next call recomputes; an expired entry is recomputed and evicted with the other expired ones."""
    calls: list = []
    monkeypatch.setattr(ui_mod.chores_mod, "status", _fake_chore_status(calls))
    monkeypatch.setattr(ui_mod, "_CHORE_CACHE", {})
    a, b = tmp_path / "a", tmp_path / "b"
    for r in (a, b):
        r.mkdir(); (r / "chores.txt").write_text("c1", encoding="utf-8")
    first = ui_mod.chore_cards(a, set())
    assert ui_mod.chore_cards(a, set()) is first and calls == ["a"]
    ui_mod.chore_cards(b, set())
    ui_mod.invalidate_chores()
    assert ui_mod._CHORE_CACHE == {}
    ui_mod.chore_cards(a, set())
    assert calls == ["a", "b", "a"]
    t = ui_mod.time.time()
    monkeypatch.setattr(ui_mod.time, "time", lambda: t + 121)
    ui_mod.chore_cards(b, set())                                                    # a's entry expired: evicted
    assert calls == ["a", "b", "a", "b"] and list(ui_mod._CHORE_CACHE) == [str(b.resolve())]


def test_every_watch_need_goes_through_the_keyed_helper():
    """Every NEEDS-YOU line has a stable key: `self.needs.append` appears only inside Watcher.need."""
    src = (REPO / "scr" / "watch.py").read_text(encoding="utf-8")
    assert src.count("self.needs.append(") == 1 and src.count("self.need(") >= 15


# ---- T-0180: a half-written report.md is not a submission --------------------------------------------------
# T-0172 was submitted with a literal FULL_SUITE_PLACEHOLDER where the full-suite result belonged. The text
# below is T-0172's final report.md (the placeholder line already replaced by the real result).
from scr.watch import report_unfinished  # noqa: E402

T0172_REPORT = """# Report

## Result
The suite now runs the real WinRT toast script through powershell once, in **dry** mode: nothing is put on screen.

- `scr/notify.py` `toast_argv(cards, exe="powershell", dry=False)`: new keyword `dry`. The script now binds
  `$t = ...CreateToastNotifier(<app id>)` and `$n = [ToastNotification]::new($x)` and, only when not dry, ends with
  `; $t.Show($n)`. So dry exercises everything the real toast does (both WinRT type loads, `New-Object XmlDocument`,
  `LoadXml` of the escaped XML, `CreateToastNotifier` with `TOAST_APP_ID`, building the `ToastNotification`) except `.Show`.
  The script also now starts with `$ErrorActionPreference = 'Stop'`, so any failing statement ends the script with rc 1
  (before, a failed type load in the middle could be followed by statements that still "succeed"; the last statement's
  status decided rc). This applies to the real toast too: `toast()` now reliably raises `powershell rc 1: ...` on such a break.
- `tests/test_watch.py` `test_toast_smoke_runs_the_real_script_through_powershell`: `@pytest.mark.skipif(os.name != "nt" or not
  shutil.which("powershell"))`; card titled `scr test suite: toast smoke (ignore)`; runs
  `subprocess.run(notify.toast_argv([card], shutil.which("powershell"), dry=True), capture_output=True, text=True, timeout=60)`
  (plus `CREATE_NO_WINDOW`) and asserts rc 0 and empty stderr; also asserts `.Show(` is absent from the dry script.
  Deselect with `-k "not toast_smoke"`.

done_when:
- `pytest tests/test_watch.py -k toast_smoke -q` -> `1 passed, 102 deselected` (not skipped). PASS.
- skipif on `os.name != "nt"` and `shutil.which("powershell")`. PASS.
- `test_toast_argv_names_the_card_title_and_task_id` unchanged and passes (`-k toast` -> 2 passed); `toast()` and
  `_under_pytest` untouched, `test_scr_notify_test_reports_every_channel` still sees `skipped (pytest)`. PASS.
- full suite: see below.
- Judgment, broken script: with `XmlDocument` misspelled as `XmlDocumnet` in the type-load line (and `::new` as `::nwe`),
  the smoke test FAILED: `AssertionError: rc 1: Unable to find type [Windows.Data.Xml.Dom.XmlDocumnet,Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime].`
  After reverting it passed again (`1 passed`).
- Judgment, toasts on screen: dry, so running the suite puts **zero** toasts on screen.

Full suite: `.venv/Scripts/python.exe -m pytest tests -q` in the worktree with the live-root `.venv` python ->
`231 passed in 2611.58s (0:43:31)`, exit 0. PASS. Note that the suite has become slow (43.5 min on this box, up from about
20 min in T-0090). A land gate with a shorter timeout will time out on it, as T-0170's land did with `exited 124`. The new
smoke test adds about 2 s of that.

## What I am unsure about
- Dry mode does not prove `.Show` itself succeeds (for example Focus Assist, or the app id being unregistered at show
  time). Everything before it is covered; `.Show` on a built `ToastNotification` with a valid notifier is the one
  untested call.
- `$ErrorActionPreference = 'Stop'` is a small behaviour change to the live toast: failures that were silently
  swallowed now raise in `toast()` (and so become `notify FAILED: toast: ...` lines). I think that is the wanted
  direction, but the Steward may want it noted.

## Statement changes
None. (`toast_argv` gained an optional keyword argument `dry=False`; existing callers are unchanged. This is Python, no
Lean statements touched.)

## Tokens and attempts
Attempt 1 of 2; roughly 60k tokens.

## unlocks
```yaml
on_success: []     # {title, kind, queue, scope, reason} — proposals, not tasks; Intake decides
on_failure: []
informs:
  - {task: T-0171, weight: 0.2, reason: "toast_argv gained dry= and $ErrorActionPreference='Stop'; a failing toast now raises reliably, which feeds the T-0171 retry path"}
```
"""
REPORT_TPL = (REPO / "templates" / "report.md").read_text(encoding="utf-8").strip()


def test_report_unfinished_names_a_placeholder_an_empty_result_and_the_template_prompt():
    assert report_unfinished(T0172_REPORT, REPORT_TPL) is None
    submitted = T0172_REPORT.replace("Full suite: `.venv", "FULL_SUITE_PLACEHOLDER\n\nFull suite: `.venv")
    assert submitted != T0172_REPORT and "FULL_SUITE_PLACEHOLDER" in report_unfinished(submitted, REPORT_TPL)
    assert "FULL_SUITE_PLACEHOLDER" in report_unfinished("# Report\n\n## Result\nProved a.\n- full suite: FULL_SUITE_PLACEHOLDER\n", REPORT_TPL)
    empty = REPORT_TPL.replace("What was proved / produced. Which done_when lines pass, in the Scriber's own assessment.", "")
    assert empty != REPORT_TPL and "## Result is empty" in report_unfinished(empty + "\nfilled in elsewhere\n", REPORT_TPL)
    assert "## Result is empty" in report_unfinished("# Report\n\n## Result\n\n## What I am unsure about\nnothing\n", REPORT_TPL)
    prompt = REPORT_TPL + "\n\nfilled in elsewhere\n"                          # differs from the template, Result untouched
    assert "template prompt" in report_unfinished(prompt, REPORT_TPL)
    # lower-case or unanchored words are not placeholders; a report with no Result heading is not judged
    for ok in ("# Report\n\n## Result\nThe _PLACEHOLDER rule in scr/review.py is untouched; a placeholder_x name too.\n",
               "# report\n\n## Result\n\ndone\n\n## Statement changes\n\nnone\n", "done\n"):
        assert report_unfinished(ok, REPORT_TPL) is None, ok


def test_report_unfinished_ignores_tokens_in_a_fence_or_backticks():
    """T-0181: a token quoted in a code fence or inline backticks is a mention (a report about T-0180 names
    FULL_SUITE_PLACEHOLDER); a bare prose token is still caught, also next to a fenced one. An unclosed fence
    hides the rest of the text, as `_unfenced` does for review.md Decision lines."""
    fenced = "# Report\n\n## Result\nProved a.\n\n```\nFull suite: FULL_SUITE_PLACEHOLDER\n```\n\nFull suite: 3 passed\n"
    assert report_unfinished(fenced, REPORT_TPL) is None
    assert report_unfinished(fenced.replace("```", "~~~"), REPORT_TPL) is None
    ticked = "# Report\n\n## Result\nT-0172 held a literal `FULL_SUITE_PLACEHOLDER`; now caught. Full suite: 3 passed\n"
    assert report_unfinished(ticked, REPORT_TPL) is None
    bare = "# Report\n\n## Result\nProved a.\n\nFull suite: FULL_SUITE_PLACEHOLDER\n"
    assert "FULL_SUITE_PLACEHOLDER" in report_unfinished(bare, REPORT_TPL)
    both = fenced + "\nAxioms: AXIOMS_PLACEHOLDER\n"
    assert "AXIOMS_PLACEHOLDER" in report_unfinished(both, REPORT_TPL)
    assert "FULL_SUITE_PLACEHOLDER" in report_unfinished("# Report\n\n## Result\n`x` FULL_SUITE_PLACEHOLDER `y`\n", REPORT_TPL)
    unclosed = "# Report\n\n## Result\nProved a.\n\n```\nFull suite: FULL_SUITE_PLACEHOLDER\n"
    assert report_unfinished(unclosed, REPORT_TPL) is None
    # the Result checks still read the original text: a Result that is only a fenced block is not empty
    assert report_unfinished("# Report\n\n## Result\n```\nlake build: ok\n```\n", REPORT_TPL) is None


def test_tick_keeps_placeholder_report_in_progress(world, monkeypatch):
    """DoD: an in_progress task whose report.md holds a placeholder stays in_progress, a needs line with key
    report:<id> appears (logged once), and no `task set ... submitted` call is made; once the report is
    finished the next tick submits it as before."""
    root, _, _ = world
    _running(root)
    t = Store(root).get("T-0001")
    (t.dir / "report.md").write_text("# report\n\n## Result\n\nProved a.\n\nFull suite: FULL_SUITE_PLACEHOLDER\n\n## Statement changes\n\nnone\n", encoding="utf-8")
    calls = _spy_scr(monkeypatch)
    for _ in range(2):
        w = Watcher(root, quiet=True)
        res = w.tick()
        assert res["ok"], res
        assert Store(root).get("T-0001").status == "in_progress"
        items = [n for n in w.need_items if n["key"] == "report:T-0001"]
        assert len(items) == 1 and items[0]["task"] == "T-0001" and "FULL_SUITE_PLACEHOLDER" in items[0]["text"], w.need_items
        assert items[0]["text"].startswith("T-0001 report.md unfinished: ")
        assert "REPORT-UNFINISHED" in res["rows"][0]["flags"]
    assert not any("submitted" in args for _, args in calls), calls
    log = (root / "reports" / "steward_watch.log").read_text(encoding="utf-8")
    assert log.count("T-0001 report.md unfinished") == 1                                # logged once
    assert "report_unfinished:T-0001" in _flags(root)
    # finished: submitted exactly as today, and the flag is gone
    (t.dir / "report.md").write_text("# report\n\n## Result\n\nProved a.\n\nFull suite: 3 passed\n\n## Statement changes\n\nnone\n", encoding="utf-8")
    w = Watcher(root, quiet=True)
    w.tick()
    assert Store(root).get("T-0001").status == "submitted" and not any(n["key"] == "report:T-0001" for n in w.need_items)
    assert "report_unfinished:T-0001" not in _flags(root)


def test_tick_returns_a_dead_session_that_left_an_unfinished_report(world):
    """A session that died with a placeholder still in report.md is not submitted: the existing dead-session
    path returns it (one session_dead event), as it would a session that wrote no report."""
    import subprocess as sp
    from test_journal_ops import _named, _running_with_session
    p = sp.Popen([sys.executable, "-c", "pass"]); p.wait()                  # a pid that existed and is gone
    root, _, t = _running_with_session(world, p.pid)
    (t.dir / "report.md").write_text("# report\n\n## Result\n\nFULL_SUITE_PLACEHOLDER\n", encoding="utf-8")
    w = Watcher(root, quiet=True)
    res = w.tick()
    assert res["ok"] and Store(root).get("T-0001").status == "ready", res         # returned, then relaunched (2026-10-14)
    assert len(_named(root, "session_dead")) == 1
    assert any(n["key"] == "report:T-0001" for n in w.need_items) and not any(n["key"] == "dead:T-0001" for n in w.need_items)


# ---- T-0188: retire measures a workload branch against origin/<main>, not the local main ------------------------
# quantum-insertion's local master sat 53 commits behind origin/master on 2026-09-24, so retire refused T-0115
# (merged by PR #64) as `52 commit(s) ahead of master`.
from test_journal_ops import git as jgit  # noqa: E402
from scr.ops import checkpoint_commit, retire  # noqa: E402


def _retire_world(world, merged: bool):
    """T-0001 spawned with two commits; origin/master moves on by three commits of its own (plus the branch when
    `merged`), pushed from another clone, so the workload's local master and its origin/master ref stay behind."""
    root, repo, origin = world
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched")
    spawn(root, ["T-0001"], lake=False)
    wt = repo / ".worktrees" / "T-0001"
    for i in (1, 2):
        (wt / f"n{i}.txt").write_text(f"{i}\n"); checkpoint_commit(root, "T-0001")
    jgit(wt, "push", "-q", "origin", "task/T-0001")
    other = repo.parent / "other-clone"
    jgit(repo.parent, "clone", "-q", str(origin), str(other))
    jgit(other, "config", "user.email", "t@t"); jgit(other, "config", "user.name", "t")
    for i in (1, 2, 3):
        (other / f"o{i}.txt").write_text(f"{i}\n"); jgit(other, "add", "-A"); jgit(other, "commit", "-q", "-m", f"o{i}")
    if merged:
        jgit(other, "fetch", "-q", "origin", "task/T-0001")
        jgit(other, "merge", "-q", "--no-edit", "FETCH_HEAD")
    jgit(other, "push", "-q", "origin", "master")
    return root, repo, wt


def test_retire_takes_a_branch_merged_into_origin_main_only(world):
    """DoD: local main behind origin/main, the branch merged into origin/main only -> retire proceeds (dry run shows
    the move), no REFUSING line; the local master is left where it was."""
    root, repo, wt = _retire_world(world, merged=True)
    _set_status(root, "T-0001", "merged")
    local = jgit(repo, "rev-parse", "master")
    assert int(jgit(repo, "rev-list", "--count", "master..task/T-0001")) == 2          # the old measure: 2 ahead
    res = retire(root, "T-0001")
    assert res["ok"] and res["code"] == 0, res["out"]
    assert not any("REFUSING" in l for l in res["out"])
    assert "origin/master" in res["out"][0] and any(l.startswith(f"  delete {wt}") for l in res["out"]) and res["out"][-1] == "DRY RUN — nothing deleted"
    assert wt.exists() and jgit(repo, "rev-parse", "master") == local                  # nothing moved, local master untouched
    assert retire(root, "T-0001", execute=True)["ok"] and not wt.exists()


def test_retire_still_refuses_a_branch_not_in_origin_main(world):
    """DoD: a branch not merged into origin/main is refused, its ahead count measured against origin/main."""
    root, repo, wt = _retire_world(world, merged=False)
    _set_status(root, "T-0001", "merged")
    res = retire(root, "T-0001", execute=True)
    assert not res["ok"] and res["code"] == 2 and wt.exists(), res["out"]
    assert res["out"][-1].startswith("REFUSING: task/T-0001 is 2 commit(s) ahead of origin/master")


# ---- T-0209: the watcher retries a refused retire of a merged task, at most hourly ------------------------------
# 2026-09-25: seven retire_refused:<id>:2 flags (T-0115 among them) stood as NEEDS-YOU cards after T-0188 had fixed
# the cause, because nothing ever called retire again.
import scr.watch as watch_mod  # noqa: E402
from scr.watch import LOG_FILE, STATE_FILE  # noqa: E402


def _set_status(root: Path, tid: str, status: str) -> None:
    t = Store(root).get(tid)
    t.rec["status"] = status; t.save()


def _counting_retire(monkeypatch) -> list:
    calls, real = [], watch_mod.retire
    monkeypatch.setattr(watch_mod, "retire", lambda root, tid, execute=False: calls.append(tid) or real(root, tid, execute=execute))
    return calls


def _retire_flags(w, tid: str = "T-0001") -> list[str]:
    return sorted(k for k in w.flags if k.startswith((f"retire_refused:{tid}:", f"retire_retry:{tid}:")))


def test_watch_retries_a_refused_retire_once_the_branch_is_merged(world):
    """DoD: merged task, leftover worktree whose branch is in origin/master, a retire_refused:<id>:2 flag -> after one
    tick the worktree is deleted (T-0321), no retire_refused flag remains and no need is raised."""
    root, repo, wt = _retire_world(world, merged=True)
    _set_status(root, "T-0001", "merged")
    w = Watcher(root, quiet=True)
    w.flags.add("retire_refused:T-0001:2")
    res = w.tick()
    assert not wt.exists() and len(_events(root, "worktree_deleted", "T-0001")) == 1 and jgit(repo, "rev-parse", "--verify", "task/T-0001")
    assert _retire_flags(w) == [] and "retire_refused:T-0001" not in [n["key"] for n in w.need_items], res["needs"]
    assert not any("retire_refused:" in l for l in (root / STATE_FILE).read_text(encoding="utf-8").splitlines())
    assert "T-0001 retire retry: worktree retired" in (root / LOG_FILE).read_text(encoding="utf-8")


def test_watch_retire_retry_is_hourly_and_quiet_while_refused(world, monkeypatch):
    """DoD: the branch still ahead -> flag and need stay; a second tick within the hour does not call retire, a tick
    after the hour does. A repeat refusal with the same code adds no log line."""
    root, repo, wt = _retire_world(world, merged=False)
    _set_status(root, "T-0001", "merged")
    calls = _counting_retire(monkeypatch)
    w = Watcher(root, quiet=True)
    w.flags.add("retire_refused:T-0001:2")
    log = root / LOG_FILE

    def tick() -> list[str]:
        w.tick()
        return [l for l in log.read_text(encoding="utf-8").splitlines() if "retire retry" in l or " retire: " in l] if log.exists() else []

    lines = tick()
    assert calls == ["T-0001"] and wt.exists() and lines == []
    assert "retire_refused:T-0001:2" in w.flags and [n["key"] for n in w.need_items if n["task"] == "T-0001"] == ["retire_refused:T-0001"]
    assert tick() == [] and calls == ["T-0001"]                               # within the hour: no second attempt
    assert "retire_refused:T-0001" in [n["key"] for n in w.need_items]
    old = f"retire_retry:T-0001:{datetime.now() - timedelta(minutes=61):%Y%m%dT%H%M%S}"
    w.flags -= {k for k in w.flags if k.startswith("retire_retry:T-0001:")}
    w.flags.add(old)
    assert tick() == [] and calls == ["T-0001", "T-0001"]                     # after the hour: one more, still refused, still quiet
    assert "retire_refused:T-0001:2" in w.flags and old not in w.flags
    assert "retire_refused:T-0001" in [n["key"] for n in w.need_items]


def test_watch_retire_retry_never_runs_off_merged_or_without_a_worktree(world, monkeypatch):
    """DoD: a task that is no longer merged, or whose worktree is gone -> no retire call, the flag is dropped."""
    root, _, _ = world
    calls = _counting_retire(monkeypatch)
    t = Store(root).get("T-0001")
    wt = task_worktree(workload_for(root, Store(root), t), t)
    wt.mkdir(parents=True)
    for st in ("returned", "accepted", "in_progress"):
        _set_status(root, "T-0001", st)
        w = Watcher(root, quiet=True)
        w.flags |= {"retire_refused:T-0001:2", "retire_retry:T-0001:20000101T000000"}
        w.retire_refused_needs(w.store.load_all())
        assert calls == [] and _retire_flags(w) == [] and w.need_items == [], st
    shutil.rmtree(wt)
    _set_status(root, "T-0001", "merged")
    w = Watcher(root, quiet=True)
    w.flags |= {"retire_refused:T-0001:2", "retire_retry:T-0001:20000101T000000"}
    w.retire_refused_needs(w.store.load_all())
    assert calls == [] and _retire_flags(w) == [] and w.need_items == []


# ---- T-0321: closed / rejected trees are deleted by the watcher; a dirty one takes the retire_refused path ------
def _finish(root: Path, tid: str, status: str) -> None:
    from scr.events import now_iso
    t = Store(root).get(tid)
    t.rec["status"] = status; t.rec["updated"] = now_iso(); t.save()


def test_watch_deletes_a_closed_or_rejected_worktree(world, monkeypatch):
    """DoD 2 through the watcher: a task closed / rejected while the watcher runs has its worktree deleted on the next
    tick (branch kept, one worktree_deleted event); one closed before the watcher started is left to `worktrees gc`."""
    root, repo, wt = _retire_world(world, merged=False)
    tip = jgit(repo, "rev-parse", "task/T-0001")
    _finish(root, "T-0001", "closed")
    calls = _counting_retire(monkeypatch)
    w = Watcher(root, quiet=True)
    w.started_iso = "2999-01-01T00:00:00Z"                                    # closed long before this watcher
    w.retire_finished(w.store.load_all())
    assert calls == [] and wt.exists()
    for n, status in enumerate(("rejected", "closed"), start=1):
        if n == 2:                                                            # a fresh tree on the same branch
            jgit(repo, "worktree", "add", "-q", str(wt), "task/T-0001")
        w = Watcher(root, quiet=True)
        w.started_iso = "2000-01-01T00:00:00Z"
        _finish(root, "T-0001", status)
        w.tick()
        assert not wt.exists() and calls == ["T-0001"] * n, status
        assert len(_events(root, "worktree_deleted", "T-0001")) == n and jgit(repo, "rev-parse", "task/T-0001") == tip
        assert _retire_flags(w) == [] and "retire_refused:T-0001" not in [k["key"] for k in w.need_items]


def test_watch_dirty_finished_worktree_takes_the_retire_refused_path(world):
    """DoD 6 through the watcher: a merged task whose worktree has a modified tracked file -> the retry refuses with
    code 3, the tree is untouched, flag retire_refused:<id>:3 and its NEEDS-YOU line stand; the same for a closed one."""
    root, repo, wt = _retire_world(world, merged=True)
    _set_status(root, "T-0001", "merged")
    (wt / "n1.txt").write_text("changed\n")
    w = Watcher(root, quiet=True)
    w.flags |= {"retire_refused:T-0001:2", "retire_retry:T-0001:20000101T000000"}
    w.retire_refused_needs(w.store.load_all())
    assert (wt / "n1.txt").read_text() == "changed\n" and not (repo.parent / f"{repo.name}-RETIRED").exists()
    assert "retire_refused:T-0001:3" in w.flags and [n["key"] for n in w.need_items] == ["retire_refused:T-0001"]
    assert "merged" in w.needs[0] and "(3)" in w.needs[0]
    _finish(root, "T-0001", "closed")
    w = Watcher(root, quiet=True)
    w.started_iso = "2000-01-01T00:00:00Z"
    w.retire_finished(w.store.load_all())
    assert wt.exists() and "retire_refused:T-0001:3" in w.flags and not _events(root, "worktree_deleted", "T-0001")
    w.retire_refused_needs(w.store.load_all())
    assert [n["key"] for n in w.need_items] == ["retire_refused:T-0001"] and "closed" in w.needs[0]



# ---- T-0440: a finished tree something still works in is not retired; one need line until it is free ----------
def _busy_job(root: Path, cwd: Path, task: str | None = None, jid: str = "J-0007") -> None:
    """A `running` job record (no wrapper pid, no deadline: jobs.poll leaves it alone) working in `cwd`."""
    from scr import jobs
    jobs.save(root, {"id": jid, "task": task, "name": "lake", "cmd": ["lake", "build"], "cwd": str(cwd.resolve()),
                     "status": "running", "pid": None, "deadline": None, "timeout_min": 240})


def _busy_keys(w) -> list[str]:
    return [n["key"] for n in w.need_items if n["key"].startswith("retire_busy:")]


def test_watch_retire_skips_a_rejected_tree_with_a_live_session(world, monkeypatch):
    """DoD 1 + 3: rejected task, worktree present, the run's launch pid alive -> the tick leaves the tree, never calls
    retire, raises one need line naming the task and pid; a second tick adds no second line. Once the pid is dead
    (and no job is active) the next tick retires the tree and clears flag and line."""
    root, repo, wt = _retire_world(world, merged=False)
    _launched(root, "T-0001", 4242)
    _finish(root, "T-0001", "rejected")
    calls = _counting_retire(monkeypatch)
    alive = {4242}
    monkeypatch.setattr(watch_mod, "pid_alive", lambda pid: pid in alive)
    w = Watcher(root, quiet=True)
    w.started_iso = "2000-01-01T00:00:00Z"
    for _ in range(2):
        w.tick()
        assert wt.exists() and calls == [] and _busy_keys(w) == ["retire_busy:T-0001"]
        lines = [l for l in w.needs if "4242" in l]
        assert len(lines) == 1 and "T-0001" in lines[0] and "rejected" in lines[0]
        assert "retire_busy:T-0001" in w.flags
    assert sum("retire deferred" in l for l in (root / LOG_FILE).read_text(encoding="utf-8").splitlines()) == 1
    alive.clear()
    w.tick()
    assert not wt.exists() and calls == ["T-0001"] and len(_events(root, "worktree_deleted", "T-0001")) == 1
    assert "retire_busy:T-0001" not in w.flags and _busy_keys(w) == [] and not any("4242" in l for l in w.needs)


def test_watch_retire_skips_a_tree_with_an_active_job_inside(world, monkeypatch):
    """DoD 2: no live session but a `running` job whose cwd is inside the worktree (owned by no task) -> skipped,
    the need line names the J-id; the job finished -> the next tick retires."""
    root, repo, wt = _retire_world(world, merged=False)
    _finish(root, "T-0001", "closed")
    (wt / "sub").mkdir()
    _busy_job(root, wt / "sub")
    calls = _counting_retire(monkeypatch)
    w = Watcher(root, quiet=True)
    w.started_iso = "2000-01-01T00:00:00Z"
    w.tick()
    assert wt.exists() and calls == [] and _busy_keys(w) == ["retire_busy:T-0001"]
    assert len([l for l in w.needs if "J-0007" in l]) == 1 and "T-0001" in w.needs[[i for i, l in enumerate(w.needs) if "J-0007" in l][0]]
    from scr import jobs
    jobs.save(root, {**jobs.load(root, "J-0007"), "status": "done", "rc": 0})
    w.tick()
    assert not wt.exists() and calls == ["T-0001"] and "retire_busy:T-0001" not in w.flags and _busy_keys(w) == []


def test_watch_retire_busy_by_task_job_and_reused_pid(world, monkeypatch):
    """An active job of the task (cwd elsewhere) also defers; a launch pid that now belongs to another process
    (pid_reused) counts as dead, so with no job the tree is retired."""
    root, repo, wt = _retire_world(world, merged=False)
    _launched(root, "T-0001", 4242, created=111)
    _finish(root, "T-0001", "rejected")
    _busy_job(root, root, task="T-0001", jid="J-0009")
    calls = _counting_retire(monkeypatch)
    monkeypatch.setattr(watch_mod, "pid_alive", lambda pid: True)
    monkeypatch.setattr(watch_mod, "process_created", lambda pid: 222)
    w = Watcher(root, quiet=True)
    w.started_iso = "2000-01-01T00:00:00Z"
    w.retire_finished(w.store.load_all())
    assert wt.exists() and calls == [] and _busy_keys(w) == ["retire_busy:T-0001"] and "J-0009" in w.needs[0]
    assert "4242" not in w.needs[0]
    from scr import jobs
    jobs.save(root, {**jobs.load(root, "J-0009"), "status": "killed"})
    w.need_items, w.needs = [], []
    w.retire_finished(w.store.load_all())
    assert not wt.exists() and calls == ["T-0001"] and "retire_busy:T-0001" not in w.flags and w.need_items == []

# ---- T-0187: out of usage credits is not a session death ---------------------------------------------------
# 2026-09-23 09:47Z-13:56Z the account had no credits: each headless session printed one line and exited, the
# watcher returned the task and spent its attempt, then dispatched the next two into the same wall (16 tasks).
# Now a credit death is ready again with its attempt refunded, ONE credits_exhausted event pauses auto-dispatch,
# and a death that did no work is set ready once. pid_alive is replaced in scr.watch; no real claude runs.
from scr.watch import CREDITS_PAUSE_MINUTES, credits_line, credits_pause, did_work  # noqa: E402

CREDIT_MSG = ("You're out of usage credits. Switch to another model, or manage usage credits at "
              "claude.ai/settings/usage?from=cc_cli_limit_message, to continue.\n")
TRANSCRIPT = "session-20260923-000000.txt"          # the name _launched records


def _dying(root: Path, tid: str = "T-0001", pid: int = 4242, transcript: str = CREDIT_MSG, work: bool = False,
           scriber_line: bool = False) -> None:
    """A ready task dispatched, spawned (once), in_progress and launched as `pid`, whose session has exited leaving
    `transcript` (and an empty .err, as on 2026-09-23). `work` edits the worktree (the tick checkpoints it);
    `scriber_line` appends a [scriber] line to log.md."""
    _dispatched(root, tid)
    t = Store(root).get(tid)
    wt = task_worktree(workload_for(root, Store(root), t), t)
    if not wt.exists():
        spawn(root, [tid], lake=False)
    scr(root, "--quiet", "--actor", f"steward:{tid}", "task", "set", tid, "in_progress")
    _launched(root, tid, pid)
    (t.dir / TRANSCRIPT).write_text(transcript, encoding="utf-8")
    (t.dir / (TRANSCRIPT + ".err")).write_text("", encoding="utf-8")
    if scriber_line:
        with (t.dir / "log.md").open("a", encoding="utf-8") as f:
            f.write("- 2026-09-23T10:00:00 [scriber] read the spec; plan written\n")
    if work:
        (wt / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")


def _dead_pids(monkeypatch) -> None:
    import scr.watch as watch_mod
    monkeypatch.setattr(watch_mod, "pid_alive", lambda pid: False)


def _sys_events(root: Path, name: str) -> list[dict]:
    from scr.events import EventLog
    return [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "system" and e.get("name") == name]


def _credit_needs(res: dict) -> list[str]:
    return [n for n in res["needs"] if "usage credits" in n]


def test_credits_line_reads_the_tail_and_not_a_quote(tmp_path: Path):
    p = tmp_path / "s.txt"
    p.write_text(CREDIT_MSG, encoding="utf-8")
    assert credits_line(p).startswith("You're out of usage credits")
    p.write_text("Done. The watcher now reads `You're out of usage credits` as a pause.\nreport written\n", encoding="utf-8")
    assert credits_line(p) is None                       # a quote inside a sentence is not the message
    p.write_text(CREDIT_MSG + "\n".join(f"line {i}" for i in range(5)), encoding="utf-8")
    assert credits_line(p) is None                       # only the last lines count
    assert credits_line(tmp_path / "missing.txt", p.with_name("e.err")) is None


def test_credits_pause_is_a_pure_function_of_the_log_and_time():
    t0 = datetime(2026, 9, 23, 9, 47, tzinfo=timezone.utc)
    ev = lambda name, m: {"kind": "system", "name": name, "ts": (t0 + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")}  # noqa: E731
    assert credits_pause([], t0, 60) is None
    p = credits_pause([ev("credits_exhausted", 0)], t0 + timedelta(minutes=10), 60)
    assert p and p["since"] == t0 and p["until"] == t0 + timedelta(minutes=60)
    assert credits_pause([ev("credits_exhausted", 0)], t0 + timedelta(minutes=61), 60) is None          # the probe
    for resume in ("credits_resumed", "watcher_resumed"):
        assert credits_pause([ev("credits_exhausted", 0), ev(resume, 5)], t0 + timedelta(minutes=10), 60) is None
    assert credits_pause([ev("credits_resumed", 0), ev("credits_exhausted", 5)], t0 + timedelta(minutes=10), 60)


def test_credit_death_is_ready_unspent_and_pauses_dispatch(world, monkeypatch):
    """DoD 2: a dead session whose transcript holds the credit message -> one credits_exhausted event, the task
    ready, attempts as before its dispatch, and neither that tick nor the next dispatches anything; a
    credits_resumed event ends the pause and the next tick dispatches again."""
    root, _, _ = world
    before = int(Store(root).get("T-0001").rec.get("attempts", 0))
    _dying(root)
    assert int(Store(root).get("T-0001").rec["attempts"]) == before + 1
    _dead_pids(monkeypatch)
    launches = _record_launches(monkeypatch)
    calls = _spy_scr(monkeypatch)
    res = Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()
    t = Store(root).get("T-0001")
    assert res["ok"] and t.status == "ready" and int(t.rec["attempts"]) == before, res
    assert [e["to"] for e in _state_events(root, "T-0001")][-2:] == ["returned", "ready"]
    assert len(_sys_events(root, "credits_exhausted")) == 1 and not _events(root, "session_dead", "T-0001")
    assert len(_credit_needs(res)) == 1 and "credits_resumed" in _credit_needs(res)[0]
    assert [n["key"] for n in _needs_file(root)["needs"]].count("credits") == 1
    res2 = Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()
    assert not [a for _, a in calls if a and a[0] == "dispatch"] and not launches, calls
    assert Store(root).get("T-0001").status == "ready" and len(_credit_needs(res2)) == 1
    assert len(_sys_events(root, "credits_exhausted")) == 1                       # the standing pause raises nothing more
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "credits_resumed")
    res3 = Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()
    assert not _credit_needs(res3) and [a for _, a in calls if a and a[0] == "dispatch"], calls
    assert Store(root).get("T-0001").status == "in_progress" and len(launches) == 1


def test_two_credit_deaths_in_one_tick_are_one_event_and_one_line(world, monkeypatch):
    """DoD 3: two dead sessions with the message in one tick -> one event and one needs line, both tasks ready
    with their attempts refunded."""
    root, _, _ = world
    _ready_task(root, "T-0002", "QuantumInsertion/B.lean")
    _dying(root, "T-0001", 4242)
    _dying(root, "T-0002", 4343)
    _dead_pids(monkeypatch)
    res = Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()
    assert res["ok"], res
    for tid in ("T-0001", "T-0002"):
        t = Store(root).get(tid)
        assert t.status == "ready" and int(t.rec["attempts"]) == 0, (tid, t.rec)
    assert len(_sys_events(root, "credits_exhausted")) == 1
    assert len(_credit_needs(res)) == 1 and [n["key"] for n in _needs_file(root)["needs"]].count("credits") == 1


def test_no_work_death_is_ready_once_per_episode(world, monkeypatch):
    """DoD 4: a returned-by-death task with no checkpoint and no [scriber] log line goes back to ready once
    (attempt counted); the next death in the same episode, again without work, leaves it returned."""
    root, _, _ = world
    _dying(root, transcript="")
    _dead_pids(monkeypatch)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    t = Store(root).get("T-0001")
    assert res["ok"] and t.status == "ready" and int(t.rec["attempts"]) == 1, res
    assert [e["to"] for e in _state_events(root, "T-0001")][-2:] == ["returned", "ready"]
    assert len(_events(root, "session_dead", "T-0001")) == 1 and not _sys_events(root, "credits_exhausted")
    _dying(root, pid=4343, transcript="")
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    t = Store(root).get("T-0001")
    # 2026-10-14: the no-work path is spent, but the task still has attempts: relaunched by autofix (auto_fix event)
    assert res["ok"] and t.status == "ready" and int(t.rec["attempts"]) == 2, res
    assert len(_events(root, "session_dead", "T-0001")) == 2 and len(_events(root, "auto_fix", "T-0001")) == 1
    # attempts spent: it stays returned and is Omri's
    t.rec["attempts"] = 3; t.save()
    _dying(root, pid=4444, transcript="")
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    t = Store(root).get("T-0001")
    assert res["ok"] and t.status == "returned", res
    assert any(n.startswith("T-0001 session died") and "attempts spent" in n for n in res["needs"]), res["needs"]


@pytest.mark.parametrize("work", ["checkpoint", "scriber", "report", "no_transcript"])
def test_death_with_real_work_is_unchanged(world, monkeypatch, work):
    """DoD 5: a dead session that did real work (a checkpoint commit this tick, a [scriber] log line, a report.md
    written) — or one with no transcript on disk, so no evidence it never ran — is returned as before, its attempt
    counted, one session_dead event, no pause."""
    root, _, _ = world
    _dying(root, transcript="", work=work == "checkpoint", scriber_line=work == "scriber")
    t = Store(root).get("T-0001")
    if work == "report":
        (t.dir / "report.md").write_text("# Report\n\n## Result\n\nFULL_SUITE_PLACEHOLDER\n", encoding="utf-8")   # unfinished: no submission
    if work == "no_transcript":
        (t.dir / TRANSCRIPT).unlink()
    _dead_pids(monkeypatch)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    t = Store(root).get("T-0001")
    assert res["ok"] and t.status == "ready" and int(t.rec["attempts"]) == 1, res     # returned -> relaunched (2026-10-14)
    assert [e["to"] for e in _state_events(root, "T-0001")][-2:] == ["returned", "ready"]
    assert len(_events(root, "session_dead", "T-0001")) == 1 and not _sys_events(root, "credits_exhausted")
    if work == "checkpoint":
        assert _events(root, "checkpoint_commit", "T-0001")
    assert not _credit_needs(res)


def test_credit_review_session_pauses_and_is_relaunched_after(world, monkeypatch):
    """A review transcript that holds the credit message opens the same pause (one event): no review starts while
    it stands, and once it is resumed the review is launched afresh."""
    root, _, _ = world
    _dispatched(root)
    spawn(root, ["T-0001"], lake=False)
    for st in ("in_progress", "submitted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    launches = _record_launches(monkeypatch)
    Watcher(root, quiet=True, auto_review=True, auto_decide=True).tick()
    assert len(launches) == 1 and "review:T-0001" in _flags(root)
    t = Store(root).get("T-0001")
    trs = sorted(t.dir.glob("review-session-*.txt"))
    assert trs, "the review launch names its transcript"
    trs[-1].write_text(CREDIT_MSG, encoding="utf-8")          # the review session exited on the credit wall
    res = Watcher(root, quiet=True, auto_review=True, auto_decide=True).tick()
    assert len(launches) == 1 and "review:T-0001" not in _flags(root), launches
    assert len(_sys_events(root, "credits_exhausted")) == 1 and len(_credit_needs(res)) == 1
    Watcher(root, quiet=True, auto_review=True, auto_decide=True).tick()
    assert len(launches) == 1 and len(_sys_events(root, "credits_exhausted")) == 1
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "credits_resumed")
    Watcher(root, quiet=True, auto_review=True, auto_decide=True).tick()
    assert len(launches) == 2 and Store(root).get("T-0001").status == "submitted"


def test_did_work_counts_only_this_launch():
    evs = [{"kind": "system", "name": "checkpoint_commit", "task": "T-1", "ts": "2026-09-23T09:00:00Z"}]
    log = "- [launcher] pid 7, transcript x\n- [scriber] old run\n- [launcher] pid 8, transcript y\n"
    assert not did_work(evs, "T-1", "2026-09-23T10:00:00Z", log, 8)
    assert did_work(evs, "T-1", "2026-09-23T08:00:00Z", log, 8)
    assert did_work([], "T-1", "2026-09-23T10:00:00Z", log, 7)


def test_credits_pause_key_is_read_and_mirrored():
    src = (REPO / "scr" / "watch.py").read_text(encoding="utf-8")
    limits = yaml.safe_load((CONFIG / "limits.yaml").read_text(encoding="utf-8"))
    assert 'self.limits.get("credits_pause_minutes", CREDITS_PAUSE_MINUTES)' in src
    assert limits["credits_pause_minutes"] == CREDITS_PAUSE_MINUTES


# ---- per-task background-wait ceiling (scr/bgwait.py) -------------------------------------------------------
# Headless sessions were killed 600 s after their turn ended while LPs still ran in the background (T-0015,
# T-0051, T-0226). budget.max_bg_wait_minutes sets CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS for that task's session;
# over 12 h it needs Omri: `task field` refuses other actors, and the launcher parks a hand-edited request.
from scr import bgwait  # noqa: E402


def test_bgwait_parse_rejects_zero_negative_and_junk():
    assert bgwait.parse(90) == 90 and bgwait.parse("90") == 90
    for bad in (0, -5, "x", None, True, 1.5):
        with pytest.raises(ValueError):
            bgwait.parse(bad)


def test_task_field_bg_wait_over_12h_is_omri_only(world):
    root, _, _ = world
    scr(root, "--quiet", "--actor", "steward:T-0001", "task", "field", "T-0001", bgwait.FIELD, "720")
    assert Store(root).get("T-0001").rec["budget"]["max_bg_wait_minutes"] == 720
    r = scr(root, "--quiet", "--actor", "intake", "task", "field", "T-0001", bgwait.FIELD, "721", check=False)
    assert r.returncode != 0 and "only Omri" in (r.stdout + r.stderr)
    assert Store(root).get("T-0001").rec["budget"]["max_bg_wait_minutes"] == 720
    r = scr(root, "--quiet", "--actor", "omri", "task", "field", "T-0001", bgwait.FIELD, "0", check=False)
    assert r.returncode != 0
    scr(root, "--quiet", "--actor", "omri", "task", "field", "T-0001", bgwait.FIELD, "1440")
    assert Store(root).get("T-0001").rec["budget"]["max_bg_wait_minutes"] == 1440


def test_launch_sets_the_ceiling_env_only_when_asked(world, monkeypatch):
    from scr.launch import launch
    root, _, _ = world
    calls = _record_launches(monkeypatch)
    _dispatched(root)
    launch(root, ["T-0001"], mode="headless")
    assert calls[-1]["env"] is None                       # no field: Claude Code's default, env untouched
    scr(root, "--quiet", "--actor", "steward:T-0001", "task", "field", "T-0001", bgwait.FIELD, "240")
    launch(root, ["T-0001"], mode="headless", relaunch=True)
    assert calls[-1]["env"][bgwait.ENV] == str(240 * 60_000)
    assert _events(root, "launch", "T-0001")[-1]["data"]["bg_wait_minutes"] == 240
    assert "background-wait ceiling 240 min" in (Store(root).get("T-0001").dir / "log.md").read_text(encoding="utf-8")


def test_launch_honours_omri_authorized_long_ceiling(world, monkeypatch):
    from scr.launch import launch
    root, _, _ = world
    calls = _record_launches(monkeypatch)
    scr(root, "--quiet", "--actor", "omri", "task", "field", "T-0001", bgwait.FIELD, "1800")
    _dispatched(root)
    launch(root, ["T-0001"], mode="headless")
    assert calls and calls[-1]["env"][bgwait.ENV] == str(1800 * 60_000)
    assert Store(root).get("T-0001").status == "in_progress"


def test_launch_parks_an_unauthorized_long_ceiling(world, monkeypatch):
    """A record edited by hand past the field guard: no session starts, the task goes to Omri's queue."""
    from scr.launch import launch
    root, _, _ = world
    calls = _record_launches(monkeypatch)
    t = Store(root).get("T-0001")
    t.rec.setdefault("budget", {})["max_bg_wait_minutes"] = 1800
    t.save()
    scr(root, "--quiet", "--actor", "steward:T-0001", "event", "system", "--name", "x")   # keep the log moving
    _dispatched(root)
    out = launch(root, ["T-0001"], mode="headless")
    assert not calls, calls
    assert any("NOT launched" in line for line in out), out
    assert Store(root).get("T-0001").status == "omri_queue"
    assert _events(root, "bg_wait_unauthorized", "T-0001")


def test_brief_names_the_ceiling(world):
    from scr.brief import brief_for
    root, _, _ = world
    st = Store(root)
    assert "10 min (the default)" in brief_for(root, st, st.get("T-0001"))
    scr(root, "--quiet", "--actor", "s", "task", "field", "T-0001", bgwait.FIELD, "300")
    st = Store(root)
    assert "300 min" in brief_for(root, st, st.get("T-0001"))


# ---- T-0247: every headless session has a known id, and Omri can take it over ---------------------------------
# claude 2.1.283 takes `--session-id <uuid>` with -p; `--resume <id>` finds the session only from the folder it was
# started in (checked 2026-09-27). Every launch site records the id in a `launch` event; `scr takeover` resumes it
# there, never while the headless pid runs; the watcher leaves a taken-over task alone.
import uuid  # noqa: E402

import scr.discuss as discuss_mod  # noqa: E402
from scr.events import EventLog  # noqa: E402
from scr.store import RepoError  # noqa: E402
from scr.watch import is_scriber_launch, takeover_of  # noqa: E402


def _sid(argv: list[str]) -> str:
    return argv[argv.index("--session-id") + 1]


def test_session_argv_carries_a_fresh_uuid_session_id(world, monkeypatch):
    import scr.run as run_mod
    root, repo, _ = world
    monkeypatch.setattr(run_mod, "claude_bin", lambda: FAKE_CLAUDE)
    a, b = run_mod.session_argv(repo, root=root), run_mod.session_argv(repo, root=root)
    for argv in (a, b):
        assert str(uuid.UUID(_sid(argv))) == _sid(argv)             # a valid, canonical UUID
    assert _sid(a) != _sid(b)                                        # two launches, two ids
    fixed = run_mod.new_session_id()
    assert _sid(run_mod.session_argv(repo, root=root, session_id=fixed)) == fixed


def test_every_launch_site_records_its_session_id(world, monkeypatch):
    """scriber (launch + auto-dispatch), review, tooling-review, intake, concierge: one `launch` event each whose
    session_id, pid and cwd are the ones the session really got."""
    from scr.answers import start_concierge
    root, _, _ = world
    sites = _five_call_sites(world, monkeypatch)
    calls = _record_launches(monkeypatch)
    start_concierge(root, "T-0001", "go ahead")
    sites["concierge"] = calls.pop()
    kinds = {"watch_dispatch": "scriber", "launch": "scriber", "start_review": "review",
             "start_tooling_review": "tooling-review", "intake": "intake", "concierge": "concierge"}
    launches = [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "system" and e.get("name") == "launch"]
    by_sid = {(e.get("data") or {}).get("session_id"): e for e in launches}
    sids = set()
    for name, c in sites.items():
        sid = _sid(c["argv"])
        sids.add(sid)
        e = by_sid.get(sid)
        assert e, (name, sid, [(x.get("data") or {}).get("session_id") for x in launches])
        d = e["data"]
        assert d["kind"] == kinds[name] and d["pid"] == _Proc.pid, (name, d)
        assert Path(d["cwd"]) == c["cwd"].resolve(), (name, d["cwd"], c["cwd"])
        assert d["add_dirs"] == _add_dirs(c["argv"]) and d["permission_mode"] == _mode(c["argv"]), (name, d)
        assert e.get("task") == (None if name == "intake" else "T-0001"), (name, e.get("task"))
    assert len(sids) == len(sites)                                   # every session its own id
    # the scriber's log.md names the id too, and only scriber launches are the watcher's session
    log = (Store(root).get("T-0001").dir / "log.md").read_text(encoding="utf-8")
    assert f"[launcher] session {_sid(sites['launch']['argv'])}" in log
    assert [is_scriber_launch(by_sid[_sid(sites[n]["argv"])]) for n in kinds] == [k == "scriber" for k in kinds.values()]


def _recorded(root: Path, cwd: Path, pid: int = 4242, tid: str = "T-0001", kind: str = "scriber", **extra) -> str:
    sid = str(uuid.uuid4())
    data = {"kind": kind, "session_id": sid, "pid": pid, "cwd": str(cwd.resolve()), "agent": "scriptorium-steward",
            "add_dirs": [str(root)], "permission_mode": "auto", "transcript": "session-20260927-000000.txt", **extra}
    scr(root, "--quiet", "--actor", f"steward:{tid}", "event", "system", "--name", "launch", "--task", tid,
        "--note", f"headless pid {pid}", "--data", json.dumps(data))
    return sid


def _fake_process(monkeypatch, alive: bool) -> list[str]:
    """pid_alive / kill / checkpoint in scr.watch (the watcher's kill path, which takeover uses) and resume_argv in
    scr.discuss replaced; returns the ordered calls. The fake pid dies when it is killed."""
    import scr.watch as w
    calls: list[str] = []
    state = {"alive": alive}
    monkeypatch.setattr(w, "pid_alive", lambda pid: state["alive"])
    monkeypatch.setattr(w, "process_created", lambda pid: None)

    def fake_kill(pid):
        calls.append(f"kill {pid}")
        state["alive"] = False
        return True
    monkeypatch.setattr(w, "kill", fake_kill)
    monkeypatch.setattr(w, "checkpoint", lambda *a, **k: calls.append("checkpoint"))
    real = discuss_mod.resume_argv

    def spy(*a, **k):
        calls.append("resume")
        return real(*a, **k)
    monkeypatch.setattr(discuss_mod, "resume_argv", spy)
    return calls


def test_takeover_stops_a_live_pid_before_building_the_resume(world, monkeypatch):
    root, repo, _ = world
    spawn(root, ["T-0001"], lake=False)                               # a worktree to checkpoint before the stop
    sid = _recorded(root, repo, pid=777001)
    calls = _fake_process(monkeypatch, alive=True)
    with pytest.raises(RepoError, match="still running"):             # never without --stop (the UI asks first)
        discuss_mod.takeover(root, "T-0001", open_terminal=False)
    assert calls == [] and not _sys_events(root, "takeover")
    out = discuss_mod.takeover(root, "T-0001", stop=True, open_terminal=False)
    assert calls.index("kill 777001") < calls.index("resume"), calls
    assert calls[0] == "checkpoint"                                   # the watcher's stop path: checkpoint, then kill
    ev = _sys_events(root, "takeover")
    assert len(ev) == 1 and ev[0]["task"] == "T-0001" and ev[0]["data"]["session_id"] == sid and ev[0]["data"]["stopped"] is True
    assert ev[0]["data"]["by"] == "omri" and any(line.startswith("resume: claude --resume " + sid) for line in out)


def test_takeover_of_an_ended_session_resumes_it_in_its_folder(world, monkeypatch):
    root, repo, _ = world
    folder = repo / ".worktrees"
    folder.mkdir(exist_ok=True)
    sid = _recorded(root, folder, pid=777002)
    calls = _fake_process(monkeypatch, alive=False)
    opened: list[tuple] = []
    monkeypatch.setattr(discuss_mod, "open_ps1_terminal", lambda ps1, title, cwd: opened.append((ps1, cwd)) or "terminal opened")
    monkeypatch.setattr(discuss_mod, "open_sh_terminal", lambda line: opened.append((line, None)) or "terminal opened")
    discuss_mod.takeover(root, "T-0001")
    assert calls == ["resume"]                                         # nothing to stop
    rec = discuss_mod.recorded_session(EventLog(root / "events" / "events.jsonl").read(), "T-0001")
    argv = discuss_mod.resume_argv(root, rec)
    assert argv == ["claude", "--resume", sid, "--agent", "scriptorium-steward", "--permission-mode", "auto", "--add-dir", str(root)]
    (script, cwd), = opened
    if cwd is not None:                                                # Windows: the launcher script
        text = Path(script).read_text(encoding="utf-8-sig")
        assert Path(cwd) == folder.resolve()
        assert f"Set-Location -LiteralPath '{folder.resolve()}'" in text
        assert " ".join(f"'{a}'" for a in argv) in text                 # claude --resume with the recorded flags
        assert "'takeover_ended'" in text                              # the terminal says when it closed
    else:
        assert str(folder.resolve()) in script and sid in script
    phone = discuss_mod.resume_argv(root, {"session_id": sid, "task": "T-0001"}, phone=True)
    assert phone[-2:] == ["--remote-control", "T-0001 takeover"]


def test_record_launch_records_created_for_every_kind(world, monkeypatch):
    """Not only the scriber: a review / intake / concierge launch carries its pid's creation time too, so a
    takeover can tell the session from a later process that reused its pid (T-0147)."""
    import scr.run as run_mod
    root, repo, _ = world
    monkeypatch.setattr(run_mod, "process_created", lambda pid: 1234500 + int(pid))
    run_mod.record_launch(root, "review", "T-0001", 17, str(uuid.uuid4()), repo, [])
    run_mod.record_launch(root, "scriber", "T-0001", 18, str(uuid.uuid4()), repo, [], data={"created": 99})
    run_mod.record_launch(root, "intake", None, None, str(uuid.uuid4()), root, [])
    ev = _sys_events(root, "launch")
    assert [e["data"].get("created") for e in ev] == [1234517, 99, None]   # the caller's own value is kept


def test_takeover_never_kills_a_reused_pid_of_a_review_session(world, monkeypatch):
    """A review that ended long ago; its pid now belongs to another live process (another creation time). The
    session is not alive, so --stop kills nothing: it resumes as already ended."""
    import scr.watch as w
    root, repo, _ = world
    sid = _recorded(root, repo, pid=777003, kind="review", created=111)
    calls = _fake_process(monkeypatch, alive=True)
    monkeypatch.setattr(w, "process_created", lambda pid: 222)
    rec = discuss_mod.recorded_session(EventLog(root / "events" / "events.jsonl").read(), "T-0001")
    assert not discuss_mod.session_alive(rec)
    out = discuss_mod.takeover(root, "T-0001", stop=True, open_terminal=False)
    assert calls == ["resume"], calls                                   # no kill of the stranger
    ev = _sys_events(root, "takeover")
    assert len(ev) == 1 and ev[0]["data"]["stopped"] is False and any(sid in line for line in out)


def test_takeover_refuses_to_stop_a_non_scriber_pid_with_no_creation_time(world, monkeypatch):
    """A review launch recorded before `created` was written for every kind: its live pid cannot be told from a
    reused one, so --stop refuses rather than taskkill /T /F on it."""
    root, repo, _ = world
    _recorded(root, repo, pid=777004, kind="review")
    calls = _fake_process(monkeypatch, alive=True)
    with pytest.raises(RepoError, match="no creation time"):
        discuss_mod.takeover(root, "T-0001", stop=True, open_terminal=False)
    assert calls == [] and not _sys_events(root, "takeover")


def test_takeover_of_a_session_nobody_recorded_is_refused(world):
    root, _, _ = world
    with pytest.raises(RepoError, match="no recorded session id"):
        discuss_mod.takeover(root, "T-0001", open_terminal=False)


def test_after_a_takeover_the_watcher_leaves_the_dead_pid_alone(world, monkeypatch):
    """The same no-work death that is returned without a takeover (the control test below) is left in_progress
    with one: no session_dead, no returned, no relaunch, no attempt spent, no stale."""
    root, _, _ = world
    _dying(root, transcript="partial answer\n")
    _dead_pids(monkeypatch)
    attempts = Store(root).get("T-0001").rec.get("attempts")
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover", "--task", "T-0001",
        "--note", "taken over", "--data", json.dumps({"session_id": str(uuid.uuid4()), "by": "omri"}))
    n_launch = len(_sys_events(root, "launch"))
    w = Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True, stale_minutes=0)
    for _ in range(2):
        res = w.tick()
    t = Store(root).get("T-0001")
    assert t.status == "in_progress" and t.rec.get("attempts") == attempts
    assert not _sys_events(root, "session_dead") and not _sys_events(root, "stale")
    assert len(_sys_events(root, "launch")) == n_launch                 # never relaunched
    row = next(r for r in res["rows"] if r["task"] == "T-0001")
    assert "TAKEOVER" in row["flags"] and "DEAD" not in row["flags"]
    # the terminal closed without a report: one needs line, still no death
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover_ended", "--task", "T-0001", "--note", "closed")
    res = w.tick()
    assert Store(root).get("T-0001").status == "in_progress" and not _sys_events(root, "session_dead")
    assert any("takeover session ended" in n for n in res["needs"])


def test_without_a_takeover_the_same_death_is_returned(world, monkeypatch):
    root, _, _ = world
    _dying(root, transcript="partial answer\n")
    _dead_pids(monkeypatch)
    Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status in {"returned", "ready"} and _sys_events(root, "session_dead")


def test_takeover_of_is_scoped_to_the_current_run():
    def ev(**k):
        return {"kind": "system", "task": "T-1", **k}
    assert takeover_of([ev(name="takeover", data={"session_id": "s"})], "T-1")["session_id"] == "s"
    assert takeover_of([ev(name="takeover"), ev(name="launch", data={"kind": "scriber"})], "T-1") is None
    assert takeover_of([ev(name="takeover"), ev(name="launch", data={"kind": "review"})], "T-1") is not None
    assert takeover_of([ev(name="takeover"), ev(name="takeover_aborted")], "T-1") is None
    assert takeover_of([ev(name="takeover"), {"kind": "state", "task": "T-1", "from": "dispatched", "to": "in_progress"}], "T-1") is None
    assert takeover_of([ev(name="takeover", ts="a"), ev(name="takeover_ended", ts="b")], "T-1")["ended"] == "b"
    # a takeover counts as the launcher's hold on a dispatched task: no dispatch timeout
    old = "2026-09-01T00:00:00Z"
    assert dispatch_stale([{"kind": "state", "task": "T-1", "to": "dispatched", "ts": old}, ev(name="takeover", ts=old)], "T-1", 30,
                          datetime.now(timezone.utc)) is None


# ---- T-0273: a takeover terminal closed with X still ends the takeover ------------------------------------------
# The launch script records its own process first (`scr takeover <id> --session <sid> --terminal-pid $PID`); closing
# the window kills it before the `takeover_ended` line runs, so the watcher writes that event itself, once.
from scr.watch import open_takeovers  # noqa: E402


def _taken_over(root: Path, monkeypatch, terminal_pid: int | None = 5555, created: int | None = 111) -> str:
    """T-0001 running under a takeover (its headless session dead, as after a --stop) whose terminal recorded
    `terminal_pid` with creation time `created` (through the same helper the CLI flag calls)."""
    import scr.watch as w
    _dying(root, transcript="partial answer\n")
    sid = str(uuid.uuid4())
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover", "--task", "T-0001",
        "--note", "taken over", "--data", json.dumps({"session_id": sid, "by": "omri"}))
    if terminal_pid is not None:
        monkeypatch.setattr(w, "process_created", lambda pid: created)
        discuss_mod.takeover_terminal(root, "T-0001", sid, terminal_pid)
    return sid


def _terminal(monkeypatch, alive: bool, created: int | None = 111) -> None:
    """The recorded terminal pid is `alive` with creation time `created`; every other pid (the stopped headless
    session) is dead."""
    import scr.watch as w
    monkeypatch.setattr(w, "pid_alive", lambda pid: alive and int(pid) == 5555)
    monkeypatch.setattr(w, "process_created", lambda pid: created if int(pid) == 5555 else None)


def _takeover_needs(res: dict) -> list[str]:
    return [n for n in res["needs"] if "takeover" in n]


def test_a_closed_takeover_terminal_is_ended_by_the_watcher_once(world, monkeypatch):
    root, _, _ = world
    _taken_over(root, monkeypatch)
    attempts = Store(root).get("T-0001").rec.get("attempts")
    n_launch = len(_sys_events(root, "launch"))
    _terminal(monkeypatch, alive=False)
    w = Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True, stale_minutes=0)
    for _ in range(2):
        res = w.tick()
        needs = _takeover_needs(res)
        assert len(needs) == 1 and "terminal was closed" in needs[0], res["needs"]
        assert [i["key"] for i in w.need_items if "takeover" in i["text"]] == ["takeover:T-0001"]
    ended = _sys_events(root, "takeover_ended")
    assert len(ended) == 1, ended
    assert ended[0]["actor"] == "watcher" and ended[0]["note"] == "terminal closed" and ended[0]["task"] == "T-0001"
    assert ended[0]["data"]["pid"] == 5555 and "no longer exists" in ended[0]["data"]["reason"]
    # Omri decides: no relaunch, no attempt, no death, no stale
    t = Store(root).get("T-0001")
    assert t.status == "in_progress" and t.rec.get("attempts") == attempts
    assert len(_sys_events(root, "launch")) == n_launch and not _sys_events(root, "session_dead") and not _sys_events(root, "stale")
    assert takeover_of(EventLog(root / "events" / "events.jsonl").read(), "T-0001")["ended_by"] == "watcher"


def test_a_live_takeover_terminal_is_left_alone_and_a_reused_pid_ends_it(world, monkeypatch):
    root, _, _ = world
    _taken_over(root, monkeypatch, created=111)
    w = Watcher(root, quiet=True, auto_decide=True, stale_minutes=0)
    _terminal(monkeypatch, alive=True, created=111)                   # the same process: Omri is still in it
    for _ in range(2):
        res = w.tick()
    assert not _sys_events(root, "takeover_ended") and not _takeover_needs(res)
    row = next(r for r in res["rows"] if r["task"] == "T-0001")
    assert row["flags"].split().count("TAKEOVER") == 1 and "ended" not in row["flags"]
    _terminal(monkeypatch, alive=True, created=222)                   # the pid now belongs to another process
    res = w.tick()
    ended = _sys_events(root, "takeover_ended")
    assert len(ended) == 1 and ended[0]["actor"] == "watcher" and "reused" in ended[0]["data"]["reason"]
    assert len(_takeover_needs(res)) == 1


def test_a_takeover_closed_normally_gets_no_second_ended_event(world, monkeypatch):
    root, _, _ = world
    _taken_over(root, monkeypatch)
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover_ended", "--task", "T-0001",
        "--note", "takeover terminal closed")                        # the script's own line after claude exited
    _terminal(monkeypatch, alive=False)                               # and then the window was closed
    w = Watcher(root, quiet=True, auto_decide=True, stale_minutes=0)
    for _ in range(2):
        res = w.tick()
    ended = _sys_events(root, "takeover_ended")
    assert len(ended) == 1 and ended[0]["actor"] == "omri"
    needs = _takeover_needs(res)
    assert len(needs) == 1 and "takeover session ended" in needs[0], needs


def test_a_takeover_with_no_terminal_recorded_is_left_alone(world, monkeypatch):
    """A takeover opened before T-0273 (or whose script has not recorded its pid yet): nothing to check."""
    root, _, _ = world
    _taken_over(root, monkeypatch, terminal_pid=None)
    _terminal(monkeypatch, alive=False)
    Watcher(root, quiet=True, auto_decide=True, stale_minutes=0).tick()
    assert not _sys_events(root, "takeover_ended")


def test_a_closed_takeover_of_a_session_with_no_task_is_ended_too(world, monkeypatch):
    """An intake session (no task): the takeover is found by its session id, and the script's own takeover_ended,
    which names the session only in its note, closes it."""
    import scr.watch as w
    root, _, _ = world
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    for s in (sid, other):
        scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover", "--note", "taken over",
            "--data", json.dumps({"session_id": s, "by": "omri", "kind": "intake"}))
        monkeypatch.setattr(w, "process_created", lambda pid: 111)
        discuss_mod.takeover_terminal(root, None, s, 5555)
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover_ended",
        "--note", f"takeover terminal of session {other} closed")
    assert [tk["session_id"] for tk in open_takeovers(EventLog(root / "events" / "events.jsonl").read())] == [sid]
    _terminal(monkeypatch, alive=False)
    wt = Watcher(root, quiet=True, stale_minutes=0)
    res = wt.tick()
    assert [n for n in _takeover_needs(res) if sid in n]
    wt.tick()
    ended = [e for e in _sys_events(root, "takeover_ended") if e["actor"] == "watcher"]
    assert len(ended) == 1 and ended[0].get("task") is None and ended[0]["data"]["session_id"] == sid


def test_takeover_of_carries_the_terminal_and_who_ended_it():
    def ev(**k):
        return {"kind": "system", "task": "T-1", **k}
    to = ev(name="takeover", ts="a", data={"session_id": "s"})
    term = ev(name="takeover_terminal", data={"session_id": "s", "pid": 9, "created": 7})
    tk = takeover_of([to, term], "T-1")
    assert (tk["pid"], tk["created"], tk["ended"]) == (9, 7, None)
    assert takeover_of([to, ev(name="takeover_terminal", data={"session_id": "x", "pid": 9})], "T-1")["pid"] is None
    tk = takeover_of([to, term, ev(name="takeover_ended", ts="b", actor="watcher"), ev(name="takeover_ended", ts="c", actor="omri")], "T-1")
    assert (tk["ended"], tk["ended_by"]) == ("b", "watcher")          # the first end is the end
    assert open_takeovers([to, term]) and not open_takeovers([to, term, ev(name="takeover_ended", ts="b")])


def test_takeover_script_records_its_terminal_before_resuming(world, monkeypatch):
    """The launch script names its own process before `claude --resume` (the resume line itself unchanged), and
    `scr takeover --terminal-pid` records that pid with its real creation time."""
    import scr.run as run_mod
    root, repo, _ = world
    sid = _recorded(root, repo, pid=777005)
    _fake_process(monkeypatch, alive=False)
    opened: list[tuple] = []
    monkeypatch.setattr(discuss_mod, "open_ps1_terminal", lambda ps1, title, cwd: opened.append((ps1, cwd)) or "terminal opened")
    monkeypatch.setattr(discuss_mod, "open_sh_terminal", lambda line: opened.append((line, None)) or "terminal opened")
    discuss_mod.takeover(root, "T-0001")
    (script, cwd), = opened
    text = Path(script).read_text(encoding="utf-8-sig") if cwd is not None else script
    argv = discuss_mod.resume_argv(root, discuss_mod.recorded_session(EventLog(root / "events" / "events.jsonl").read(), "T-0001"))
    if cwd is not None:
        lines = text.splitlines()
        rec = next(i for i, l in enumerate(lines) if "'--terminal-pid' $PID" in l)
        res = next(i for i, l in enumerate(lines) if " ".join(f"'{a}'" for a in argv) in l)
        assert rec < res and f"'{sid}'" in lines[rec] and "'takeover'" in lines[rec]
    else:
        assert text.index("--terminal-pid") < text.index("--resume")
    scr(root, "--quiet", "--actor", "omri", "takeover", "T-0001", "--session", sid, "--terminal-pid", str(os.getpid()))
    ev, = _sys_events(root, "takeover_terminal")
    assert ev["task"] == "T-0001" and ev["data"]["session_id"] == sid and ev["data"]["pid"] == os.getpid()
    assert ev["data"]["created"] == run_mod.process_created(os.getpid())
    assert takeover_of(EventLog(root / "events" / "events.jsonl").read(), "T-0001")["pid"] == os.getpid()


def test_ui_offers_take_over_for_a_recorded_session(world, monkeypatch):
    """The task page (and the running / recent rows, same helper) carries the newest recorded session; `alive`
    is what makes the Take over button ask before it stops the pid."""
    import scr.watch as w
    root, repo, _ = world
    assert ui_mod.task_detail(root, "T-0001")["session"] is None           # launched before T-0247: no button
    sid = _recorded(root, repo, pid=777003)
    monkeypatch.setattr(w, "pid_alive", lambda pid: True)
    monkeypatch.setattr(w, "process_created", lambda pid: None)
    s = ui_mod.task_detail(root, "T-0001")["session"]
    assert s == {"session_id": sid, "kind": "scriber", "pid": 777003, "alive": True, "taken_over": False}
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover", "--task", "T-0001",
        "--note", "taken over", "--data", json.dumps({"session_id": sid, "by": "omri"}))
    assert ui_mod.session_cards(root, {"T-0001"})["T-0001"]["taken_over"] is True


# ---- T-0121: an over-wall task is escalated, not held ----------------------------------------------------------
# `budget.max_wall_hours` is per task. The running loop raises the one `budget_wall` diagnostic per in_progress
# start; with auto_decide on, `escalation_checks` then routes the task through `_escalate` on the same tick (one
# `slot_freed` card), and auto-dispatch refills the slot. The in_progress start is backdated with T-0122's
# `_backdate`, so the wall is a real 1 h budget, not 0.
from scr.watch import wall_hours, wall_stale  # noqa: E402


def _over_wall(root: Path, tid: str = "T-0001", hours: float = 1, minutes: float = 90, spent: bool = True) -> None:
    _running(root, tid)
    scr(root, "--quiet", "--actor", "s", "task", "field", tid, "budget.max_wall_hours", str(hours))
    _backdate(root, tid, "in_progress", minutes)
    if spent:                                   # 2026-10-14: the first two exhaustions extend the wall; these tests are about the third
        p = root / ".scr" / "watch.state.txt"
        p.parent.mkdir(exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"autofix:{tid}:wall:1\nautofix:{tid}:wall:2\n")


def test_over_wall_is_extended_and_relaunched_twice_before_escalating(world, monkeypatch):
    """2026-10-14: the first wall exhaustion doubles budget.max_wall_hours and relaunches the task from its
    checkpoint (returned -> ready, auto_fix event); no card. The third one is escalated as before."""
    root, _, _ = world
    _over_wall(root, spent=False)
    _mock_session(monkeypatch, alive=False)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    t = Store(root).get("T-0001")
    assert res["ok"] and t.status == "ready" and not res["escalated"], res
    assert t.rec["budget"]["max_wall_hours"] == 2.0
    assert len(_events(root, "auto_fix", "T-0001")) == 1 and not _events(root, "slot_freed", "T-0001")
    assert not [n for n in res["needs"] if n.startswith("T-0001 ")], res["needs"]


def test_stuck_wall_stale_is_a_pure_function_of_the_start_and_the_budget():
    start = NOW - timedelta(hours=4)
    assert wall_stale(start, 3.0, NOW) == {"since": start, "age_h": 4.0}
    assert wall_stale(start, 4.0, NOW) is None                                   # exactly at the wall: not over
    assert wall_stale(NOW - timedelta(minutes=183), 3.0, NOW) is None             # 3.05 h rounds to the 3.0 h the flag shows
    assert wall_stale(None, 0.0, NOW) is None                                     # undated (still dispatched): never early
    assert wall_hours({"budget": {"max_wall_hours": 1.5}}) == 1.5 and wall_hours({}) == 6.0


def test_stuck_over_wall_is_escalated_and_the_slot_refilled(world, monkeypatch):
    """DoD: in_progress task 1.5 h past its dispatched -> in_progress start with max_wall_hours 1, auto_decide
    on, cap 1, one ready task -> after one tick() escalated as steward:<id>, one budget_wall, one slot_freed
    naming max_wall_hours and `task set <id> returned`, and the ready task dispatched on the same tick."""
    root, _, _ = world
    _set_limit(root, "concurrency", 1)
    _over_wall(root)
    _ready_task(root, "T-0002", "QuantumInsertion/B.lean")
    launches = _record_launches(monkeypatch)
    calls = _spy_scr(monkeypatch)
    res = Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()
    assert res["ok"], res
    assert Store(root).get("T-0001").status == "escalated"
    last = _state_events(root, "T-0001")[-1]
    assert (last["to"], last["actor"]) == ("escalated", "steward:T-0001"), last
    assert "wall budget exhausted: 1.5 h" in last["note"] and "max_wall_hours 1" in last["note"], last["note"]
    assert len(_events(root, "budget_wall", "T-0001")) == 1
    ev = _events(root, "slot_freed", "T-0001")
    assert len(ev) == 1 and ev[0]["actor"] == "steward:T-0001", ev
    note = ev[0]["note"]
    assert note.startswith("ACTION for Omri: extend the wall") and "max_wall_hours" in note and "task set T-0001 returned" in note, note
    assert "or decide in the gate card" in note and "1.5 h" in note and "max_wall_hours 1" in note, note
    # the trail reads budget_wall -> escalated -> slot_freed, and the dispatch came after it, on this tick
    i_wall = next(i for i, (_, a) in enumerate(calls) if a[:2] == ("event", "system") and "budget_wall" in a)
    i_esc = next(i for i, (_, a) in enumerate(calls) if a[:2] == ("task", "set") and a[2:4] == ("T-0001", "escalated"))
    i_slot = next(i for i, (_, a) in enumerate(calls) if a[:2] == ("event", "system") and "slot_freed" in a)
    i_disp = next(i for i, (_, a) in enumerate(calls) if a[0] == "dispatch")
    assert i_wall < i_esc < i_slot < i_disp, calls
    assert Store(root).get("T-0002").status == "in_progress" and len(launches) == 1
    assert res["escalated"][0]["task"] == "T-0001" and "wall budget exhausted" in res["escalated"][0]["reason"]
    assert not any("over wall budget" in n for n in res["needs"]), res["needs"]      # escalated: the card, not a held line


def test_stuck_over_wall_escalates_once_per_episode(world, monkeypatch):
    """The same world ticked twice: still one slot_freed and one budget_wall; the flags name the in_progress start."""
    root, _, _ = world
    _over_wall(root)
    _record_launches(monkeypatch)
    for _ in range(2):
        Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()
    assert Store(root).get("T-0001").status == "escalated"
    assert len(_events(root, "slot_freed", "T-0001")) == 1 and len(_events(root, "budget_wall", "T-0001")) == 1
    start = Watcher(root, quiet=True).started_at("T-0001").astimezone(timezone.utc)
    assert {f"slot_freed:T-0001:{start:%Y%m%dT%H%M%SZ}", f"wall:T-0001:{start:%Y%m%dT%H%M%SZ}"} <= _flags(root)


def test_stuck_over_wall_auto_decide_off_holds(world):
    """auto_decide off: the task stays in_progress; one budget_wall event and the needs line every tick, as before."""
    root, _, _ = world
    _over_wall(root)
    for _ in range(2):
        res = Watcher(root, quiet=True).tick()
        assert Store(root).get("T-0001").status in {"in_progress", "checkpoint"} and not res["escalated"]
        assert any(n.startswith("T-0001 over wall budget (1.5 h of 1.0 h running time)") for n in res["needs"]), res["needs"]
    assert len(_events(root, "budget_wall", "T-0001")) == 1 and not _events(root, "slot_freed", "T-0001")


def test_stuck_over_wall_with_a_report_is_submitted_not_escalated(world):
    """A finished session is never stuck: report.md written wins over the wall (the tick's submission branch)."""
    root, _, _ = world
    _over_wall(root)
    (Store(root).get("T-0001").dir / "report.md").write_text("# Report\n\n## Result\nProved a.\n", encoding="utf-8")
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and Store(root).get("T-0001").status == "submitted" and not res["escalated"], res
    assert not _events(root, "slot_freed", "T-0001")


def test_stuck_over_wall_under_takeover_is_held(world):
    """A task Omri took over (T-0247) is his run: over the wall it is flagged, never escalated (its session is his)."""
    root, _, _ = world
    _over_wall(root)
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover", "--task", "T-0001",
        "--note", "taken over", "--data", json.dumps({"session_id": "s", "by": "omri"}))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status in {"in_progress", "checkpoint"} and not res["escalated"]
    assert any(n.startswith("T-0001 over wall budget") for n in res["needs"]) and not _events(root, "slot_freed", "T-0001")


# ---- T-0342: the wall is running time; a `waiting` span (a job running detached) is not charged ---------------
# 2026-10-12: T-0136 was escalated "13.3 h in progress" 27 s after its resume from J-0015; almost all of it was
# `waiting`. The wall age is now the sum of the in_progress/checkpoint spans since the dispatched -> in_progress start.
from scr.watch import running_hours, running_wall  # noqa: E402


def _st(tid: str, frm: str, to: str, at: datetime) -> dict:
    return {"kind": "state", "task": tid, "from": frm, "to": to, "ts": at.isoformat().replace("+00:00", "Z")}


def test_running_wall_excludes_waiting_spans():
    t0 = NOW - timedelta(hours=13.5)
    evs = [_st("T-1", "ready", "dispatched", t0 - timedelta(minutes=1)), _st("T-1", "dispatched", "in_progress", t0),
           _st("T-1", "in_progress", "waiting", t0 + timedelta(hours=1)), _st("T-1", "waiting", "in_progress", t0 + timedelta(hours=13)),
           _st("T-2", "dispatched", "in_progress", t0 - timedelta(hours=40))]          # another task's clock is not ours
    assert running_hours(evs, "T-1", NOW) == 1.5
    assert running_wall(evs, "T-1", 2.0, NOW) is None
    later = NOW + timedelta(hours=1)                                                    # t0 + 14.5 h: 2.5 h running
    assert running_wall(evs, "T-1", 2.0, later) == {"since": t0, "age_h": 2.5}
    assert running_hours(evs, "T-1", later) == 2.5
    assert running_wall(evs, "T-3", 0.0, NOW) is None and running_hours(evs, "T-3", NOW) is None   # never started
    # while still waiting, the clock is stopped at the 1 h it ran
    assert running_hours(evs[:3], "T-1", NOW) == 1.0


def test_running_wall_heartbeats_neither_reset_nor_double_count():
    t0 = NOW - timedelta(hours=3)
    h = lambda x: t0 + timedelta(hours=x)  # noqa: E731
    evs = [_st("T-1", "dispatched", "in_progress", t0), _st("T-1", "in_progress", "checkpoint", h(0.5)),
           _st("T-1", "checkpoint", "in_progress", h(1)), _st("T-1", "in_progress", "checkpoint", h(1.5)),
           _st("T-1", "checkpoint", "in_progress", h(2)), _st("T-1", "in_progress", "in_progress", h(2.2)),
           _st("T-1", "in_progress", "checkpoint", h(2.5))]
    assert running_hours(evs, "T-1", NOW) == 3.0                                        # all 3 h, counted once
    # checkpoint -> waiting -> in_progress: the waiting hour drops out, the checkpoint spans stay
    evs2 = evs[:4] + [_st("T-1", "checkpoint", "waiting", h(1.5)), _st("T-1", "waiting", "in_progress", h(2.5))]
    assert running_hours(evs2, "T-1", NOW) == 2.0
    # a new dispatched -> in_progress starts a new episode at zero
    evs3 = evs + [_st("T-1", "checkpoint", "returned", h(2.8)), _st("T-1", "ready", "dispatched", h(2.9)),
                  _st("T-1", "dispatched", "in_progress", h(2.9))]
    assert running_wall(evs3, "T-1", 0.0, NOW) == {"since": h(2.9), "age_h": 0.1}


def _set_state_times(root: Path, tid: str, minutes_ago: list[float]) -> None:
    """Date the last len(minutes_ago) state events of `tid` that many minutes before now, re-chaining after them."""
    p = root / "events" / "events.jsonl"
    evs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    idx = [i for i, e in enumerate(evs) if e.get("kind") == "state" and e.get("task") == tid][-len(minutes_ago):]
    now = datetime.now(timezone.utc)
    for i, m in zip(idx, minutes_ago):
        evs[i]["ts"] = (now - timedelta(minutes=m)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    prev = evs[idx[0] - 1]["h"] if idx[0] else ZERO
    for e in evs[idx[0]:]:
        e["prev"] = prev
        e["h"] = prev = event_hash(e)
    p.write_text("\n".join(canon(e) for e in evs) + "\n", encoding="utf-8")
    scr(root, "--quiet", "snapshot")


def _waited(root: Path, running_min: float, tid: str = "T-0001") -> None:
    """dispatched -> in_progress at t0, -> waiting at t0+1h, -> in_progress at t0+13h; now = t0+13h+running_min."""
    _over_wall(root, tid, hours=2)
    for st in ("waiting", "in_progress"):
        scr(root, "--quiet", "--actor", "s", "task", "set", tid, st)
    _set_state_times(root, tid, [13 * 60 + running_min, 12 * 60 + running_min, running_min])


def test_wall_waiting_on_a_job_is_not_charged(world):
    """DoD: 1 h running, 12 h waiting, 0.5 h running with max_wall_hours 2 -> no escalation; wall age 1.5 h."""
    root, _, _ = world
    _waited(root, 30)
    w = Watcher(root, quiet=True, auto_decide=True)
    res = w.tick()
    assert Store(root).get("T-0001").status in {"in_progress", "checkpoint"} and not res["escalated"], res
    assert running_hours(w.read_events(), "T-0001", datetime.now(timezone.utc)) == 1.5
    assert not _events(root, "budget_wall", "T-0001") and not _events(root, "slot_freed", "T-0001")
    assert not any("over wall budget" in n for n in res["needs"]), res["needs"]


def test_wall_running_time_over_budget_escalates_once_quoting_running_time(world):
    """DoD: the same task 2.5 h running (14.5 h elapsed) is escalated once; the reason and the slot_freed card
    quote 2.5 h of running time, not 14.5 h."""
    root, _, _ = world
    _waited(root, 90)
    for _ in range(2):
        Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "escalated"
    last = _state_events(root, "T-0001")[-1]
    assert "wall budget exhausted: 2.5 h running time" in last["note"] and "14.5" not in last["note"], last["note"]
    assert "max_wall_hours 2" in last["note"], last["note"]
    ev = _events(root, "slot_freed", "T-0001")
    assert len(ev) == 1 and "2.5 h running time" in ev[0]["note"] and "14.5" not in ev[0]["note"], ev
    bw = _events(root, "budget_wall", "T-0001")
    assert len(bw) == 1 and bw[0]["note"] == "2.5 h", bw


def test_wall_rule_is_written_in_the_agent_docs():
    text = (REPO / "agents" / "_common.md").read_text(encoding="utf-8")
    assert any("max_wall_hours" in l and "watcher" in l for l in text.splitlines())


# ---- T-0127: a launch that keeps failing costs no attempts and raises one needs line -----------------------
# After T-0122 a dispatch with no launch goes back to ready after dispatch_timeout_minutes and auto-dispatch may
# pick it again; `scr dispatch` charged an attempt each round (attempts = dispatches), so three failed launches
# spent the whole budget with no scriber run, silently. Now the launcher's dispatched -> in_progress charges the
# attempt, and dispatch_timeout_rounds rounds in a row raise one needs line quoting the launcher's reason.
from scr.watch import DISPATCH_TIMEOUT_ROUNDS, dispatch_rounds, launcher_reason  # noqa: E402


def _attempts(root: Path, tid: str = "T-0001") -> int:
    return int(Store(root).get(tid).rec.get("attempts", 0))


def _field_events(root: Path, tid: str, field: str) -> list[dict]:
    from scr.events import EventLog
    return [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "field" and e.get("task") == tid and e.get("field") == field]


def _no_claude(monkeypatch) -> None:
    """The launcher finds no claude: launch() logs `claude (Claude Code CLI) not on PATH — nothing launched`."""
    import scr.launch as launch_mod
    monkeypatch.setattr(launch_mod, "claude_bin", lambda: None)


def _round_needs(res: dict, tid: str = "T-0001") -> list[str]:
    return [n for n in res["needs"] if n.startswith(f"{tid} was dispatched") and "dispatch_timeout_rounds" in n]


def test_dispatch_rounds_is_a_pure_function_of_the_log():
    to = {"kind": "system", "name": "dispatch_timeout", "task": "T-1"}
    launch = {"kind": "system", "name": "launch", "task": "T-1", "data": {"kind": "scriber"}}
    review = {"kind": "system", "name": "launch", "task": "T-1", "data": {"kind": "review"}}
    assert dispatch_rounds([], "T-1") == 0
    assert dispatch_rounds([to, to, to], "T-1") == 3
    assert dispatch_rounds([to, to, launch, to], "T-1") == 1                         # a launch resets the count
    assert dispatch_rounds([to, review, to], "T-1") == 2                             # a review session is no scriber launch
    assert dispatch_rounds([to, {**to, "task": "T-2"}, {**launch, "task": "T-2"}], "T-1") == 1
    assert dispatch_rounds([to, {"kind": "system", "name": "takeover", "task": "T-1"}], "T-1") == 0


def test_launcher_reason_reads_the_newest_run_that_names_the_task():
    log = "\n".join([
        "2026-09-29T01:00:00 auto-dispatch: dispatched T-0001 -> task/T-0001 (attempt 1 if it launches)",
        "2026-09-29T01:00:00   launcher: launching: T-0001 (mode headless)",
        "2026-09-29T01:00:00   launcher: claude (Claude Code CLI) not on PATH — nothing launched",
        "2026-09-29T01:00:01 tick",
        "2026-09-29T02:00:00   launcher: launching: T-0001, T-0002 (mode headless)",
        "2026-09-29T02:00:00   launcher: === T-0001 -> C:/wt/T-0001 (task/T-0001)",
        "2026-09-29T02:00:00   launcher:   exists, reusing",
        "2026-09-29T02:00:00   launcher: === T-0002 [dispatched] C:/wt/T-0002",
        "2026-09-29T02:00:00   launcher:   worktree missing — skipped",
        "2026-09-29T02:00:00   launcher: === T-0001 [dispatched] C:/wt/T-0001",
        "2026-09-29T02:00:00   launcher:   worktree missing — skipped",
        "2026-09-29T02:00:01 tick",
        "2026-09-29T03:00:00   launcher: launching: T-0002 (mode headless)",
        "2026-09-29T03:00:00   launcher: === T-0002 NOT launched: bg_wait_minutes = 900 min is over 12 h and was not set by Omri",
    ])
    assert launcher_reason(log, "T-0001") == "worktree missing — skipped (launcher, 2026-09-29T02:00:00)"
    assert launcher_reason(log, "T-0002").startswith("=== T-0002 NOT launched: bg_wait_minutes")
    first = "\n".join(log.splitlines()[:4])
    assert launcher_reason(first, "T-0001") == "claude (Claude Code CLI) not on PATH — nothing launched (launcher, 2026-09-29T01:00:00)"
    assert launcher_reason(log, "T-0003") is None                                    # no run names it
    assert launcher_reason(log, "T-000") is None                                     # an id prefix is not the task
    ok = "2026-09-29T04:00:00   launcher: launching: T-0004 (mode headless)\n2026-09-29T04:00:00   launcher:   headless pid 7; transcript x"
    assert launcher_reason(ok, "T-0004") is None                                     # the run names no failure


def test_dispatch_that_never_launched_costs_no_attempt(world, monkeypatch):
    """DoD: a task dispatched by `scr dispatch` and bounced by dispatch_checks (no launch event) has the attempts it
    had before the dispatch; dispatched again and launched (the launcher's dispatched -> in_progress, start_claude
    mocked) it has one more, with one `field attempts` event at the launch and none at a dispatch."""
    root, _, _ = world
    before = _attempts(root)
    out = scr(root, "--actor", "prioritizer", "dispatch").stdout
    assert f"dispatched T-0001 -> task/T-0001 (attempt {before + 1} if it launches)" in out, out
    assert Store(root).get("T-0001").status == "dispatched" and _attempts(root) == before
    assert _state_events(root, "T-0001")[-1]["note"] == f"attempt {before + 1} if it launches"
    assert not _field_events(root, "T-0001", "attempts")
    _backdate(root, "T-0001", "dispatched", 45)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and res["timed_out"][0]["task"] == "T-0001", res
    assert Store(root).get("T-0001").status == "ready" and _attempts(root) == before
    assert not _field_events(root, "T-0001", "attempts")
    launches = _record_launches(monkeypatch)
    res = Watcher(root, quiet=True, auto_dispatch=True).tick()
    assert res["ok"] and len(launches) == 1, res
    t = Store(root).get("T-0001")
    assert t.status == "in_progress" and _attempts(root) == before + 1
    assert [e["to"] for e in _state_events(root, "T-0001")][-2:] == ["dispatched", "in_progress"]
    assert [e["value"] for e in _field_events(root, "T-0001", "attempts")] == [before + 1]
    assert len(_events(root, "launch", "T-0001")) == 1
    assert scr(root, "verify", "--quiet").returncode == 0


def test_dispatch_at_max_attempts_after_real_launches_is_unchanged(world, monkeypatch):
    """DoD: attempts == max_attempts after real launches -> `scr dispatch` still dispatches it (the budget rule is
    agents/_common.md's, applied by the Steward, not by dispatch), and its launch is attempt max + 1, as before."""
    root, _, _ = world
    budget = int(Store(root).get("T-0001").rec["budget"]["max_attempts"])
    for i in range(budget):                                  # budget real runs, each launched and returned
        if i:
            for st in ("returned", "ready"):
                scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
        _dispatched(root)
        scr(root, "--quiet", "--actor", "steward:T-0001", "task", "set", "T-0001", "in_progress")
        _launched(root, pid=5000 + i)
    for st in ("returned", "ready"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    assert _attempts(root) == budget
    out = scr(root, "--actor", "prioritizer", "dispatch").stdout
    assert f"dispatched T-0001 -> task/T-0001 (attempt {budget + 1} if it launches)" in out, out
    assert Store(root).get("T-0001").status == "dispatched" and _attempts(root) == budget
    scr(root, "--quiet", "--actor", "steward:T-0001", "task", "set", "T-0001", "in_progress")
    assert _attempts(root) == budget + 1
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", "checkpoint")
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", "in_progress")
    assert _attempts(root) == budget + 1                     # the heartbeat checkpoint -> in_progress charges nothing


def test_repeated_dispatch_timeouts_raise_one_needs_line(world, monkeypatch):
    """DoD: dispatch_timeout_rounds (3) dispatch_timeout rounds in a row with no launch -> exactly one needs line
    naming the task, the round count and the launcher's reason from reports/steward_watch.log (here the real
    auto-dispatch branch logging launch()'s `not on PATH` line); a fourth round adds no second line or card; the
    task is never escalated and no round costs an attempt; a launch event resets the count and the line goes."""
    root, _, _ = world
    limits = yaml.safe_load((root / "config" / "limits.yaml").read_text(encoding="utf-8"))
    assert int(limits["dispatch_timeout_rounds"]) == DISPATCH_TIMEOUT_ROUNDS == 3
    _no_claude(monkeypatch)
    before = _attempts(root)
    _dispatched(root)
    w = lambda: Watcher(root, quiet=True, auto_decide=True, auto_dispatch=True).tick()   # noqa: E731
    for k in (1, 2):
        _backdate(root, "T-0001", "dispatched", 45)
        res = w()
        assert res["ok"] and res["timed_out"][0]["task"] == "T-0001", res
        assert Store(root).get("T-0001").status == "dispatched"          # redispatched on the same tick, launch failed
        assert not _round_needs(res), res["needs"]
    assert "launcher: claude (Claude Code CLI) not on PATH — nothing launched" in (root / LOG).read_text(encoding="utf-8")
    _backdate(root, "T-0001", "dispatched", 45)
    res = w()
    lines = _round_needs(res)
    assert len(lines) == 1, res["needs"]
    assert "T-0001 was dispatched 3 times in a row and never launched (dispatch_timeout_rounds 3)" in lines[0]
    assert "the launcher says: claude (Claude Code CLI) not on PATH — nothing launched (launcher, " in lines[0]
    assert "redispatched automatically" in lines[0] and "fix the launcher, not the task" in lines[0]
    assert [n["key"] for n in _needs_file(root)["needs"]].count("dispatch_rounds:T-0001") == 1
    _backdate(root, "T-0001", "dispatched", 45)
    res = w()                                                                     # round 4
    assert len(_events(root, "dispatch_timeout", "T-0001")) == 4
    assert len(_round_needs(res)) == 1 and "dispatched 4 times" in _round_needs(res)[0]
    assert [n["key"] for n in _needs_file(root)["needs"]].count("dispatch_rounds:T-0001") == 1   # the same card
    assert (root / LOG).read_text(encoding="utf-8").count("rounds in a row without a launch") == 1
    assert _attempts(root) == before and not res["escalated"] and not _sys_events(root, "slot_freed")
    _launched(root)                                                               # a launch succeeds
    res = w()
    assert res["ok"] and not _round_needs(res) and "dispatch_rounds:T-0001" not in [n["key"] for n in _needs_file(root)["needs"]]
    assert "dispatch_rounds:T-0001" not in _flags(root)
    assert scr(root, "verify", "--quiet").returncode == 0


def test_dispatch_records_the_worktree_the_watcher_resolves(world):
    """T-0153 review, item 1: a record whose workload key is not registered gets from `scr dispatch` the same
    worktree directory ops.task_worktree (the watcher, the launcher) resolves: the first entry's root."""
    root, _, _ = world
    t = Store(root).get("T-0001")
    t.rec["workload"] = "not-registered"
    t.save()
    scr(root, "--quiet", "snapshot")
    scr(root, "--quiet", "--actor", "prioritizer", "dispatch")
    t = Store(root).get("T-0001")
    assert t.status == "dispatched"
    assert (root / t.rec["worktree"]).resolve() == task_worktree(workload_for(root, Store(root), t), t).resolve()
    assert t.rec["worktree"] == "../quantum-insertion/.worktrees/T-0001"          # the entry's string form


# ---- T-0272: the freeze switch -----------------------------------------------------------------------------
# `frozen: true` on a workload in config/workloads.yaml: no auto-dispatch (scr dispatch skips the task), no
# auto-review, no auto-land for its tasks; `scr dispatch <id>` refuses it unless the actor is omri. The world
# fixture has two workloads: quantum-insertion (first; T-0001) and scriptorium.
from scr.ops import frozen_workloads, is_frozen  # noqa: E402


def _set_frozen(root: Path, name: str, value) -> None:
    """Set (value True/False) or delete (value None) `frozen` on a workload and commit it, like _set_limit."""
    p = root / "config" / "workloads.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    if value is None:
        cfg[name].pop("frozen", None)
    else:
        cfg[name]["frozen"] = value
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    sgit(root, "add", "config/workloads.yaml"); sgit(root, "commit", "-q", "-m", f"[test] workloads: {name}.frozen={value}")


def _ready_tooling_task(root: Path, tid: str = "T-0002") -> None:
    scr(root, "--quiet", "task", "new", f"Tool {tid}", "--id", tid, "--workload", "scriptorium", "--files", "tool.txt", "--origin", "omri")
    for st in ("triaged", "ready"):
        scr(root, "--quiet", "--actor", "s", "task", "set", tid, st)


def test_frozen_workload_task_is_not_auto_dispatched_and_another_is(world, monkeypatch):
    root, _, _ = world
    _set_frozen(root, "scriptorium", True)
    _ready_tooling_task(root)
    _record_launches(monkeypatch)
    calls = _spy_scr(monkeypatch)
    Watcher(root, quiet=True, auto_dispatch=True).tick()
    assert any(a[0] == "dispatch" for _, a in calls)
    assert Store(root).get("T-0001").status == "in_progress"            # quantum-insertion: dispatched and launched
    assert Store(root).get("T-0002").status == "ready"                  # scriptorium, frozen: held
    log = (root / LOG_FILE).read_text(encoding="utf-8")
    assert log.count("frozen: 1 scriptorium tasks held (1 ready)") == 1  # one count line per tick, not one per task
    assert scr(root, "verify", "--quiet").returncode == 0


def test_frozen_workload_gets_no_auto_review_and_no_auto_land(world, monkeypatch):
    root, repo, _ = world
    _set_frozen(root, "quantum-insertion", True)
    _submit(root)
    reviews: list[str] = []
    monkeypatch.setattr(Watcher, "start_review", lambda self, t: reviews.append(t.id))
    Watcher(root, quiet=True, auto_review=True).tick()
    assert reviews == [] and "review:T-0001" not in _flags(root)        # held; the flag waits for the thaw
    assert "frozen: 1 quantum-insertion tasks held (1 submitted)" in (root / LOG_FILE).read_text(encoding="utf-8")

    _write_review(root, "T-0001", accepted_review())
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", "accepted")
    lands: list[str] = []
    (repo / ".worktrees" / "T-0001").mkdir(parents=True, exist_ok=True)          # a landing needs a worktree to land from
    from scr import jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "submit", lambda root, cmd, **kw: lands.append(kw.get("task")) or {"id": "J-9999", "task": kw.get("task"), "name": kw.get("name"), "status": "running"})
    monkeypatch.setattr(jobs_mod, "save", lambda root, job: None)
    Watcher(root, quiet=True, auto_land=True).tick()
    assert lands == [] and "land:T-0001" not in _flags(root) and Store(root).get("T-0001").status == "accepted"

    _set_frozen(root, "quantum-insertion", False)                        # the thaw: the landing job is submitted on the next tick
    Watcher(root, quiet=True, auto_land=True).tick()
    assert lands == ["T-0001"]


def test_frozen_submitted_task_is_reviewed_after_the_thaw(world, monkeypatch):
    root, _, _ = world
    _set_frozen(root, "quantum-insertion", True)
    _submit(root)
    reviews: list[str] = []
    monkeypatch.setattr(Watcher, "start_review", lambda self, t: reviews.append(t.id))
    Watcher(root, quiet=True, auto_review=True).tick()
    assert reviews == []
    _set_frozen(root, "quantum-insertion", None)
    Watcher(root, quiet=True, auto_review=True).tick()
    assert reviews == ["T-0001"]


def test_scr_dispatch_of_a_frozen_task_is_omris_alone(world):
    root, _, _ = world
    _set_frozen(root, "scriptorium", True)
    _ready_tooling_task(root)
    r = scr(root, "--actor", "prioritizer", "dispatch", "T-0002", check=False)
    assert r.returncode == 2 and "T-0002: workload 'scriptorium' is frozen" in r.stderr, r.stderr
    assert "scr --actor omri dispatch T-0002" in r.stderr
    assert Store(root).get("T-0002").status == "ready"
    out = scr(root, "--actor", "prioritizer", "dispatch", "--dry-run").stdout      # a bare dispatch skips it
    assert "frozen: 1 task(s) held (T-0002)" in out and "would dispatch: T-0001 " in out, out
    r = scr(root, "--actor", "omri", "dispatch", "T-0002")
    assert "dispatched T-0002" in r.stdout and Store(root).get("T-0002").status == "dispatched"
    assert Store(root).get("T-0001").status == "ready"                           # only the named task
    assert scr(root, "verify", "--quiet").returncode == 0


def test_frozen_flag_absent_is_false(world):
    root, _, _ = world
    _ready_tooling_task(root)
    outs = []
    for value in (None, False):
        _set_frozen(root, "scriptorium", value)
        assert not is_frozen(Store(root), "scriptorium") and frozen_workloads(Store(root)) == []
        outs.append(scr(root, "dispatch", "--dry-run").stdout)
        scr(root, "--quiet", "refresh")                                           # the snapshot hashes config/
        assert "info" not in scr(root, "verify").stdout
    assert outs[0] == outs[1] and "T-0001" in outs[0] and "T-0002" in outs[0] and "frozen" not in outs[0]
    _set_frozen(root, "scriptorium", "yes")                                        # only a real true freezes
    assert not is_frozen(Store(root), "scriptorium")
    _set_frozen(root, "scriptorium", True)
    assert is_frozen(Store(root), "scriptorium") and not is_frozen(Store(root), "quantum-insertion")
    assert frozen_workloads(Store(root)) == ["scriptorium"]
    scr(root, "--quiet", "refresh")
    v = scr(root, "verify").stdout
    assert v.count("info    workload 'scriptorium' is frozen") == 1, v
    assert "exception: scr --actor omri dispatch <id>" in v


def test_live_config_leaves_every_workload_unfrozen_and_intake_names_the_note():
    """T-0272 builds the switch only (T-0276 throws it); the Intake rule names the parking note verbatim."""
    wls = yaml.safe_load((CONFIG / "workloads.yaml").read_text(encoding="utf-8"))
    assert all(not cfg.get("frozen") for cfg in wls.values())
    assert '"frozen workload: parked for the thaw"' in (REPO / "agents" / "intake.md").read_text(encoding="utf-8")


# ---- T-0274: taking over a review session holds the review; its end relaunches it or applies the Decision -------
# `scr takeover` on a submitted task picks the review's launch (recorded_session), so the `takeover` event carries
# `kind: review`. Before T-0274 the watcher ignored it: review_stale escalated the task under Omri, and a takeover
# ended with no Decision left `review:<id>` set (the dead-review state: a hand edit of .scr/watch.state.txt).
from scr.watch import review_takeover  # noqa: E402


def _takeover_event(root: Path, tid: str = "T-0001", kind: str = "review") -> str:
    sid = str(uuid.uuid4())
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover", "--task", tid,
        "--note", "taken over", "--data", json.dumps({"session_id": sid, "by": "omri", "kind": kind}))
    return sid


def _reviewing(root: Path, monkeypatch) -> list[str]:
    """T-0001 submitted and its review launched (the `review:` flag set, start_review faked); returns the ids
    start_review was called for."""
    _submit(root)
    reviews: list[str] = []
    monkeypatch.setattr(Watcher, "start_review", lambda self, t: reviews.append(t.id))
    Watcher(root, quiet=True, auto_review=True, auto_decide=True).tick()
    assert reviews == ["T-0001"] and "review:T-0001" in _flags(root)
    return reviews


def test_review_takeover_is_the_takeover_of_the_current_submissions_review():
    def ev(ts, **k):
        return {"kind": "system", "task": "T-1", "ts": ts, **k}
    sub = {"kind": "state", "task": "T-1", "from": "in_progress", "to": "submitted", "ts": "2026-09-29T10:00:00Z"}
    rev = ev("2026-09-29T11:00:00Z", name="takeover", data={"session_id": "r", "kind": "review"})
    assert review_takeover([sub, rev], "T-1")["session_id"] == "r"
    assert review_takeover([sub, ev("2026-09-29T11:00:00Z", name="takeover", data={"session_id": "r", "kind": "tooling-review"})], "T-1")
    assert review_takeover([sub, ev("2026-09-29T11:00:00Z", name="takeover", data={"session_id": "s", "kind": "scriber"})], "T-1") is None
    assert review_takeover([sub, ev("2026-09-29T11:00:00Z", name="takeover", data={"session_id": "s"})], "T-1") is None
    assert review_takeover([rev, {**sub, "ts": "2026-09-29T12:00:00Z"}], "T-1") is None      # an earlier submission's review
    assert review_takeover([rev], "T-1") is None                                                 # never submitted
    ended = review_takeover([sub, rev, ev("2026-09-29T12:00:00Z", name="takeover_ended")], "T-1")
    assert ended["ended"] == "2026-09-29T12:00:00Z"


def test_an_open_review_takeover_raises_no_dead_review_or_review_stale(world, monkeypatch):
    root, repo, _ = world
    reviews = _reviewing(root, monkeypatch)
    _recorded(root, repo, pid=777010, kind="review", created=1)       # the review's launch, which takeover picks
    _takeover_event(root)
    _backdate(root, "T-0001", "submitted", 300)                        # 5 h > review_stale_hours 2
    monkeypatch.setattr(watch_mod, "pid_alive", lambda pid: False)     # the stopped review pid is gone
    w = Watcher(root, quiet=True, auto_review=True, auto_decide=True)
    for _ in range(2):
        res = w.tick()
        assert res["ok"] and not res["escalated"], res
    assert Store(root).get("T-0001").status == "submitted" and not _events(root, "slot_freed", "T-0001")
    assert reviews == ["T-0001"] and "review:T-0001" in _flags(root)  # Omri's review: nothing relaunched, flag held
    assert not [n for n in res["needs"] if "T-0001" in n], res["needs"]


def test_a_review_takeover_ended_without_a_decision_relaunches_the_review(world, monkeypatch):
    root, _, _ = world
    reviews = _reviewing(root, monkeypatch)
    _takeover_event(root)
    _backdate(root, "T-0001", "submitted", 300)
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover_ended", "--task", "T-0001",
        "--note", "takeover terminal closed")
    w = Watcher(root, quiet=True, auto_review=True, auto_decide=True)
    res = w.tick()
    # the flag is gone from watch.state (nobody edited it) and the review_stale clock restarted at the end
    assert "review:T-0001" not in _flags(root) and reviews == ["T-0001"]
    assert not res["escalated"] and Store(root).get("T-0001").status == "submitted"
    assert "review takeover ended with no Decision" in (root / LOG).read_text(encoding="utf-8")
    w.tick()                                                           # the next tick launches a fresh review
    assert reviews == ["T-0001", "T-0001"] and "review:T-0001" in _flags(root)
    w.tick()                                                           # once: the flag is not cleared again
    assert reviews == ["T-0001", "T-0001"] and Store(root).get("T-0001").status == "submitted"


def test_a_review_takeover_ended_with_a_decision_applies_it(world, monkeypatch):
    root, _, _ = world
    reviews = _reviewing(root, monkeypatch)
    _takeover_event(root)
    _write_review(root, "T-0001", _review(decision="returned (feedback below)", feedback="Decision: returned (Omri: tests red)"))
    scr(root, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover_ended", "--task", "T-0001",
        "--note", "takeover terminal closed")
    res = Watcher(root, quiet=True, auto_review=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "returned" and res["decided"] and reviews == ["T-0001"]
    assert _state_events(root, "T-0001")[-1]["note"].startswith(DECISION_NOTE)
    # without auto-decide the written Decision is held for Omri as before, and no review is relaunched over it
    root2 = root
    for st in ("ready", "dispatched", "in_progress", "submitted"):
        scr(root2, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    _takeover_event(root2)
    scr(root2, "--quiet", "--actor", "omri", "event", "system", "--name", "takeover_ended", "--task", "T-0001", "--note", "closed")
    Watcher(root2, quiet=True, auto_review=True).tick()
    assert Store(root2).get("T-0001").status == "submitted" and reviews == ["T-0001"]


def test_a_scriber_takeover_does_not_hold_the_review(world, monkeypatch):
    """T-0247's scriber takeover is untouched: its task, once submitted, is reviewed and review_stale escalates it."""
    root, _, _ = world
    _dispatched(root)
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", "in_progress")
    _takeover_event(root, kind="scriber")
    scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", "submitted")
    reviews: list[str] = []
    monkeypatch.setattr(Watcher, "start_review", lambda self, t: reviews.append(t.id))
    _backdate(root, "T-0001", "submitted", 300)
    _exhaust_review_retries(root)
    assert review_takeover(EventLog(root / "events" / "events.jsonl").read(), "T-0001") is None
    res = Watcher(root, quiet=True, auto_review=True, auto_decide=True).tick()
    assert reviews == ["T-0001"] and res["escalated"] and Store(root).get("T-0001").status == "escalated"

def test_intake_brief_is_a_file_on_stdin(world, monkeypatch):
    """T-0330: `claude -p` reads the Intake brief from a file on stdin (as start_claude does), never from a
    communicate() pipe, which the CLI abandons after 3 s and which left every Intake run from 2026-09-30 to 10-09 empty."""
    from scr.launch import intake
    root, _, _ = world
    calls = _record_launches(monkeypatch)
    intake(root)
    assert len(calls) == 1 and calls[0]["stdin_is_file"], calls
    briefs = sorted((root / "reports").glob("intake-*.brief.txt"))
    assert briefs and "You are Intake" in briefs[-1].read_text(encoding="utf-8")


def test_stale_review_is_rerun_twice_before_escalating(world, monkeypatch):
    """2026-10-14: a submitted task whose review ended with no Decision gets its review re-run (review.md archived,
    `review:<id>` cleared, a review_retry event, the stale clock restarted) — twice; the third time it is escalated."""
    root, _, _ = world
    _set_limit(root, "review_stale_hours", 0)
    _submit(root)
    _write_review(root, "T-0001", TEMPLATE)
    d = Store(root).get("T-0001").dir
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"] and Store(root).get("T-0001").status == "submitted" and not res["escalated"], res
    assert not (d / "review.md").exists() and len(list(d.glob("review-stale-1-*.md"))) == 1
    assert len(_events(root, "review_retry", "T-0001")) == 1 and "review:T-0001" not in _flags(root)
    assert not [n for n in res["needs"] if n.startswith("T-0001 ")], res["needs"]
    _write_review(root, "T-0001", TEMPLATE)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "submitted" and len(_events(root, "review_retry", "T-0001")) == 2
    _write_review(root, "T-0001", TEMPLATE)
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "escalated" and len(_events(root, "slot_freed", "T-0001")) == 1
    assert "review_retry:T-0001" not in " ".join(_flags(root))
