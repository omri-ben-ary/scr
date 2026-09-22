"""Workload-repo operations (quantum-insertion): worktrees, checkpoint commits, landing, sweeping,
retiring. Python ports of the former hooks/*.ps1 and retire_worktree.sh so they run and are
tested on any OS. Invariants: nothing is deleted (retire = quarantine by move), never force-push,
never touch the trusted branch.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .events import EventLog, now_iso
from .gitops import Git, GitError, NOISE, gh_available, pr_create, pr_merge, pr_url_for
from .journal import _Lock as _JournalLock, journal
from .store import RepoError, Store, Task


@dataclass
class Workload:
    name: str
    cfg: dict
    repo: Path
    main: str
    trusted: str | None
    wt_root: Path


def workload(root: Path, store: Store | None = None, name: str | None = None) -> Workload:
    store = store or Store(root)
    wls = store.config("workloads") or {}
    if not wls:
        raise RepoError("config/workloads.yaml is empty")
    if not name or name not in wls:     # a record may name a workload before it is registered: default to the first entry
        name = next(iter(wls))
    cfg = wls[name]
    repo = (root / cfg.get("repo", f"../{name}")).resolve()
    wt = (root / cfg["worktrees"]).resolve() if cfg.get("worktrees") else repo / ".worktrees"
    return Workload(name, cfg, repo, cfg.get("main_branch", "master"), cfg.get("trusted_branch"), wt)


def _log(root: Path) -> EventLog:
    return EventLog(root / "events" / "events.jsonl")


def workload_for(root: Path, store: Store | None, t: Task) -> Workload:
    """The workload a task record names (`workload:` in task.yaml), falling back to the first entry.
    Every service that touches a task's worktree, branch or repo must resolve THIS, not the default
    (T-0036: until 2026-09-16 the watcher, spawn, launch and discuss all took the first entry)."""
    return workload(root, store, (t.rec or {}).get("workload"))


def self_workload(root: Path, store: Store | None = None) -> Workload | None:
    """The workload whose repo IS the scriptorium checkout (`repo: .` — registered as `scriptorium` by
    T-0043), or None. It is special in three ways (T-0061): its worktrees are spawned from the checkout's
    HEAD, not origin/<main> (the journal commits locally first and origin follows); `venv: link` gives
    each worktree the checkout's .venv; `push: true` makes the watcher push every journal commit."""
    store = store or Store(root)
    for name in (store.config("workloads") or {}):
        wl = workload(root, store, name)
        if wl.repo == root.resolve():
            return wl
    return None


def task_worktree(wl: Workload, t: Task) -> Path:
    return wl.wt_root / t.id


def in_scope(path: str, scope: list[str]) -> bool:
    for s in scope:
        s2 = s.rstrip("/")
        if path == s2 or path.startswith(s2 + "/"):
            return True
    return False


# ---- spawn ---------------------------------------------------------------------------
def _share_lake(repo: Path, wt: Path, out: list[str]) -> None:
    """.lake/packages -> junction/symlink to the main checkout; .lake/build copied (per-worktree oleans)."""
    src_pk, dst_pk = repo / ".lake" / "packages", wt / ".lake" / "packages"
    src_b, dst_b = repo / ".lake" / "build", wt / ".lake" / "build"
    (wt / ".lake").mkdir(exist_ok=True)
    if src_pk.exists() and not dst_pk.exists():
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst_pk), str(src_pk)], capture_output=True, text=True)
            out.append("  junction .lake/packages -> main" if r.returncode == 0 else f"  junction failed: {r.stderr.strip()}")
        else:
            os.symlink(src_pk, dst_pk, target_is_directory=True)
            out.append("  symlink .lake/packages -> main")
    if src_b.exists() and not dst_b.exists():
        out.append("  copying .lake/build (library oleans; once per worktree) ...")
        if os.name == "nt" and shutil.which("robocopy"):
            r = subprocess.run(["robocopy", str(src_b), str(dst_b), "/E", "/MT:16", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"], capture_output=True, text=True)
            if r.returncode >= 8:
                out.append(f"  robocopy failed ({r.returncode})")
        else:
            shutil.copytree(src_b, dst_b, dirs_exist_ok=True)


def _link_venv(repo: Path, wt: Path, out: list[str]) -> None:
    """`venv: link` — <worktree>/.venv is a junction (Windows) / symlink to the checkout's .venv: the same
    interpreter, the same pytest and PyYAML, no install, no disk. `python -m pytest tests` run in the
    worktree is honest: the cwd precedes the editable-install finder on sys.path, so `scr` is imported
    from the WORKTREE (tests/test_scriptorium.py::test_scr_under_test_is_this_checkout guards it). A bare
    `pytest.exe` would import the checkout's scr — the workload's `tests:` command must keep `python -m`."""
    src, dst = repo / ".venv", wt / ".venv"
    if dst.exists() or dst.is_symlink():
        return
    if not src.is_dir():
        out.append(f"  no venv to link: {src} is missing (uv sync in the checkout first)")
        return
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], capture_output=True, text=True)
        out.append("  junction .venv -> checkout .venv" if r.returncode == 0 else f"  venv junction failed: {(r.stdout + r.stderr).strip()[-200:]}")
    else:
        os.symlink(src, dst, target_is_directory=True)
        out.append("  symlink .venv -> checkout .venv")


def spawn(root: Path, tids: list[str] | None = None, lake: bool | None = None, wl_name: str | None = None) -> list[str]:
    """One worktree per dispatched/running task (or the ids given), branch task/<id> from origin/<main> —
    except for the scriptorium's own workload (`repo: .`), whose branch starts at the checkout's HEAD: the
    journal commits there first and origin later, so HEAD is the only base that holds every record and the
    current code (T-0061: on 2026-09-19 origin/main sat 1075 commits behind the checkout, and a scriber
    spawned from it saw neither its own task record nor scr/ops.py). With `venv: link` the worktree also
    gets the checkout's .venv (see _link_venv)."""
    store = Store(root)
    tasks = store.load_all()
    if not tids:
        tids = [t.id for t in tasks.values() if t.status in {"dispatched", "in_progress", "checkpoint"}]
    out: list[str] = []
    if not tids:
        return ["nothing dispatched"]
    prepared: dict[str, tuple[Git, str]] = {}      # per workload: its repo's Git and the spawn base
    for tid in tids:
        t = tasks.get(tid)
        if not t:
            out.append(f"{tid}: no record"); continue
        wl = workload(root, store, wl_name) if wl_name else workload_for(root, store, t)
        if wl.name not in prepared:
            wl.wt_root.mkdir(parents=True, exist_ok=True)
            g0 = Git(wl.repo)
            if wl.repo == root.resolve():
                prepared[wl.name] = (g0, "HEAD")          # the checkout itself: see the docstring
            else:
                g0.fetch()
                prepared[wl.name] = (g0, f"origin/{wl.main}" if g0.ok("rev-parse", "--verify", "--quiet", f"origin/{wl.main}") else wl.main)
        g, base = prepared[wl.name]
        wt = task_worktree(wl, t)
        br = f"task/{tid}"
        out.append(f"=== {tid} -> {wt} ({br})")
        if not wt.exists():
            r = g.worktree_add(wt, br, base)
            if not r.ok:
                out.append(f"  worktree add failed: {r.out}"); continue
            label = f"checkout HEAD ({g.branch()})" if base == "HEAD" else base
            out.append(f"  based on {label} @ {g.run('rev-parse', '--short', base).out}")
        else:
            out.append("  exists, reusing")
        if wl.cfg.get("venv") == "link":
            _link_venv(wl.repo, wt, out)
        want_lake = lake if lake is not None else (".lean" in (t.dir / "task.yaml").read_text(encoding="utf-8"))
        if want_lake:
            _share_lake(wl.repo, wt, out)
    return out


# ---- checkpoint commit -------------------------------------------------------------------
def checkpoint_commit(root: Path, tid: str, push: bool = False, wl_name: str | None = None) -> str | None:
    """Commit whatever a running task's worktree holds, on its task branch. Nothing may exist only
    in a worktree: a crashed session or a crashed git (QI23, 2026-09-07) must cost at most one tick."""
    store = Store(root)
    t = store.get(tid)
    wl = workload(root, store, wl_name) if wl_name else workload_for(root, store, t)
    wt = task_worktree(wl, t)
    if not wt.exists():
        return None
    g = Git(wt)
    if g.branch() != f"task/{tid}":
        _log(root).append("system", name="checkpoint_skipped", task=tid, actor=f"steward:{tid}", note=f"worktree on {g.branch()}, expected task/{tid}")
        return None
    if not g.dirty():
        return None
    sha = g.commit_all(f"[checkpoint] {tid} {now_iso()}\n\nAutomatic checkpoint by the Steward watcher; content unreviewed.")
    if sha:
        _log(root).append("system", name="checkpoint_commit", task=tid, actor=f"steward:{tid}", note=sha)
        if push:
            g.push(f"task/{tid}")
    return sha


# ---- land --------------------------------------------------------------------------------
def _section(text: str, name: str) -> str:
    m = re.search(rf"(?ms)^## {re.escape(name)}\s*\n(.*?)(?=^## |\Z)", text)
    return m.group(1).strip() if m else ""


def land(root: Path, tid: str, no_pr: bool = False, draft: bool = False, dry_run: bool = False, merge: bool = False, wl_name: str | None = None) -> dict:
    """Commit the task's worktree, push its branch, open the PR (and with merge=True merge it and
    mark the task merged after verifying origin/<main> contains the branch)."""
    store = Store(root)
    t = store.get(tid)
    wl = workload(root, store, wl_name) if wl_name else workload_for(root, store, t)
    log = _log(root)
    out: list[str] = []
    wt = task_worktree(wl, t)
    if not wt.exists():
        raise RepoError(f"worktree missing: {wt}")
    g = Git(wt)
    br = g.branch()
    if br != f"task/{tid}":
        raise RepoError(f"worktree is on '{br}', expected task/{tid} — not landing from the wrong branch")
    title = t.rec.get("title", tid)
    scope = (t.rec.get("scope") or {}).get("files") or []
    out.append(f"=== {tid} [{t.status}] on {br}")

    report = (t.dir / "report.md").read_text(encoding="utf-8") if (t.dir / "report.md").exists() else ""
    result = _section(report, "Result")
    summary = "\n".join([l for l in result.splitlines() if l.strip() and not l.startswith("|") and not l.startswith("#")][:6])
    msg = f"[{tid}] {title}\n\n{summary}\n\nScriptorium task {tid}; report: scriptorium/tasks/{t.dir.name}/report.md"
    dirty = g.dirty()
    if dirty:
        outside = [p for p in dirty if scope and not in_scope(p, scope)]
        out.append(f"  staging {len(dirty)} file(s)" + (f"; outside scope.files: {', '.join(outside)}" if outside else ""))
        if dry_run:
            out.append("--- commit message ---\n" + msg)
            return {"ok": True, "url": None, "out": out}
        sha = g.commit_all(msg)
        out.append(f"  committed {sha}")
    else:
        out.append("  nothing to commit (already committed)")
    if dry_run:
        return {"ok": True, "url": None, "out": out}

    r = g.push(br)
    if not r.ok:
        out.append(f"  push failed: {r.out[-400:]}")
        return {"ok": False, "url": None, "out": out}
    out.append("  pushed")

    url = None
    if not no_pr:
        if not gh_available():
            out.append(f"  gh not on PATH — open the PR by hand: {br} -> {wl.main}")
        else:
            url = pr_url_for(br, wt)
            if url:
                out.append(f"  PR exists: {url}")
            else:
                body = f"Scriptorium task **{tid}** — {title}\n\n"
                if result:
                    body += "## Result (from report.md)\n\n" + result + "\n\n"
                sc = _section(report, "Statement changes")
                if sc:
                    body += "## Statement changes\n\n" + sc + "\n\n"
                body += f"Full record: `scriptorium/tasks/{t.dir.name}/` (spec, log, report, review).\n"
                bf = t.dir / "pr-body.md"
                bf.write_text(body, encoding="utf-8")
                url = pr_create(wl.main, br, f"[{tid}] {title}", bf, wt, draft=draft)
                out.append(f"  PR: {url}" if url else "  PR creation failed (see gh output)")
    if t.status == "submitted":
        from . import lifecycle
        lifecycle.apply(t.rec, "accepted"); t.save()
        log.append("state", task=tid, **{"from": "submitted"}, to="accepted", actor=f"steward:{tid}", note=f"landed on {br}" + (f"; PR {url}" if url else ""))
    if url:
        log.append("system", name="pr", task=tid, actor=f"steward:{tid}", note=url)

    if merge and url:
        m = pr_merge(url, wt)
        if not m.ok:
            out.append(f"  merge failed: {m.out[-300:]}")
            return {"ok": False, "url": url, "out": out}
        mg = Git(wl.repo)
        mg.fetch()
        if not mg.remote_contains(f"origin/{wl.main}", br):
            out.append(f"  WARNING: origin/{wl.main} does not contain {br} after merge — not marking merged")
            return {"ok": False, "url": url, "out": out}
        out.append(f"  merged -> {wl.main}; {tid} marked merged")
        out += _record_merged(root, store, tid, f"auto-land: {url}", report)
    return {"ok": True, "url": url, "out": out}


def _record_merged(root: Path, store: Store, tid: str, note: str, report: str = "") -> list[str]:
    """The record side of a landing, shared by the quantum-insertion `land` and `land_scriptorium`:
    status -> merged (tokens from the report's "N tokens" line added to tokens_used), the state event,
    and the blocker cascade. Returns the lines to print."""
    from . import lifecycle
    log = _log(root)
    tokens = 0
    mt = re.search(r"(?i)(\d[\d,.]*)\s*(k|m)?\s*tokens", report)
    if mt:
        n = float(mt.group(1).replace(",", "")); n *= {"k": 1e3, "m": 1e6}.get((mt.group(2) or "").lower(), 1); tokens = int(n)
    t = store.get(tid)
    old = t.status
    lifecycle.apply(t.rec, "merged"); t.rec["tokens_used"] = int(t.rec.get("tokens_used", 0)) + tokens; t.save()
    log.append("state", task=tid, **{"from": old}, to="merged", actor=f"steward:{tid}", tokens=tokens or None, note=note)
    return [f"  unblocked {d} -> ready" for d in lifecycle.cascade(store, log, tid, "merged", actor=f"steward:{tid}")]


def _run_tests(wl: Workload, cwd: Path, timeout: int = 1800) -> tuple[int, str]:
    """Run the workload's `tests:` command IN `cwd` (a worktree). Same resolution as scr.hotfix.run_tests,
    which is bound to the checkout: a relative interpreter path is taken relative to cwd (a spawned
    scriptorium worktree links `.venv` to the checkout's), a missing python.exe falls back to this one."""
    cmd = wl.cfg.get("tests")
    if not cmd:
        cmd = ".venv/Scripts/python.exe -m pytest tests -q" if os.name == "nt" else "python -m pytest tests -q"
    argv = shlex.split(cmd, posix=(os.name != "nt"))
    if os.name == "nt":
        argv = [a.strip('"') for a in argv]
    exe = Path(argv[0])
    if not exe.is_absolute() and len(exe.parts) > 1:      # a path relative to the worktree (.venv/Scripts/python.exe), not a PATH lookup
        if (cwd / exe).exists():
            argv[0] = str(cwd / exe)
        elif exe.name.lower().startswith("python"):
            argv[0] = sys.executable
    env = {**os.environ, "SCR_NO_JOURNAL": "1"}
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout + r.stderr)[-6000:]
    except subprocess.TimeoutExpired:
        return 124, f"tests timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, f"test command not found: {e}"


def land_scriptorium(root: Path, tid: str) -> dict:
    """Land an accepted task of the scriptorium's OWN workload that was spawned into a worktree (T-0078).
    Until 2026-09-21 the watcher marked every accepted scriptorium task merged with the note "change
    already on main (journal)" — true for a hotfix made in the checkout, false for a spawned worktree:
    T-0063 and T-0071 (2026-09-17), then T-0058/T-0059/T-0067/T-0076/T-0077, finished as uncommitted
    changes on a branch nothing ever merged, and every review had to escalate for a hand merge.

    The trunk is the workload's `main_branch` (config/workloads.yaml), and the checkout must be ON it —
    the same rule as `scr ensure-main` (T-0061), which folds any side branch back into it; a checkout
    elsewhere is a landing failure, not a guess. Steps, all of them before `merged` is recorded:
      1. commit whatever the worktree still holds on task/<id> (a checkpoint the tooling path skipped);
      2. nothing to do if task/<id> is already contained in the trunk (landed by hand);
      3. merge the TRUNK INTO task/<id>, in the worktree: a conflict surfaces there and is aborted there,
         the checkout never sees it;
      4. run the workload's `tests:` command in the worktree — on exactly the tree the trunk will get;
      5. journal the checkout, then under the journal lock merge task/<id> into the trunk: a fast-forward
         after step 3 unless the trunk moved meanwhile (journal commits: records, never the tooling under
         test), a merge commit then; a failure here is aborted and reported like the others;
      6. status merged, the state event and the task's log.md name the trunk commit.
    Testing on the task branch rather than on the trunk means a failing test never leaves the checkout —
    the tree every scr and the watcher itself run from — on a broken commit. Nothing is ever reset or
    deleted; the trunk moves forward or not at all.
    Returns {"ok", "reason" (conflict | tests | trunk | ...), "out" (lines), "sha" (trunk head on success),
    "tip" (the task branch head after the attempt; the watcher keys its retry on it)}."""
    store = Store(root)
    t = store.get(tid)
    wl = workload_for(root, store, t)
    if wl.repo != root.resolve():
        raise RepoError(f"{tid} is a {wl.name} task, not a scriptorium one — use land()")
    wt = task_worktree(wl, t)
    if not wt.exists():
        raise RepoError(f"worktree missing: {wt}")
    br, trunk = f"task/{tid}", wl.main
    g, mg = Git(wt), Git(root)
    out: list[str] = [f"=== {tid} [{t.status}] {br} -> {trunk} (scriptorium)"]
    if g.branch() != br:
        raise RepoError(f"worktree is on '{g.branch()}', expected {br} — not landing from the wrong branch")
    title = t.rec.get("title", tid)
    report = (t.dir / "report.md").read_text(encoding="utf-8") if (t.dir / "report.md").exists() else ""

    def fail(reason: str, line: str) -> dict:
        out.append(f"  FAILED ({reason}): {line}")
        return {"ok": False, "reason": reason, "out": out, "sha": None, "tip": g.head()}

    # 1. the worktree's uncommitted work goes on its branch first (nothing may exist only in a worktree)
    dirty = g.dirty()
    if dirty:
        summary = "\n".join([l for l in _section(report, "Result").splitlines() if l.strip() and not l.startswith(("|", "#"))][:6])
        sha = g.commit_all(f"[{tid}] {title}\n\n{summary}\n\nScriptorium task {tid}; report: tasks/{t.dir.name}/report.md")
        out.append(f"  committed {sha} ({len(dirty)} file(s)) on {br}")
    # 2. the checkout must sit on the trunk (scr ensure-main is the way to get there)
    cur = mg.branch()
    if cur != trunk:
        return fail("trunk", f"the checkout is on '{cur}', not {trunk} — `scr ensure-main` first")
    if mg.is_ancestor(br, trunk):
        out.append(f"  {br} is already contained in {trunk} — nothing to merge")
        out += _record_merged(root, store, tid, f"landed: {br} already contained in {trunk} @ {mg.head(short=True)}", report)
        _log_md(t, f"landed: {br} was already contained in {trunk} @ {mg.head(short=True)}")
        return {"ok": True, "reason": None, "out": out, "sha": mg.head(short=True), "tip": g.head()}
    # 3. trunk -> branch, in the worktree (a conflict is aborted there and never reaches the checkout)
    if not g.is_ancestor(trunk, br):
        m = g.run("merge", "--no-edit", "-m", f"[land] {tid}: merge {trunk} into {br} before landing", trunk)
        if not m.ok:
            conflicts = [l.strip() for l in g.run("diff", "--name-only", "--diff-filter=U").out.splitlines() if l.strip()]
            g.run("merge", "--abort")
            return fail("conflict", f"{trunk} does not merge into {br}: " + (", ".join(conflicts[:12]) if conflicts else m.out[-300:]))
        out.append(f"  merged {trunk} into {br}: {g.head(short=True)}")
    # 4. the tests, on the tree the trunk will get
    rc, txt = _run_tests(wl, wt)
    if rc != 0:
        tail = " | ".join(l.strip() for l in txt.splitlines() if l.strip())[-400:]
        return fail("tests", f"`{wl.cfg.get('tests') or 'pytest tests -q'}` exited {rc} in {wt}: {tail}")
    out.append("  tests pass")
    # 5. branch -> trunk, in the checkout, serialized with the journal
    journal(root, f"steward:{tid}", f"pre-land checkpoint for {tid}")
    with _JournalLock(root):
        if mg.branch() != trunk:
            return fail("trunk", f"the checkout moved to '{mg.branch()}' during the tests")
        before = mg.head()
        m = mg.run("merge", "--no-edit", "-m", f"[land] {tid}: {title}"[:200], br)
        if not m.ok:
            mg.run("merge", "--abort")
            return fail("trunk", f"{br} does not merge into {trunk} (moved during the tests?): {m.out[-300:]}")
        head = mg.head(short=True)
    how = "fast-forward" if mg.run("rev-parse", br).out == mg.run("rev-parse", "HEAD").out else f"merge commit on {before[:8]}"
    out.append(f"  {trunk}: {before[:8]} -> {head} ({how})")
    # 6. the record
    _log(root).append("system", name="landed", task=tid, actor=f"steward:{tid}", note=f"{br} -> {trunk} @ {head} ({how}); tests pass")
    out += _record_merged(root, store, tid, f"landed: {br} -> {trunk} @ {head} ({how}); tests pass", report)
    _log_md(t, f"landed: {br} -> {trunk} @ {head} ({how}); tests pass")
    out.append(f"  {tid} marked merged")
    return {"ok": True, "reason": None, "out": out, "sha": head, "tip": g.head()}


def _log_md(t: Task, line: str) -> None:
    try:
        with (t.dir / "log.md").open("a", encoding="utf-8") as f:
            f.write(f"- {now_iso()} [steward:{t.id}] {line}\n")
    except OSError:
        pass


# ---- sweep -------------------------------------------------------------------------------
def sweep(root: Path, merge: bool = False, dry_run: bool = False, wl_name: str | None = None) -> list[dict]:
    """Every non-task worktree (missions like qi-mission-24) with uncommitted work: commit on its
    branch, push, and with merge=True PR + merge into <main>. Returns one row per worktree."""
    wl = workload(root, None, wl_name)
    rows: list[dict] = []
    if not wl.wt_root.exists():
        return rows
    for d in sorted(p for p in wl.wt_root.iterdir() if p.is_dir() and not re.match(r"^T-\d+$", p.name)):
        g = Git(d)
        if not g.is_repo():
            continue
        dirty = g.dirty()
        if not dirty:
            continue
        br = g.branch()
        row = {"worktree": d.name, "branch": br, "files": len(dirty), "sample": dirty[:25], "sha": None, "url": None, "ok": True, "note": ""}
        rows.append(row)
        if dry_run:
            continue
        try:
            row["sha"] = g.commit_all(f"[sweep] uncommitted work found in .worktrees/{d.name} on {br} ({now_iso()})\n\nSwept by `scr sweep` so it exists in git history; content unreviewed.")
        except GitError as e:
            row["ok"] = False; row["note"] = str(e)[-300:]; continue
        r = g.push(br)
        if not r.ok:
            row["ok"] = False; row["note"] = "push failed: " + r.out[-300:]; continue
        if merge:
            if not gh_available():
                row["note"] = f"gh missing; PR by hand: {br} -> {wl.main}"; continue
            url = pr_url_for(br, d)
            if not url:
                bf = root / ".scr" / f"sweep-{d.name}.md"; bf.parent.mkdir(exist_ok=True)
                bf.write_text(f"Swept by `scr sweep`; see the commit message. Merging keeps the agents' {wl.main} a complete record.\n", encoding="utf-8")
                url = pr_create(wl.main, br, f"[sweep] {d.name}: land uncommitted mission work", bf, d)
            row["url"] = url
            if url:
                m = pr_merge(url, d)
                if not m.ok:
                    row["ok"] = False; row["note"] = "merge failed: " + m.out[-300:]
        _log(root).append("system", name="sweep", actor="steward-watch", note=f"{d.name} ({br}): {len(dirty)} file(s) -> {row['sha']}" + (f" PR {row['url']}" if row["url"] else ""))
    return rows


# ---- retire (quarantine, never delete) ---------------------------------------------------
def retire(root: Path, name: str, execute: bool = False, wl_name: str | None = None) -> dict:
    if wl_name is None:                 # a task's worktree lives under ITS workload's root
        _st = Store(root)
        if name in _st.load_all():
            wl_name = _st.get(name).rec.get("workload")
    wl = workload(root, None, wl_name)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in (".", "..") or ".." in name or name.startswith("-"):
        raise RepoError(f"illegal worktree name: {name}")
    wt = wl.wt_root / name
    if not wt.is_dir() or wt.is_symlink():
        raise RepoError(f"no such worktree (or it is a link): {wt}")
    if wt.resolve().parent != wl.wt_root.resolve():
        raise RepoError(f"refusing: {name} resolves outside {wl.wt_root}")
    g, mg = Git(wt), Git(wl.repo)
    br = g.branch()
    out = [f"=== retire {name} (branch {br}, main {wl.main}) [{'execute' if execute else 'dry-run'}]"]
    if not mg.is_ancestor(br, wl.main):
        ahead = mg.run("rev-list", "--count", f"{wl.main}..{br}").out
        return {"ok": False, "code": 2, "out": out + [f"REFUSING: {br} is {ahead} commit(s) ahead of {wl.main} — land it first"]}
    if g.run("diff", "--stat", "HEAD").out.strip():
        return {"ok": False, "code": 3, "out": out + ["REFUSING: uncommitted changes to tracked files (checkpoint commit first)"]}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    retired = wl.repo.parent / f"{wl.repo.name}-RETIRED"
    dest = retired / f"{stamp}-{name}"
    untracked = [l for l in g.run("ls-files", "--others", "--exclude-standard").out.splitlines() if l.strip()]
    out.append(f"  {len(untracked)} untracked file(s) travel with the tree; quarantine -> {dest / 'worktree'}")
    if not execute:
        return {"ok": True, "code": 0, "out": out + ["DRY RUN — nothing moved"]}
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(wt), str(dest / "worktree"))
    mg.run("worktree", "prune")
    meta = wl.repo / ".git" / "worktrees" / name
    if meta.exists():
        shutil.move(str(meta), str(dest / "git-worktree-meta"))
    files = sum(1 for _ in (dest / "worktree").rglob("*") if _.is_file())
    if files == 0:
        return {"ok": False, "code": 1, "out": out + ["FAIL: quarantine is EMPTY — stop and investigate"]}
    out.append(f"  OK: {files} file(s) readable in quarantine; branch {br} intact at {mg.run('rev-parse', '--short', br).out}")
    _log(root).append("system", name="worktree_retired", actor="steward-watch", note=f"{name} -> {dest}")
    return {"ok": True, "code": 0, "out": out}


# ---- park --------------------------------------------------------------------------------
def park(root: Path, tid: str, note: str, actor: str | None = None) -> list[str]:
    """returned -> escalated -> omri_queue: take a task off the running set and put it in front of Omri."""
    from . import lifecycle
    store = Store(root)
    t = store.get(tid)
    actor = actor or f"steward:{tid}"
    log = _log(root)
    out = []
    for st in ("returned", "escalated", "omri_queue"):
        if t.status == st:
            continue
        if not lifecycle.allowed(t.rec, st):
            out.append(f"{tid}: {t.status} -> {st} not allowed; forcing (logged)")
        old = t.status
        lifecycle.apply(t.rec, st); t.save()
        log.append("state", task=tid, **{"from": old}, to=st, actor=actor, note=note if st == "returned" else None)
        out.append(f"{tid}: {old} -> {st}")
    return out


# ---- chore helpers -------------------------------------------------------------------------
def land_all_submitted(root: Path) -> list[str]:
    """Land + merge every submitted/accepted task, one after the other; failures are reported, not fatal."""
    out: list[str] = []
    for t in sorted(Store(root).load_all().values(), key=lambda t: t.id):
        if t.status not in {"submitted", "accepted"}:
            continue
        try:
            r = land(root, t.id, merge=True)
            out += r["out"]
            if not r["ok"]:
                out.append(f"{t.id}: NOT landed")
        except Exception as e:  # noqa: BLE001
            out.append(f"{t.id}: failed: {e}")
    return out or ["nothing submitted or accepted"]

