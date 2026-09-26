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
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .events import EventLog, now_iso
from .gitops import Git, GitError, NOISE, gh_available, pr_create, pr_merge, pr_url_for
from .journal import LOCK_STALE_S, SCR_DIR, _Lock as _JournalLock, journal
from .review import acceptance_problem
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


# ---- the scope rule (T-0079) ---------------------------------------------------------------
# Run-log paths every task may write: the watcher excluded exactly these two from drift since 2026-09-16.
LOG_PATHS = (".loopy/", "missions/LOOPY/runs/")
MISSIONS_DIR = "missions"


def scope_prefixes(t: Task) -> list[str]:
    """Every prefix a task may write under: `scope.files`, `missions/<mission>/` for a mission the RECORD
    names (`scope.mission`, a name or a list; `missions/QI26/` accepted), and LOG_PATHS — but LOG_PATHS only
    when the task declares some scope at all (a record without one cannot drift and needs no allowlist)."""
    sc = t.rec.get("scope") or {}
    out = [str(f).replace("\\", "/").strip().rstrip("/") for f in (sc.get("files") or []) if str(f).strip()]
    val = sc.get("mission")
    for m in (val if isinstance(val, (list, tuple)) else ([val] if val else [])):
        m = str(m).replace("\\", "/").strip().strip("/")
        if m.startswith(MISSIONS_DIR + "/"):
            m = m[len(MISSIONS_DIR) + 1:]
        m = m.split("/", 1)[0]
        if m and f"{MISSIONS_DIR}/{m}" not in out:
            out.append(f"{MISSIONS_DIR}/{m}")
    if out:
        out += [lp.rstrip("/") for lp in LOG_PATHS if lp.rstrip("/") not in out]
    return out


def git_ignored(repo: Path | None, paths: list[str]) -> set[str]:
    """The subset of `paths` the repo's git ignores (`git check-ignore --stdin -z`, one call). A tracked file
    is never reported, so a real change to a tracked file under an ignore pattern still counts. Empty when
    there is no repo or git fails."""
    paths = [p for p in paths if p]
    if not repo or not paths or not Path(repo).exists():
        return set()
    try:
        r = subprocess.run(["git", "check-ignore", "--stdin", "-z"], cwd=str(repo), input="\0".join(paths) + "\0",
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return set()
    if r.returncode not in (0, 1):     # 0 = some ignored, 1 = none; anything else is an error
        return set()
    return {l.replace("\\", "/") for l in r.stdout.split("\0") if l.strip()}


def scope_drift(paths: list[str], t: Task, repo: Path | None = None) -> list[str]:
    """The paths a task may NOT touch, in input order, deduplicated: not under `scope.files`, not under its
    own mission folder, not a run-log path, and not ignored by the workload's git. A task that declares no
    scope never drifts (the watcher's old `scope and not in_scope(...)` guard, kept). ONE rule for the
    checkpoint, the watcher's drift block and both `land`s, so they cannot disagree about a file.

    T-0082 makes `scr/scope.py` THE allowlist (it also infers the mission from the spec's Scope and knows the
    transient handshake files); its `drift(paths, task, repo=)` has this signature on purpose. While that
    module is not on the trunk the rule above stands in; the day it lands, every consumer here switches to
    it at once and this fallback is dead code to delete."""
    try:
        from . import scope as _scope  # type: ignore[attr-defined]  # T-0082's module, when it is on the trunk
    except ImportError:
        _scope = None
    if _scope is not None:
        return list(_scope.drift(paths, t, repo=repo))
    prefixes = scope_prefixes(t)
    if not prefixes:
        return []
    cands: list[str] = []
    for raw in paths:
        p = str(raw).replace("\\", "/").strip().rstrip("/")
        if not p or p in cands or in_scope(p, prefixes):
            continue
        cands.append(p)
    if not cands:
        return []
    ign = git_ignored(repo, cands)
    return [p for p in cands if p not in ign]


def branch_base(root: Path, wl: Workload, g: Git) -> str:
    """The ref a task branch is measured against — what `spawn` based it on: the local trunk for the
    scriptorium's own workload (spawned from the checkout's HEAD, T-0061), `origin/<main>` otherwise
    (never the local main, which can sit far behind origin: 465 false drift files on T-0029, 2026-09-16)."""
    if wl.repo == root.resolve():
        return wl.main
    return f"origin/{wl.main}" if g.ok("rev-parse", "--verify", "--quiet", f"origin/{wl.main}") else wl.main


def branch_changes(g: Git, base: str) -> list[str]:
    """Paths the branch's own commits touch: `git diff --name-only <base>...HEAD` (merge-base), NUL-separated
    so a quoted non-ASCII name comes back verbatim."""
    r = g.run("diff", "--name-only", "-z", f"{base}...HEAD")
    return [l.replace("\\", "/") for l in r.out.split("\0") if l.strip()] if r.ok else []


def branch_drift(root: Path, wl: Workload, g: Git, t: Task) -> list[str]:
    """The out-of-scope paths the task branch's commits carry (a `[checkpoint-drift]` commit, or anything
    else that got there). `land` refuses to merge while this is non-empty."""
    return scope_drift(branch_changes(g, branch_base(root, wl, g)), t, repo=g.cwd)


@dataclass
class Checkpoint:
    """What one scoped commit pass produced: the `[checkpoint]` commit of the in-scope paths (`sha`), the
    `[checkpoint-drift]` commit of the out-of-scope ones (`drift_sha`) and those paths (`drift`)."""
    sha: str | None
    drift_sha: str | None
    drift: list[str]

    @property
    def head(self) -> str | None:
        """The branch tip after the pass: the drift commit when there was one, else the checkpoint."""
        return self.drift_sha or self.sha


DRIFT_MESSAGE = ("[checkpoint-drift] {tid} {stamp}\n\n{n} path(s) outside the task's scope, found in its worktree and committed apart so "
                 "nothing exists only there; `land` refuses task/{tid} while it carries them — widen scope.files "
                 "(or scope.mission) in the record if the task may own them, else revert this commit.\n\n{files}\n")


def scoped_commit(g: Git, t: Task, message: str, tid: str | None = None) -> Checkpoint:
    """The dirty tree as at most two commits: the in-scope paths with `message` (`[checkpoint]` / the landing
    message), then the out-of-scope paths as a `[checkpoint-drift]` commit of their own. This is the design
    the brief offered for the conflict between the crash-safety rule (nothing may exist only in a worktree)
    and the scope rule (a checkpoint never carries a file the task may not touch): both hold — the drift is
    in git, in a commit of its own that `land` refuses to merge, and never in a commit that carries the
    task's work. Noise (build.log, __pycache__, .lake, .loopy: gitops.NOISE_RE) stays uncommitted as before;
    ignored paths never reach `dirty()`."""
    dirty = g.dirty()
    if not dirty:
        return Checkpoint(None, None, [])
    drift = scope_drift(dirty, t, repo=g.cwd)
    keep = [p for p in dirty if p not in set(drift)]
    sha = g.commit_paths(message, keep) if keep else None
    dsha = None
    if drift:
        dsha = g.commit_paths(DRIFT_MESSAGE.format(tid=tid or t.id, stamp=now_iso(), n=len(drift), files="\n".join(drift)), drift)
    return Checkpoint(sha, dsha, drift if dsha else [])


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
def checkpoint(root: Path, tid: str, push: bool = False, wl_name: str | None = None) -> Checkpoint | None:
    """Commit whatever a running task's worktree holds, on its task branch. Nothing may exist only
    in a worktree: a crashed session or a crashed git (QI23, 2026-09-07) must cost at most one tick.
    Since T-0079 the `[checkpoint]` commit stages only what the task may own (ops.scope_drift: scope.files,
    its mission folder, the run-log paths, ignored paths excluded as before); anything else goes into a
    separate `[checkpoint-drift]` commit on the same branch and is surfaced as ONE `scope_drift` event
    naming the files (the watcher's drift block consumes it and does not raise a second one). T-0021 had
    copied `missions/SCRIPTORIUM/` into its worktree to run the checker, and `git add -A` shipped the four
    files through PR #49. None when there is no worktree, the branch is wrong, or the tree is clean."""
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
    c = scoped_commit(g, t, f"[checkpoint] {tid} {now_iso()}\n\nAutomatic checkpoint by the Steward watcher; content unreviewed.", tid)
    log = _log(root)
    if c.sha:
        log.append("system", name="checkpoint_commit", task=tid, actor=f"steward:{tid}", note=c.sha)
    if c.drift_sha:
        note = ", ".join(c.drift[:12]) + (f" (+{len(c.drift) - 12} more)" if len(c.drift) > 12 else "")
        log.append("system", name="scope_drift", task=tid, actor=f"steward:{tid}",
                   note=(note + f" — outside scope, committed apart as [checkpoint-drift] {c.drift_sha}; land refuses task/{tid} while it carries them")[:900],
                   data={"commit": c.drift_sha, "files": c.drift[:50]})
    if c.head and push:
        g.push(f"task/{tid}")
    return c


def checkpoint_commit(root: Path, tid: str, push: bool = False, wl_name: str | None = None) -> str | None:
    """`checkpoint`, returning only the branch tip it produced (the sha of the last commit made) or None."""
    c = checkpoint(root, tid, push=push, wl_name=wl_name)
    return c.head if c else None


# ---- land --------------------------------------------------------------------------------
def _section(text: str, name: str) -> str:
    m = re.search(rf"(?ms)^## {re.escape(name)}\s*\n(.*?)(?=^## |\Z)", text)
    return m.group(1).strip() if m else ""


def land(root: Path, tid: str, no_pr: bool = False, draft: bool = False, dry_run: bool = False, merge: bool = False, wl_name: str | None = None) -> dict:
    """Commit the task's worktree, push its branch, open the PR (and with merge=True merge it and
    mark the task merged after verifying origin/<main> contains the branch). A `submitted` task is
    accepted here only when its review.md decides `accepted` with at least one mechanical check row
    (scr/review.py, T-0081): otherwise RepoError, no commit, no push, no PR, status untouched. T-0021
    was accepted and merged (PR #49) on the untouched review template before this guard existed."""
    store = Store(root)
    t = store.get(tid)
    if t.status == "submitted":                  # T-0081: no acceptance without a written review
        problem = acceptance_problem(t.dir)
        if problem:
            raise RepoError(f"{tid}: not landing a submitted task without a written review — {problem}; "
                            f"fill tasks/{t.dir.name}/review.md first")
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
    out.append(f"=== {tid} [{t.status}] on {br}")

    report = (t.dir / "report.md").read_text(encoding="utf-8") if (t.dir / "report.md").exists() else ""
    result = _section(report, "Result")
    summary = "\n".join([l for l in result.splitlines() if l.strip() and not l.startswith("|") and not l.startswith("#")][:6])
    msg = f"[{tid}] {title}\n\n{summary}\n\nScriptorium task {tid}; report: scriptorium/tasks/{t.dir.name}/report.md"
    dirty = g.dirty()
    if dirty:
        outside = scope_drift(dirty, t, repo=wt)
        out.append(f"  staging {len(dirty)} file(s)" + (f"; outside scope: {', '.join(outside)} (a [checkpoint-drift] commit of their own)" if outside else ""))
        if dry_run:
            out.append("--- commit message ---\n" + msg)
        else:
            c = scoped_commit(g, t, msg, tid)
            if c.sha:
                out.append(f"  committed {c.sha}")
            if c.drift_sha:
                out.append(f"  committed {c.drift_sha} [checkpoint-drift] ({len(c.drift)} out-of-scope path(s))")
    else:
        out.append("  nothing to commit (already committed)")
    # T-0079: a branch whose commits touch paths outside the task's scope is not pushed, gets no PR and is not
    # merged — T-0021's four missions/SCRIPTORIUM/ files would have stopped here, named. The Steward widens
    # scope.files / scope.mission in the record when the task may own them, or reverts the drift commit.
    drift = branch_drift(root, wl, g, t)
    if dry_run and dirty:
        drift = list(dict.fromkeys(drift + scope_drift(dirty, t, repo=wt)))
    if drift:
        out.append(f"  REFUSED: {br} carries {len(drift)} path(s) outside {tid}'s scope — " + ", ".join(drift[:12])
                   + (f" (+{len(drift) - 12} more)" if len(drift) > 12 else "") + "; not pushed, no PR")
        return {"ok": False, "url": None, "out": out}
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


LAND_TEST_TIMEOUT_KEY = "land_test_timeout_seconds"   # config/limits.yaml (T-0186)
LAND_TEST_TIMEOUT_S = 3600      # its default: T-0170's landing hit the old hardcoded 1800 s on 2026-09-24


def land_test_timeout(store: Store) -> int:
    """The landing test timeout in seconds: limits.yaml land_test_timeout_seconds, LAND_TEST_TIMEOUT_S when
    the key is absent or not a positive number."""
    try:
        v = int(store.config("limits").get(LAND_TEST_TIMEOUT_KEY) or 0)
    except (TypeError, ValueError, AttributeError):
        v = 0
    return v if v > 0 else LAND_TEST_TIMEOUT_S


def _run_tests(wl: Workload, cwd: Path, timeout: int = LAND_TEST_TIMEOUT_S) -> tuple[int, str]:
    """Run the workload's `tests:` command IN `cwd` (a worktree). Same resolution as scr.hotfix.run_tests,
    which is bound to the checkout: a relative interpreter path is taken relative to cwd (a spawned
    scriptorium worktree links `.venv` to the checkout's), a missing python.exe falls back to this one.
    land_scriptorium passes `timeout` from limits.yaml land_test_timeout_seconds (land_test_timeout)."""
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
        return 124, f"tests timed out after {timeout}s (raise {LAND_TEST_TIMEOUT_KEY} in config/limits.yaml)"
    except FileNotFoundError as e:
        return 127, f"test command not found: {e}"


# The scriptorium's record paths (T-0120): on a task branch they are never authoritative — they exist there
# only because a scriber ran `scr` with the worktree as root (T-0059's phantom [omri] journal commit and
# hotfix T-0108), so land_scriptorium makes them follow the trunk. `.scr/` is not one: it is gitignored and
# never tracked (`git ls-files .scr` is empty on main), so no branch can carry it.
RECORD_ROOTS = ("tasks/", "events/", "state/", "queues/", "graph/")


def is_record_path(path: str) -> bool:
    return str(path).replace("\\", "/").startswith(RECORD_ROOTS)


def _take_trunk(g: Git, ref: str, paths: list[str]) -> None:
    """Make `paths` in the worktree (and its index) what `ref` has: checked out from `ref` where it has
    them, removed where only the branch does. Works inside an unfinished merge and outside one."""
    have = set(l.replace("\\", "/") for l in g.run("ls-tree", "-r", "--name-only", "-z", ref, "--", *paths).out.split("\0") if l.strip())
    if [p for p in paths if p in have]:
        g.run("checkout", ref, "--", *[p for p in paths if p in have])
    if [p for p in paths if p not in have]:
        g.run("rm", "-q", "-f", "--ignore-unmatch", "--", *[p for p in paths if p not in have])


class _LandLock:
    """One landing per task at a time (T-0120, from T-0119's review): `scr land <id>` and the watcher tick
    both call land_scriptorium, and only step 5 holds the journal lock, so steps 1-4 could race in one
    worktree. `.scr/land-<id>.lock` holds "<pid> <time>"; it is never waited on — a second caller gets
    `busy`. A lock whose holder is dead, or older than the journal lock's LOCK_STALE_S, is taken over; the
    holder touches it every LOCK_STALE_S / 3 s, since the tests of step 4 can run far longer than that."""

    def __init__(self, root: Path, tid: str):
        self.path = root / SCR_DIR / f"land-{tid}.lock"
        self.fd: int | None = None
        self._stop: threading.Event | None = None

    def stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
            words = self.path.read_text(encoding="utf-8").split()
        except OSError:
            return True
        if not words or not words[0].isdigit():
            return age > 5                      # a holder between its create and its write, or garbage
        pid = int(words[0])
        from .run import pid_alive
        return age > LOCK_STALE_S or not pid_alive(pid)

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f"{os.getpid()} {time.time():.0f}\n".encode())
                break
            except FileExistsError:
                if not self.stale():
                    return False
                try:
                    self.path.unlink()          # stale: a crashed or dead holder
                except OSError:
                    return False
        else:
            return False
        self._stop = threading.Event()
        threading.Thread(target=self._beat, args=(self._stop,), daemon=True).start()
        return True

    def _beat(self, stop: threading.Event) -> None:
        while not stop.wait(LOCK_STALE_S / 3):
            try:
                os.utime(self.path)
            except OSError:
                pass

    def release(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            try:
                self.path.unlink()
            except OSError:
                pass


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
         the checkout never sees it — unless every conflicted path is under RECORD_ROOTS: those take the
         trunk's version, path by path, and the merge is committed (`records follow the trunk: ...`);
      3b. whatever still differs from the trunk under RECORD_ROOTS (added, modified, deleted) is reverted to
         the trunk's side in one commit and listed — except the task's own report.md / log.md, which fail
         the landing (`records`) by name: a scriber's words are moved to the live root, never dropped;
      4. run the workload's `tests:` command in the worktree — on exactly the tree the trunk will get —
         within limits.yaml land_test_timeout_seconds (default 3600; a timeout fails the landing: `tests`);
      5. journal the checkout, then under the journal lock merge task/<id> into the trunk: a fast-forward
         after step 3 unless the trunk moved meanwhile (journal commits: records, never the tooling under
         test), a merge commit then; a failure here is aborted and reported like the others;
      6. status merged, the state event and the task's log.md name the trunk commit.
    Testing on the task branch rather than on the trunk means a failing test never leaves the checkout —
    the tree every scr and the watcher itself run from — on a broken commit. Nothing is ever reset or
    deleted; the trunk moves forward or not at all. Record paths on task/<id> are resolved on task/<id>,
    before the tests, so the tests still run on exactly the tree the trunk gets.
    The whole landing holds `.scr/land-<id>.lock` (_LandLock); a second landing of the same task meanwhile
    returns `busy` without touching the worktree.
    Returns {"ok", "reason" (busy | scope | trunk | conflict | records | tests | ...), "out" (lines), "sha"
    (trunk head on success), "tip" (the task branch head after the attempt; the watcher keys its retry on it)}."""
    lock = _LandLock(root, tid)
    if not lock.acquire():
        store = Store(root)
        t = store.get(tid)
        wt = task_worktree(workload_for(root, store, t), t)
        tip = Git(wt).head() if wt.exists() else None
        return {"ok": False, "reason": "busy", "sha": None, "tip": tip,
                "out": [f"=== {tid} [{t.status}] task/{tid} (scriptorium)",
                        f"  FAILED (busy): another landing of {tid} holds {lock.path} — nothing touched"]}
    try:
        return _land_scriptorium(root, tid)
    finally:
        lock.release()


def _land_scriptorium(root: Path, tid: str) -> dict:
    """land_scriptorium's steps, under its lock."""
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
        c = scoped_commit(g, t, f"[{tid}] {title}\n\n{summary}\n\nScriptorium task {tid}; report: tasks/{t.dir.name}/report.md", tid)
        if c.sha:
            out.append(f"  committed {c.sha} ({len(dirty) - len(c.drift)} file(s)) on {br}")
        if c.drift_sha:
            out.append(f"  committed {c.drift_sha} [checkpoint-drift] ({len(c.drift)} out-of-scope path(s)) on {br}")
    # 1b. T-0079: the branch must not carry paths outside the task's scope (a [checkpoint-drift] commit, or
    # anything else): the trunk never gets them. The Steward widens scope.files / scope.mission in the record
    # when the task may own them (the watcher retries on that), or reverts the commit (a new tip retries).
    # Record paths are not drift here: step 3b reverts them to the trunk's, so they never reach it either.
    drift = [p for p in branch_drift(root, wl, g, t) if not is_record_path(p)]
    if drift:
        return fail("scope", f"{br} carries {len(drift)} path(s) outside {tid}'s scope: " + ", ".join(drift[:12])
                    + (f" (+{len(drift) - 12} more)" if len(drift) > 12 else "") + " — widen the record's scope or revert the [checkpoint-drift] commit")
    # 2. the checkout must sit on the trunk (scr ensure-main is the way to get there)
    cur = mg.branch()
    if cur != trunk:
        return fail("trunk", f"the checkout is on '{cur}', not {trunk} — `scr ensure-main` first")
    if mg.is_ancestor(br, trunk):
        out.append(f"  {br} is already contained in {trunk} — nothing to merge")
        out += _record_merged(root, store, tid, f"landed: {br} already contained in {trunk} @ {mg.head(short=True)}", report)
        _log_md(t, f"landed: {br} was already contained in {trunk} @ {mg.head(short=True)}")
        return {"ok": True, "reason": None, "out": out, "sha": mg.head(short=True), "tip": g.head()}
    # 3. trunk -> branch, in the worktree (a conflict is aborted there and never reaches the checkout). One
    # trunk commit for steps 3 and 3b: the checkout journals meanwhile, and 3b must not mistake that for the branch's.
    tsha = g.run("rev-parse", trunk).out
    own = {f"tasks/{t.dir.name}/report.md", f"tasks/{t.dir.name}/log.md"}
    followed: list[str] = []
    if not g.is_ancestor(tsha, br):
        msg = f"[land] {tid}: merge {trunk} into {br} before landing"
        m = g.run("merge", "--no-edit", "-m", msg, tsha)
        if not m.ok:
            conflicts = [l.strip() for l in g.run("diff", "--name-only", "--diff-filter=U").out.splitlines() if l.strip()]
            if not conflicts or not all(is_record_path(p) for p in conflicts):
                g.run("merge", "--abort")
                return fail("conflict", f"{trunk} does not merge into {br}: " + (", ".join(conflicts[:12]) if conflicts else m.out[-300:]))
            mine = sorted(own & set(conflicts))
            if mine:
                g.run("merge", "--abort")
                return fail("records", f"{mine[0]} was written in the worktree; move it to the live root (tasks/{t.dir.name}/) and retry")
            _take_trunk(g, tsha, conflicts)
            c = g.run("commit", "--no-edit", "-m", f"{msg}\n\nrecords follow the trunk: " + ", ".join(conflicts))
            if not c.ok:
                g.run("merge", "--abort")
                return fail("conflict", f"{trunk} does not merge into {br}: taking the trunk's records failed: {c.out[-300:]}")
            followed += conflicts
            out.append(f"  merged {trunk} into {br}: {g.head(short=True)}; records follow the trunk: " + ", ".join(conflicts))
        else:
            out.append(f"  merged {trunk} into {br}: {g.head(short=True)}")
    # 3b. record paths follow the trunk — also when step 3 had nothing to merge
    rec = [l.replace("\\", "/") for l in g.run("diff", "--name-only", "-z", tsha, "HEAD", "--", *[r.rstrip("/") for r in RECORD_ROOTS]).out.split("\0") if l.strip()]
    mine = sorted(own & set(rec))
    if mine:
        return fail("records", f"{mine[0]} was written in the worktree; move it to the live root (tasks/{t.dir.name}/) and retry")
    if rec:
        _take_trunk(g, tsha, rec)
        c = g.run("commit", "-q", "-m", f"[land] {tid}: record paths follow the trunk ({len(rec)} file(s))\n\n" + "\n".join(rec))
        if not c.ok or g.run("diff", "--name-only", tsha, "HEAD", "--", *[r.rstrip("/") for r in RECORD_ROOTS]).out:
            return fail("records", f"reverting {len(rec)} record path(s) on {br} to {trunk}'s failed: {c.out[-300:]}")
        followed += [p for p in rec if p not in followed]
        out.append(f"  records follow the trunk: {len(rec)} path(s) reverted on {br}: " + ", ".join(rec))
    # 4. the tests, on the tree the trunk will get
    rc, txt = _run_tests(wl, wt, land_test_timeout(store))
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
    _log_md(t, f"landed: {br} -> {trunk} @ {head} ({how}); tests pass"
            + (f"; records follow the trunk: {', '.join(followed)}" if followed else ""))
    out.append(f"  {tid} marked merged")
    return {"ok": True, "reason": None, "out": out, "sha": head, "tip": g.head()}


def _log_md(t: Task, line: str) -> None:
    try:
        with (t.dir / "log.md").open("a", encoding="utf-8") as f:
            f.write(f"- {now_iso()} [steward:{t.id}] {line}\n")
    except OSError:
        pass


def is_scriptorium_task(root: Path, store: Store | None, t: Task) -> bool:
    """True when the task's workload IS this checkout (`repo: .`): the repo identity, the same test
    land_scriptorium makes — not the task id or the `workload:` string. A record naming an unregistered
    workload resolves as workload_for does (the first entry)."""
    return workload_for(root, store, t).repo == root.resolve()


# What the operator does after a hand-path landing failure, per land_scriptorium reason (the watcher's
# land_failed note names the same reasons).
_LAND_NEXT = {
    "conflict": "merge {trunk} into {br} in {wt}, resolve, commit, then `scr land {tid}` again",
    "tests": "fix {br} in {wt} (committed or not), then `scr land {tid}` again",
    "trunk": "`scr ensure-main` in the checkout, then `scr land {tid}` again",
    "scope": "widen the record's scope or revert the [checkpoint-drift] commit on {br}, then `scr land {tid}` again",
    "records": "move the named file's text from {wt} into the live root, `git checkout {trunk} -- <file>` in {wt}, then `scr land {tid}` again",
    "busy": "another landing of {tid} is running; wait for it, then `scr land {tid}` again",
    "lost_worktree": "recreate the worktree (git worktree add {wt} {br}) or merge {br} into {trunk} by hand, then `scr land {tid}` again",
}

# The merged note of a checkout hotfix (an accepted scriptorium task with no worktree): the journal already
# put its commits on the trunk. Written only by land_no_worktree, for `scr land` and the watcher alike (T-0158).
HOTFIX_MERGED_NOTE = "tooling task: change already on main (journal)"


def land_no_worktree(root: Path, store: Store, t: Task, wl: Workload, dry_run: bool = False) -> dict:
    """The one landing of an accepted scriptorium task with no worktree (T-0154, T-0158), for land_any and
    Watcher.land_scriptorium_task. A checkout hotfix — no task/<id> branch, or one already contained in the
    trunk — is recorded merged with HOTFIX_MERGED_NOTE and nothing else: no merge, no landed event. A task/<id>
    that carries commits NOT on the trunk is a spawned task whose worktree was lost (removed by hand, a failed
    retire): recording it merged would drop its work, so it is refused with reason `lost_worktree`, tip = the
    branch tip, and the task stays accepted."""
    tid = t.id
    wt, br, trunk = task_worktree(wl, t), f"task/{tid}", wl.main
    mg = Git(root)
    head = f"=== {tid} [{t.status}] no worktree ({wt}) -> {trunk} (scriptorium"
    if mg.branch_exists(br) and not mg.is_ancestor(br, trunk):
        tip = mg.run("rev-parse", br).out.strip() or None
        why = f"FAILED (lost_worktree): worktree missing, branch {br} not on {trunk}: {br} carries commits that are not on {trunk}"
        return {"ok": False, "reason": "lost_worktree", "sha": None, "tip": tip, "url": None,
                "out": [head + (", dry run: nothing changed)" if dry_run else ")"), ("  would refuse — " if dry_run else "  ") + why]}
    if dry_run:
        return {"ok": True, "reason": None, "sha": None, "tip": None, "url": None,
                "out": [head + ", dry run: nothing changed)", f"  no worktree: record merged only (change already on {trunk} via the journal)"]}
    out = [head + ")"] + _record_merged(root, store, tid, HOTFIX_MERGED_NOTE) + [f"  {tid} marked merged ({HOTFIX_MERGED_NOTE})"]
    return {"ok": True, "reason": None, "out": out, "sha": None, "tip": None, "url": None}


def land_any(root: Path, tid: str, no_pr: bool = False, draft: bool = False, dry_run: bool = False, merge: bool = False) -> dict:
    """The one place that decides which landing a task gets (T-0119): a scriptorium task (is_scriptorium_task)
    goes through land_scriptorium — commit, trunk merged in, tests, merge into the trunk, merged — exactly the
    watcher's land step; every other workload goes through the quantum-insertion land() with the QI flags.
    The QI flags do not apply to the scriptorium path; dry_run there reports the plan and changes nothing.
    land_scriptorium is the post-acceptance step, so a scriptorium task must be `accepted` (land() accepts a
    reviewed `submitted` task because the PR merge implies the review; here the Decision comes first).
    An accepted scriptorium task with no worktree goes through land_no_worktree (T-0158): merged with
    HOTFIX_MERGED_NOTE when it is a checkout hotfix, refused (lost_worktree) when task/<id> is not on the trunk.
    A failure returns ok False with one closing line naming the reason and what to do next."""
    store = Store(root)
    t = store.get(tid)
    if not is_scriptorium_task(root, store, t):
        return land(root, tid, no_pr=no_pr, draft=draft, dry_run=dry_run, merge=merge)
    if t.status == "submitted":
        raise RepoError(f"{tid} is submitted, not accepted — write the Decision first (T-0081)")
    if t.status != "accepted":
        raise RepoError(f"{tid} is {t.status}, not accepted — only an accepted scriptorium task lands")
    wl = workload_for(root, store, t)
    wt, br, trunk = task_worktree(wl, t), f"task/{tid}", wl.main
    if not wt.exists():
        r = land_no_worktree(root, store, t, wl, dry_run=dry_run)
        if not r["ok"]:
            r["out"].append(f"{tid}: NOT landed ({r['reason']}) — next: " + _LAND_NEXT[r["reason"]].format(trunk=trunk, br=br, wt=wt, tid=tid))
        return r
    if dry_run:
        g, mg = Git(wt), Git(root)
        dirty = g.dirty()
        out = [f"=== {tid} [{t.status}] {br} -> {trunk} (scriptorium, dry run: nothing changed)",
               f"  worktree {wt} on '{g.branch()}'; {len(dirty)} uncommitted file(s) to commit on {br}",
               f"  checkout on '{mg.branch()}'" + ("" if mg.branch() == trunk else f" — not {trunk}: `scr ensure-main` first"),
               f"  {br} already contained in {trunk}: record merged only" if not dirty and mg.is_ancestor(br, trunk)
               else f"  would merge {trunk} into {br}, run `{wl.cfg.get('tests') or 'pytest tests -q'}` in the worktree, merge {br} into {trunk}"]
        return {"ok": True, "reason": None, "out": out, "sha": None, "tip": g.head(), "url": None}
    r = land_scriptorium(root, tid)
    if not r["ok"]:
        nxt = _LAND_NEXT.get(r["reason"], "see the FAILED line above, then `scr land {tid}` again").format(trunk=trunk, br=br, wt=wt, tid=tid)
        r["out"].append(f"{tid}: NOT landed ({r['reason']}) — next: {nxt}")
    else:
        r["out"].append(f"  next: `scr retire {tid} --execute` quarantines the worktree (the watcher does this itself)")
    return {**r, "url": None}


# ---- sweep: the hygiene audit pass (docs/hygiene-protocols.md §3, §10, §11 F1) ------------------
HYGIENE_KINDS = ("dirty-mission", "finished", "phantom", "orphan", "locked", "live")
FINISHED_STATUSES = {"merged", "closed", "rejected"}
TASK_DIR = re.compile(r"^T-\d+$")


def _norm(p: str | Path) -> str:
    return os.path.normcase(str(Path(p).resolve()))


def registered_worktrees(repo: Path) -> list[dict]:
    """`git worktree list --porcelain` of a repo minus its main checkout: [{path, branch, locked, reason}]."""
    r = Git(repo).run("worktree", "list", "--porcelain")
    if not r.ok:
        return []
    out: list[dict] = []
    for block in r.out.replace("\r\n", "\n").split("\n\n"):
        cur: dict = {}
        for line in block.splitlines():
            key, _, val = line.partition(" ")
            if key == "worktree":
                cur = {"path": val, "branch": None, "locked": False, "reason": ""}
            elif key == "branch" and cur:
                cur["branch"] = val.removeprefix("refs/heads/")
            elif key == "detached" and cur:
                cur["branch"] = "HEAD"
            elif key == "locked" and cur:
                cur["locked"], cur["reason"] = True, val
        if cur:
            out.append(cur)
    return out[1:]                      # the first entry is the repo's own checkout


def audit_workload(root: Path, wl: Workload, store: Store | None = None, fetch: bool = False) -> list[dict]:
    """Protocol A's detector for one workload: one typed row per worktree the repo registers or the
    worktree root holds, kind in HYGIENE_KINDS. Read-only apart from `fetch` (the daily pass fetches
    origin first; `--dry-run` never does, so it leaves the repo's refs as they were). Classification only:
    the repairs (retire, prune, quarantine, leftover commits) are F2."""
    store = store or Store(root)
    if not Git(wl.repo).is_repo():
        return []
    mg = Git(wl.repo)
    if fetch and wl.repo != root.resolve():
        mg.fetch()
    base = branch_base(root, wl, mg)
    records = store.load_all()
    reg = {_norm(w["path"]): w for w in registered_worktrees(wl.repo)}
    rows: list[dict] = []

    def row(name: str, path: Path | str, kind: str, branch: str | None = None, **kw) -> dict:
        r = {"workload": wl.name, "worktree": name, "path": str(path), "branch": branch, "kind": kind,
             "files": 0, "sample": [], "sha": None, "url": None, "ok": True, "note": ""}
        r.update(kw)
        rows.append(r)
        return r

    for key, w in sorted(reg.items()):
        if not Path(w["path"]).exists():                                    # A2 phantom
            row(Path(w["path"]).name, w["path"], "phantom", w["branch"], note="registered, directory gone")
    dirs = sorted(p for p in wl.wt_root.iterdir() if p.is_dir()) if wl.wt_root.is_dir() else []
    for d in dirs:
        w = reg.get(_norm(d))
        if w is None:                                                       # A3 orphan: not a worktree of wl.repo
            row(d.name, d, "orphan", note="not registered in git worktree list")
            continue
        g = Git(d)
        br = w["branch"] or (g.run("rev-parse", "--abbrev-ref", "HEAD").out or None)
        is_task = bool(TASK_DIR.match(d.name))
        dirty = [] if is_task else g.dirty()
        if dirty:                                                           # D3: what `scr sweep` commits
            row(d.name, d, "dirty-mission", br, files=len(dirty), sample=dirty[:25])
            continue
        if w["locked"]:                                                     # A4: a deliberate human act
            row(d.name, d, "locked", br, note=w["reason"] or "locked")
            continue
        status = records[d.name].status if is_task and d.name in records else None
        sha = g.head()
        clean = not g.run("status", "--porcelain", "--untracked-files=no").out.strip()
        merged = bool(sha) and mg.is_ancestor(sha, base)
        if is_task:
            done = status in FINISHED_STATUSES
        else:
            done = bool(br) and (mg.remote_contains(f"origin/{br}", sha or br) or mg.remote_contains(f"origin/{wl.main}", sha or br))
        extra = {"status": status} if is_task else {}
        if merged and clean and done:                                       # A1 finished
            row(d.name, d, "finished", br, note=f"ancestor of {base}", **extra)
        else:
            why = [] if clean else ["uncommitted tracked changes"]
            if not merged:
                why.append(f"not in {base}")
            if is_task and not done:
                why.append(f"record {status or 'missing'}")
            row(d.name, d, "live", br, note="; ".join(why), **extra)
    return rows


def _sweep_commit(root: Path, wl: Workload, row: dict, merge: bool) -> None:
    """Commit + push one dirty mission worktree (and with merge=True, PR + merge into <main>)."""
    d, br = Path(row["path"]), row["branch"]
    g = Git(d)
    try:
        row["sha"] = g.commit_all(f"[sweep] uncommitted work found in .worktrees/{d.name} on {br} ({now_iso()})\n\nSwept by `scr sweep` so it exists in git history; content unreviewed.")
    except GitError as e:
        row["ok"] = False; row["note"] = str(e)[-300:]; return
    r = g.push(br)
    if not r.ok:
        row["ok"] = False; row["note"] = "push failed: " + r.out[-300:]; return
    if merge:
        if not gh_available():
            row["note"] = f"gh missing; PR by hand: {br} -> {wl.main}"; return
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
    _log(root).append("system", name="sweep", actor="steward-watch", note=f"{d.name} ({br}): {row['files']} file(s) -> {row['sha']}" + (f" PR {row['url']}" if row["url"] else ""))


def sweep(root: Path, merge: bool = False, dry_run: bool = False, wl_name: str | None = None,
          all_workloads: bool = False, fetch: bool = False) -> list[dict]:
    """Every non-task worktree (missions like qi-mission-24) with uncommitted work: commit on its
    branch, push, and with merge=True PR + merge into <main>. Returns one row per such worktree
    (kind `dirty-mission`) of one workload — `scr/chores.py` `no_dirty_worktrees` reads exactly that.

    all_workloads=True (`scr sweep --all`, T-0202) is the hygiene audit: every workload in
    config/workloads.yaml, one typed row per worktree (HYGIENE_KINDS); only `dirty-mission` rows are
    ever committed, so a `T-*` worktree is listed but never swept. dry_run never commits or pushes."""
    store = Store(root)
    names = list(store.config("workloads") or {}) if all_workloads else [wl_name]
    rows: list[dict] = []
    for name in names:
        wl = workload(root, store, name)
        for r in audit_workload(root, wl, store, fetch=fetch):
            if not all_workloads and r["kind"] != "dirty-mission":
                continue
            rows.append(r)
            if r["kind"] == "dirty-mission" and not dry_run:
                _sweep_commit(root, wl, r, merge)
    return rows


def repo_position(wl: Workload) -> dict:
    """The checkout's branch and <main> vs origin/<main> as (ahead, behind), for the digest; no fetch."""
    g = Git(wl.repo)
    br = g.run("rev-parse", "--abbrev-ref", "HEAD")
    lr = g.run("rev-list", "--left-right", "--count", f"{wl.main}...origin/{wl.main}")
    ahead, behind = (lr.out.split() + ["?", "?"])[:2] if lr.ok else ("?", "?")
    return {"branch": br.out if br.ok else "?", "ahead": ahead, "behind": behind}


def hygiene_digest(root: Path, rows: list[dict], day: str) -> Path:
    """Write reports/hygiene/<day>.md from the audit rows (§10). Informational: nothing in it is a task,
    a chore or a card."""
    store = Store(root)
    lines = [f"# Hygiene digest {day}", "",
             "Written by the watcher's daily pass (`scr sweep --all`, classification only). Nothing here is an "
             "Omri-queue item (docs/hygiene-protocols.md §9).", ""]
    for name in (store.config("workloads") or {}):
        wl = workload(root, store, name)
        mine = [r for r in rows if r["workload"] == name]
        pos = repo_position(wl) if Git(wl.repo).is_repo() else None
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"- repo `{wl.repo}`: " + (f"checkout on `{pos['branch']}`, `{wl.main}` {pos['ahead']} ahead / {pos['behind']} behind `origin/{wl.main}`" if pos else "not a git repo"))
        lines.append("- worktrees: " + ", ".join(f"{k} {sum(1 for r in mine if r['kind'] == k)}" for k in HYGIENE_KINDS))
        shown = [r for r in mine if r["kind"] != "live"]
        if shown:
            lines += ["", "| worktree | branch | kind | detail |", "|---|---|---|---|"]
            for r in shown:
                detail = f"{r['files']} file(s)" if r["kind"] == "dirty-mission" else r.get("note", "")
                lines.append(f"| {r['worktree']} | {r['branch'] or ''} | {r['kind']} | {detail} |")
        lines.append("")
    counts = {n: 0 for n in ("journal_failed", "push_failed", "fetch_failed")}
    for e in _log(root).read():
        if e.get("kind") == "system" and e.get("name") in counts and str(e.get("ts", "")).startswith(day):
            counts[e["name"]] += 1
    lines += ["## Failures today", "", ", ".join(f"{k} {v}" for k, v in counts.items()), ""]
    p = root / "reports" / "hygiene" / f"{day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


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
    # T-0188: merged means merged into origin/<main> (fetched first) for a workload with its own checkout — its
    # local main can sit far behind (quantum-insertion's master, 53 commits on 2026-09-24, refused merged T-0115
    # as 52 ahead); the scriptorium's own workload keeps its checkout trunk (branch_base). A failed fetch
    # (offline) measures against the last fetched origin ref.
    if wl.repo != root.resolve():
        mg.fetch()
    base = branch_base(root, wl, mg)
    out = [f"=== retire {name} (branch {br}, main {base}) [{'execute' if execute else 'dry-run'}]"]
    if not mg.is_ancestor(br, base):
        ahead = mg.run("rev-list", "--count", f"{base}..{br}").out
        return {"ok": False, "code": 2, "out": out + [f"REFUSING: {br} is {ahead} commit(s) ahead of {base} — land it first"]}
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
    """Land + merge every submitted/accepted task, one after the other; failures are reported, not fatal.
    A submitted task whose review.md does not support `accepted` (T-0081) is skipped with the reason.
    Each task is routed by land_any: a scriptorium task lands through land_scriptorium (T-0119)."""
    out: list[str] = []
    for t in sorted(Store(root).load_all().values(), key=lambda t: t.id):
        if t.status not in {"submitted", "accepted"}:
            continue
        if t.status == "submitted":
            problem = acceptance_problem(t.dir)
            if problem:
                out.append(f"{t.id}: skipped, not landed — {problem}")
                continue
        try:
            r = land_any(root, t.id, merge=True)
            out += r["out"]
            if not r["ok"]:
                out.append(f"{t.id}: NOT landed")
        except Exception as e:  # noqa: BLE001
            out.append(f"{t.id}: failed: {e}")
    return out or ["nothing submitted or accepted"]

