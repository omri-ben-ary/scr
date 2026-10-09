"""The hygiene audit pass (T-0202, docs/hygiene-protocols.md §3, §10, §11 F1): `scr sweep --all` classifies every
worktree of every workload into typed rows, commits only dirty missions, and the watcher runs it once a day into
reports/hygiene/<date>.md with one `hygiene_digest` event. Real git repos, plain git."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from scr import chores
from scr.events import EventLog
from scr.ops import HYGIENE_KINDS, spawn, sweep
from scr.store import Store

from test_journal_ops import git, scr, world  # noqa: F401  (the shared fixture: scriptorium + workload repo + origin)


def _mission(repo: Path, name: str, branch: str) -> Path:
    git(repo, "worktree", "add", "-q", "-b", branch, str(repo / ".worktrees" / name), "master")
    return repo / ".worktrees" / name


def _garden(root: Path, repo: Path, tmp: Path) -> dict:
    """One worktree of every kind in the workload repo, plus a dirty task worktree in the scriptorium's own
    workload (repo: ., worktrees: ../scriptorium-worktrees), so both workloads have rows."""
    m = _mission(repo, "qi-mission-9", "qi9")                                        # dirty-mission
    (m / "missions").mkdir(); (m / "missions" / "PLAN.md").write_text("plan\n")
    f = _mission(repo, "qi-mission-8", "qi8")                                        # finished: merged and on origin
    (f / "B.md").write_text("b\n"); git(f, "add", "-A"); git(f, "commit", "-q", "-m", "b")
    git(f, "push", "-q", "-u", "origin", "qi8"); git(repo, "merge", "-q", "--ff-only", "qi8"); git(repo, "push", "-q", "origin", "master")
    p = _mission(repo, "qi-mission-7", "qi7")                                        # phantom: moved away (never deleted)
    shutil.move(str(p), str(tmp / "moved-qi-mission-7"))
    lk = _mission(repo, "qi-mission-6", "qi6")                                       # locked
    git(repo, "worktree", "lock", "--reason", "Omri holds it", str(lk))
    (repo / ".worktrees" / "stray").mkdir(); (repo / ".worktrees" / "stray" / "x.txt").write_text("x\n")   # orphan
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched", journal_on=False)
    spawn(root, ["T-0001"], lake=False)                                              # live task worktree, dirty
    t = repo / ".worktrees" / "T-0001"
    (t / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by trivial\n"); (t / "scratch.txt").write_text("s\n")
    st = root.parent / "scriptorium-worktrees" / "T-0002"                             # the scriptorium workload's own task worktree
    git(root, "worktree", "add", "-q", "-b", "task/T-0002", str(st), "main")
    (st / "wip.py").write_text("x = 1\n")
    return {"mission": m, "task": t, "stask": st}


def _kinds(rows: list[dict]) -> dict:
    return {(r["workload"], r["worktree"]): r["kind"] for r in rows}


def test_sweep_all_dry_run_types_every_worktree_of_every_workload(world, tmp_path, monkeypatch):
    root, repo, _ = world
    monkeypatch.setenv("SCR_NO_JOURNAL", "1")
    _garden(root, repo, tmp_path)
    names = list(yaml.safe_load((root / "config" / "workloads.yaml").read_text()))
    assert len(names) == 2
    qi, sc = names[0], [n for n in names if n != names[0]][0]
    rows = sweep(root, dry_run=True, all_workloads=True)
    assert {r["workload"] for r in rows} == set(names)
    assert all({"workload", "worktree", "branch", "kind"} <= set(r) and r["kind"] in HYGIENE_KINDS for r in rows)
    assert _kinds(rows) == {
        (qi, "qi-mission-9"): "dirty-mission", (qi, "qi-mission-8"): "finished", (qi, "qi-mission-7"): "phantom",
        (qi, "qi-mission-6"): "locked", (qi, "stray"): "orphan", (qi, "T-0001"): "live", (sc, "T-0002"): "live"}
    by = {r["worktree"]: r for r in rows}
    assert by["qi-mission-9"]["branch"] == "qi9" and by["qi-mission-9"]["files"] == 1
    assert "Omri holds it" in by["qi-mission-6"]["note"]
    # --json writes the same rows
    out = scr(root, "sweep", "--dry-run", "--all", "--json", journal_on=False).stdout
    assert _kinds(json.loads(out)) == _kinds(rows)
    # without --all the old behaviour stands: the first workload's dirty missions only
    assert [r["worktree"] for r in sweep(root, dry_run=True)] == ["qi-mission-9"]


def test_task_worktrees_are_rows_but_never_swept(world, tmp_path, monkeypatch):
    root, repo, _ = world
    monkeypatch.setenv("SCR_NO_JOURNAL", "1")
    g = _garden(root, repo, tmp_path)
    heads = {k: git(g[k], "rev-parse", "HEAD") for k in ("task", "stask")}
    rows = sweep(root, all_workloads=True)
    by = {r["worktree"]: r for r in rows}
    assert by["qi-mission-9"]["sha"] and by["qi-mission-9"]["ok"]                     # the mission WAS swept
    assert "[sweep]" in git(g["mission"], "log", "-1", "--format=%s")
    for k, name in (("task", "T-0001"), ("stask", "T-0002")):
        assert name in by and by[name]["sha"] is None
        assert git(g[k], "rev-parse", "HEAD") == heads[k]                           # no commit at all
        assert "[sweep]" not in git(g[k], "log", "--format=%s")                        # nor on the task branch
        assert git(g[k], "status", "--porcelain")                                   # still dirty, untouched
    assert [e["note"].split()[0] for e in EventLog(root / "events" / "events.jsonl").read() if e.get("name") == "sweep"] == ["qi-mission-9"]


def test_no_dirty_worktrees_iff_no_dirty_mission_row(world, tmp_path, monkeypatch):
    root, repo, _ = world
    monkeypatch.setenv("SCR_NO_JOURNAL", "1")

    def agree() -> bool:
        done = chores.check(root, {"no_dirty_worktrees": True})
        assert done == (not any(r["kind"] == "dirty-mission" for r in sweep(root, dry_run=True, all_workloads=True)))
        return done

    assert agree()                                                                   # nothing yet
    scr(root, "--actor", "s", "task", "set", "T-0001", "dispatched", journal_on=False)
    spawn(root, ["T-0001"], lake=False)
    (repo / ".worktrees" / "T-0001" / "scratch.txt").write_text("s\n")
    assert agree()                                                                   # a dirty TASK worktree is not a dirty mission
    m = _mission(repo, "qi-mission-9", "qi9"); (m / "PLAN.md").write_text("p\n")
    assert not agree()
    sweep(root)
    assert agree()


def _git_state(paths: list[Path]) -> str:
    """Every byte of each repo's common git dir (refs, packed-refs, objects, worktree metadata, locks) except the
    index files, whose stat cache any `git status` may refresh, plus each worktree's staged tree, status and
    HEAD, and the listing of every worktree root."""
    h = hashlib.sha256()
    for repo in paths:
        gd = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        for f in sorted(p for p in gd.rglob("*") if p.is_file() and p.name != "index"):
            h.update(str(f.relative_to(gd)).encode()); h.update(f.read_bytes())
        for block in git(repo, "worktree", "list", "--porcelain").split("\n\n"):
            wt = Path(block.splitlines()[0].split(" ", 1)[1])
            h.update(block.encode())
            if wt.exists() and "\nbare" not in block:
                for args in (("ls-files", "--stage"), ("status", "--porcelain", "--untracked-files=all"), ("rev-parse", "HEAD")):
                    h.update(git(wt, *args).encode())
    for d in paths:
        for wr in (d / ".worktrees", d.parent / "scriptorium-worktrees", d.parent / f"{d.name}-RETIRED"):
            if wr.exists():
                h.update("\n".join(sorted(str(p.relative_to(wr)) for p in wr.rglob("*") if ".git" not in p.parts)).encode())
    return h.hexdigest()


def test_dry_run_leaves_git_state_byte_identical(world, tmp_path, monkeypatch):
    root, repo, origin = world
    monkeypatch.setenv("SCR_NO_JOURNAL", "1")
    _garden(root, repo, tmp_path)
    repos = [repo, root, origin]
    before = _git_state(repos)
    ev = len(EventLog(root / "events" / "events.jsonl").read())
    sweep(root, dry_run=True, all_workloads=True)
    scr(root, "sweep", "--dry-run", "--all", journal_on=False)
    scr(root, "sweep", "--dry-run", "--all", "--merge", "--json", journal_on=False)
    assert _git_state(repos) == before
    assert len(EventLog(root / "events" / "events.jsonl").read()) == ev              # no sweep event either
    sweep(root, all_workloads=True)                                                   # the probe sees a real sweep
    assert _git_state(repos) != before


def test_watcher_daily_pass_once_per_date(world, tmp_path, monkeypatch):
    from scr.watch import STATE_FILE, Watcher
    root, repo, _ = world
    monkeypatch.setenv("SCR_NO_JOURNAL", "1")
    _garden(root, repo, tmp_path)
    tasks_before = set(Store(root).load_all())
    day = f"{datetime.now():%Y-%m-%d}"
    digest = root / "reports" / "hygiene" / f"{day}.md"

    def digests() -> list[dict]:
        return [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "system" and e.get("name") == "hygiene_digest"]

    res = Watcher(root, quiet=True).tick()
    assert digest.exists()
    text = digest.read_text(encoding="utf-8")
    names = list(yaml.safe_load((root / "config" / "workloads.yaml").read_text()))
    assert all(f"## {n}" in text for n in names)
    for w in ("qi-mission-9", "qi-mission-8", "qi-mission-7", "qi-mission-6", "stray"):
        assert f"| {w} |" in text
    ev = digests()
    assert len(ev) == 1 and ev[0]["note"].startswith(f"reports/hygiene/{day}.md")
    assert f"hygiene:{day}" in (root / STATE_FILE).read_text(encoding="utf-8").split()
    # informational: no needs line, no chore, no task
    assert not any("hygiene" in n.lower() for n in res["needs"])
    assert set(Store(root).load_all()) == tasks_before
    assert git(repo, "rev-parse", "qi9") == git(repo, "rev-parse", "master~1")        # the dirty mission was not committed
    stamp = digest.stat().st_mtime_ns
    Watcher(root, quiet=True).tick()                                                  # same day: nothing
    assert len(digests()) == 1 and digest.stat().st_mtime_ns == stamp and digest.read_text(encoding="utf-8") == text
