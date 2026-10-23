"""Workload-repo operations (quantum-insertion): worktrees, checkpoint commits, landing, sweeping,
retiring. Python ports of the former hooks/*.ps1 and retire_worktree.sh so they run and are
tested on any OS. Invariants: never force-push, never touch the trusted branch, never delete a branch; retire
deletes only a finished task's worktree, links unlinked and untracked value copied out first (T-0321).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .events import EventLog, now_iso
from .gitops import Git, GitError, gh_available, pr_create, pr_merge, pr_url_for
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


def is_frozen(store: Store, workload: str | None) -> bool:
    """`frozen: true` on the workload's entry in config/workloads.yaml (T-0272; absent = false). The name resolves
    the way `workload()` does: missing or unregistered -> the first entry. The one reader of the flag: while it
    holds, the watcher does not auto-dispatch, auto-review or auto-land the workload's tasks, `scr dispatch`
    refuses them unless the actor is omri, and `scr verify` and the UI header say so."""
    wls = store.config("workloads") or {}
    if not wls:
        return False
    if not workload or workload not in wls:
        workload = next(iter(wls))
    return (wls.get(workload) or {}).get("frozen") is True


def frozen_workloads(store: Store) -> list[str]:
    """The registered workloads with `frozen: true`, in config order."""
    return [name for name in (store.config("workloads") or {}) if is_frozen(store, name)]


def task_frozen(store: Store, t: Task) -> bool:
    """True when the task's own workload (T-0036's `workload:`) is frozen."""
    return is_frozen(store, (t.rec or {}).get("workload"))


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
        # a `.venv/` ignore rule matches directories only, and git sees a POSIX symlink as a file: without this the
        # checkpoint commits the link and the landing refuses it as out of scope (a junction is a directory to git)
        r = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=wt, capture_output=True, text=True)
        if r.returncode == 0:
            ex = (wt / r.stdout.strip()).resolve()
            have = ex.read_text(encoding="utf-8").splitlines() if ex.exists() else []
            if "/.venv" not in have:
                ex.parent.mkdir(parents=True, exist_ok=True)
                ex.write_text("\n".join([*have, "/.venv"]) + "\n", encoding="utf-8")


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
                broken = g.broken_refs()
                out.append(f"  worktree add failed: {r.out}" + (f" (broken ref(s) in {wl.repo.name}: {', '.join(broken)})" if broken else ""))
                continue
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
    gitdir = g.run("rev-parse", "--git-dir").out.strip()
    if gitdir and any((Path(gitdir) if Path(gitdir).is_absolute() else wt / gitdir).joinpath(f).exists() for f in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD")):
        # 2026-10-14: a merge / rebase in progress (Omri resolving T-0291's PR conflict by hand) is never checkpointed:
        # the commit would swallow MERGE_HEAD and turn the merge into a plain commit carrying the trunk's files as drift
        _log(root).append("system", name="checkpoint_skipped", task=tid, actor=f"steward:{tid}", note="merge/rebase in progress in the worktree; not checkpointed")
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

    # 2026-10-21 hotfix (T-0560 double landing): a branch that origin/<main> already contains — merged by an earlier
    # landing job or by hand — is landed by recording the merge, not by pushing and opening a PR again. Before this,
    # the re-applied `accepted` of an already-merged task queued a second landing whose PR creation failed, the
    # watcher read "rc 0 but not merged" as a test failure, and auto-fix dispatched a scriber on a merged task.
    mg0 = Git(wl.repo)
    mg0.fetch()
    if mg0.remote_contains(f"origin/{wl.main}", br) and _merged_off_first_parent(mg0, f"origin/{wl.main}", g.head()):
        out.append(f"  already in origin/{wl.main}: nothing to push, no PR")
        if t.status == "submitted":
            from . import lifecycle
            lifecycle.apply(t.rec, "accepted"); t.save()
            log.append("state", task=tid, **{"from": "submitted"}, to="accepted", actor=f"steward:{tid}", note=f"already in origin/{wl.main}")
            t = store.get(tid)
        if t.status != "merged":
            out.append(f"  {tid} marked merged (branch already in {wl.main})")
            out += _record_merged(root, store, tid, f"already in origin/{wl.main} (recorded by land)", report)
        else:
            out.append(f"  {tid} is already merged")
        return {"ok": True, "url": pr_url_for(br, wt) if (not no_pr and gh_available()) else None, "out": out}

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
                if not url and merge:
                    # 2026-10-21 hotfix: a landing asked to merge does not stop at a PR that would not open (gh, auth,
                    # a stale PR state) and does not route the branch to Omri: it merges directly — origin/<main> into
                    # the branch in the worktree (a conflict is aborted and reported as such), then the branch pushed
                    # as <main> (a fast-forward of the remote trunk onto the branch tip). Only when that fails too is
                    # it a FAILED (pr) line, which the watcher retries on later ticks before raising a card.
                    direct = _direct_merge_to_main(g, wl.main, br, tid)
                    out += direct["out"]
                    if direct["ok"]:
                        mg = Git(wl.repo)
                        mg.fetch()
                        if mg.remote_contains(f"origin/{wl.main}", br):
                            if t.status == "submitted":
                                from . import lifecycle
                                lifecycle.apply(t.rec, "accepted"); t.save()
                                log.append("state", task=tid, **{"from": "submitted"}, to="accepted", actor=f"steward:{tid}", note=f"landed on {br} (direct merge)")
                            out.append(f"  merged -> {wl.main} (direct, no PR); {tid} marked merged")
                            out += _record_merged(root, store, tid, f"auto-land: direct merge into {wl.main} (PR creation failed)", report)
                            return {"ok": True, "url": None, "out": out}
                        out.append(f"  WARNING: origin/{wl.main} does not contain {br} after the direct merge — not marking merged")
                    if direct.get("conflict"):
                        out.append(f"  merge failed: {direct.get('why', '')[:300]}")
                    else:
                        out.append(f"  FAILED (pr): PR creation failed and the direct merge did not land; {direct.get('why', '')[:300]}")
                    return {"ok": False, "url": None, "out": out}
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


def _direct_merge_to_main(g: Git, main: str, br: str, tid: str) -> dict:
    """2026-10-21 hotfix: land `br` on `main` without a PR. In the worktree: fetch, merge origin/<main> INTO the branch
    (nothing to do when the branch already contains it; a conflict is aborted, nothing is pushed, `conflict` is set),
    then push the branch tip as origin/<main> — a fast-forward, since the tip now contains the trunk. Never touches
    the trusted branch, never force-pushes, never checks out <main> anywhere."""
    out: list[str] = []
    r = g.run("fetch", "--quiet", "origin", main, timeout=600)
    if not r.ok:
        return {"ok": False, "out": out, "why": f"fetch origin {main}: {r.out[-300:]}"}
    if not g.ok("merge-base", "--is-ancestor", f"origin/{main}", "HEAD"):
        m = g.run("merge", "--no-ff", "--no-edit", "-m", f"merge origin/{main} into {br} (direct landing of {tid}; PR creation failed)", f"origin/{main}", timeout=600)
        if not m.ok:
            g.run("merge", "--abort")
            return {"ok": False, "conflict": True, "out": out + [f"  direct merge: origin/{main} conflicts with {br}; aborted"], "why": m.out[-300:]}
        out.append(f"  direct merge: origin/{main} merged into {br} ({g.head(short=True)})")
    p = g.run("push", "origin", f"HEAD:refs/heads/{main}", timeout=600)
    if not p.ok:
        return {"ok": False, "out": out, "why": f"push {br} -> {main}: {p.out[-300:]}"}
    out.append(f"  direct merge: pushed {br} as origin/{main} (fast-forward)")
    return {"ok": True, "out": out}


def _merged_off_first_parent(mg: Git, main_ref: str, tip: str | None) -> bool:
    """True when `tip` is reachable from `main_ref` but NOT on its first-parent line: the signature of a branch
    merged by a merge commit (a PR). A branch with no commits of its own sits ON the first-parent line and is
    not "already merged" — it is simply empty (2026-10-21 hotfix; keeps `land` of a fresh branch unchanged)."""
    if not tip:
        return False
    r = mg.run("rev-list", "--first-parent", main_ref)
    return r.ok and tip not in {l.strip() for l in r.out.splitlines()}


def _record_merged(root: Path, store: Store, tid: str, note: str, report: str = "") -> list[str]:
    """The record side of a landing, of the push/PR `land`:
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


# The scriptorium's record paths (T-0120): on a task branch they are never authoritative — they exist there
# only because a scriber ran `scr` with the worktree as root (T-0059's phantom [omri] journal commit and
# hotfix T-0108). `.scr/` is not one: it is gitignored and never tracked, so no branch can carry it.
RECORD_ROOTS = ("tasks/", "events/", "state/", "queues/", "graph/")


def is_record_path(path: str) -> bool:
    return str(path).replace("\\", "/").startswith(RECORD_ROOTS)


def is_scriptorium_task(root: Path, store: Store | None, t: Task) -> bool:
    """True when the task's workload IS this checkout (`repo: .`): the repo identity — not the task id or
    the `workload:` string. A record naming an unregistered
    workload resolves as workload_for does (the first entry)."""
    return workload_for(root, store, t).repo == root.resolve()


TOOLING_LANDS_AS_PR = ("tooling lands as a PR in the scr code repo (branch, PR, CI, merge); the state checkout no longer "
                      "merges tooling branches. Once its PR is merged there: `scr --actor omri task set {tid} merged --note <PR url>`")


def land_any(root: Path, tid: str, no_pr: bool = False, draft: bool = False, dry_run: bool = False, merge: bool = False) -> dict:
    """The one place that decides which landing a task gets (T-0119). Every workload goes through land()
    (push, PR, merge). A task whose workload IS this checkout (`repo: .`, is_scriptorium_task) is refused:
    since the split of code from state, tooling lives in its own repo and lands there as a PR; merging a
    tooling branch into the journaled checkout (the old land_scriptorium) is gone. The refusal is
    ok False with reason `tooling` and one closing line saying so."""
    store = Store(root)
    t = store.get(tid)
    if not is_scriptorium_task(root, store, t):
        return land(root, tid, no_pr=no_pr, draft=draft, dry_run=dry_run, merge=merge)
    out = [f"=== {tid} [{t.status}] (workload repo is this checkout)",
           f"{tid}: NOT landed (tooling) — " + TOOLING_LANDS_AS_PR.format(tid=tid)]
    return {"ok": False, "reason": "tooling", "out": out, "sha": None, "tip": None, "url": None}


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


# ---- retire: delete a finished task's worktree, quarantine a live one (T-0321) ----------------
# 2026-10-10: C: reached 0 bytes free with 55 worktrees x ~600 MB of .lake/build; Omri deleted 13 merged trees by
# hand. Since T-0321 a worktree whose task is merged / closed / rejected is DELETED (`git worktree remove --force` +
# `git worktree prune`; the branch and every commit stay); returned / escalated / omri_queue keep the quarantine
# move into <repo>-RETIRED/, minus .lake/build. The two traps the quarantine guarded against still hold: untracked
# or ignored files outside REGENERABLE are copied out (sha-256 checked) before anything is deleted, and every link
# (the .venv link, a shared .lake/packages junction) is unlinked first, never recursed into.
DELETE_STATUSES = FINISHED_STATUSES
QUARANTINE_STATUSES = {"returned", "escalated", "omri_queue"}
REGENERABLE_DIRS = {".lake", "build", "__pycache__", ".pytest_cache"}   # at any depth; plus the top-level .venv link
KEPT_LISTED = 200                                                       # files named in the event; KEPT.txt has all


def _is_link(p: Path) -> bool:
    """A symlink, or (Windows) any reparse point — a directory junction above all, which is_symlink() misses."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    import stat as _stat
    return _stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)   # FILE_ATTRIBUTE_REPARSE_POINT


def _unlink_link(p: Path) -> None:
    """Remove the link itself, never its target: rmdir removes a junction / directory symlink on Windows, unlink a
    symlink on POSIX (and a file link anywhere)."""
    if os.name == "nt" and os.path.isdir(p):
        os.rmdir(p)
    else:
        os.unlink(p)


def _scan(top: Path) -> tuple[list[tuple[str, int]], list[str]]:
    """(files as (relpath, bytes), directory links as relpath) under `top`, walked without following any link."""
    files: list[tuple[str, int]] = []
    links: list[str] = []
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            p = Path(e.path)
            rel = p.relative_to(top).as_posix()
            if _is_link(p) and (os.path.isdir(p) or e.is_dir(follow_symlinks=False)):
                links.append(rel)
            elif e.is_dir(follow_symlinks=False):
                stack.append(p)
            else:
                try:
                    files.append((rel, os.lstat(p).st_size))
                except OSError:
                    files.append((rel, 0))
    return files, links


def tree_bytes(top: Path) -> int:
    """Bytes on disk under `top`, links not followed (what deleting it frees)."""
    return sum(n for _, n in _scan(top)[0]) if top.is_dir() else 0


def _rmtree_nolinks(top: Path) -> None:
    """Delete a directory tree; every link inside is unlinked, never entered. `top` itself may be a link."""
    if _is_link(top):
        _unlink_link(top); return
    for rel in _scan(top)[1]:
        _unlink_link(top / rel)

    def _retry(fn, path, _exc):          # a read-only file (git objects, lake oleans) on Windows
        os.chmod(path, 0o700); fn(path)
    shutil.rmtree(top, onerror=_retry)


def _regenerable(rel: str, links: set[str]) -> bool:
    parts = rel.split("/")
    if parts[0] == ".venv" and ".venv" in links:
        return True
    return any(p in REGENERABLE_DIRS for p in parts[:-1])


def _within(a: Path, b: Path) -> bool:
    return a == b or b in a.parents


def _target_problem(root: Path, wl: Workload, wt: Path) -> str | None:
    """Why `wt` must not be deleted or moved: it has to be a direct child of the workload's worktree root, and neither
    the repo, the scriptorium, the worktree root, nor anything inside <repo>-RETIRED/."""
    p, base = wt.resolve(), wl.wt_root.resolve()
    retired = (wl.repo.parent / f"{wl.repo.name}-RETIRED").resolve()
    if p.parent != base:
        return f"{wt} resolves to {p}, outside the worktree root {base}"
    if _within(p, retired) or _within(retired, p):
        return f"{p} is (or holds) the quarantine root {retired}"
    for other, what in ((wl.repo.resolve(), "the repo"), (root.resolve(), "the scriptorium"), (base, "the worktree root")):
        if _within(other, p):
            return f"{p} is (or holds) {what} {other}"
    return None


def _sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def retire(root: Path, name: str, execute: bool = False, wl_name: str | None = None) -> dict:
    """Dispose of one worktree by its task's status (T-0321): merged / closed / rejected -> delete; returned /
    escalated / omri_queue -> quarantine (a move into <repo>-RETIRED/, .lake/build dropped); a non-task worktree
    (a mission) -> quarantine once merged, as before; any other status -> refused (code 4, the work is live).
    Codes: 0 done, 1 verification failed, 2 not merged (or HEAD on no ref), 3 dirty tracked files, 4 live task,
    5 unsafe target. Never touches a branch."""
    status = None
    _st = Store(root)
    if name in _st.load_all():
        rec = _st.get(name).rec
        status = rec.get("status")
        if wl_name is None:             # a task's worktree lives under ITS workload's root
            wl_name = rec.get("workload")
    wl = workload(root, None, wl_name)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in (".", "..") or ".." in name or name.startswith("-"):
        raise RepoError(f"illegal worktree name: {name}")
    wt = wl.wt_root / name
    if not wt.is_dir() or _is_link(wt):
        raise RepoError(f"no such worktree (or it is a link): {wt}")
    problem = _target_problem(root, wl, wt)
    if problem:
        return {"ok": False, "code": 5, "out": [f"REFUSING: {problem}"]}
    mode = "delete" if status in DELETE_STATUSES else "quarantine" if status in QUARANTINE_STATUSES or status is None else None
    g, mg = Git(wt), Git(wl.repo)
    br = g.branch()
    out = [f"=== retire {name} (branch {br}, task {status or '-'}) [{'execute' if execute else 'dry-run'}]"]
    if mode is None:
        return {"ok": False, "code": 4, "out": out + [f"REFUSING: task {name} is {status}; its worktree is live work"]}
    # T-0188: merged means merged into origin/<main> (fetched first) for a workload with its own checkout — its
    # local main can sit far behind (quantum-insertion's master, 53 commits on 2026-09-24, refused merged T-0115
    # as 52 ahead); the scriptorium's own workload keeps its checkout trunk (branch_base). A failed fetch
    # (offline) measures against the last fetched origin ref.
    if status in (None, "merged"):
        if wl.repo != root.resolve():
            mg.fetch()
        base = branch_base(root, wl, mg)
        out[0] = out[0].replace(f"task {status or '-'})", f"task {status or '-'}, main {base})")
        if not mg.is_ancestor(br, base):
            ahead = mg.run("rev-list", "--count", f"{base}..{br}").out
            return {"ok": False, "code": 2, "out": out + [f"REFUSING: {br} is {ahead} commit(s) ahead of {base} — land it first"]}
    elif mode == "delete" and not mg.run("for-each-ref", "--count=1", "--contains", g.head(), "refs/heads/", "refs/remotes/").out.strip():
        return {"ok": False, "code": 2, "out": out + [f"REFUSING: HEAD {g.head()[:10]} is on no branch; deleting the worktree would orphan it"]}
    if g.run("diff", "--stat", "HEAD").out.strip():
        return {"ok": False, "code": 3, "out": out + ["REFUSING: uncommitted changes to tracked files (checkpoint commit first)"]}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    retired = wl.repo.parent / f"{wl.repo.name}-RETIRED"
    files, dlinks = _scan(wt)
    links = set(dlinks)
    if mode == "quarantine":
        return _quarantine(root, wl, name, wt, retired / f"{stamp}-{name}", files, links, execute, br, out)
    tracked = set(g.run("ls-files", "-z").out.split("\0"))
    kept = sorted(rel for rel, _ in files if rel != ".git" and rel not in tracked and not _regenerable(rel, links))
    freed = sum(n for _, n in files)
    kdir = retired / f"{stamp}-{name}-kept"
    out.append(f"  {len(kept)} untracked/ignored file(s) outside the regenerable set" + (f" kept -> {kdir}" if kept else "")
               + f"; {len(links)} link(s) unlinked first ({', '.join(sorted(links)) or 'none'})")
    out.append(f"  delete {wt} ({freed} bytes): git worktree remove --force + prune; branch {br} stays")
    if not execute:
        return {"ok": True, "code": 0, "out": out + ["DRY RUN — nothing deleted"], "mode": mode, "bytes": freed, "kept": kept}
    for rel in kept:                     # the value guard: copied and checked BEFORE anything is removed
        src, dst = wt / rel, kdir / rel
        if _is_link(src) and not src.exists():      # a dangling file link holds nothing
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if _sha256(src) != _sha256(dst):
            return {"ok": False, "code": 1, "out": out + [f"FAIL: kept copy of {rel} does not match; nothing deleted"]}
    if kept:
        (kdir / "KEPT.txt").write_text("\n".join(kept) + "\n", encoding="utf-8")
    for rel in sorted(links, key=len, reverse=True):
        _unlink_link(wt / rel)
    if _scan(wt)[1]:
        return {"ok": False, "code": 1, "out": out + ["FAIL: a link survived the unlink pass; nothing deleted"]}
    r = mg.run("worktree", "remove", "--force", str(wt))
    if wt.exists():                      # `Permission denied` on this Windows setup: finish link-safely ourselves
        out.append(f"  git worktree remove left the tree ({r.out[-160:]}); removing it link-safely")
        _rmtree_nolinks(wt)
    mg.run("worktree", "prune")
    listed = [l[9:] for l in mg.run("worktree", "list", "--porcelain").out.splitlines() if l.startswith("worktree ")]
    if wt.exists() or any(Path(l).resolve() == wt.resolve() for l in listed):
        return {"ok": False, "code": 1, "out": out + [f"FAIL: {wt} still exists or is still listed by git worktree list"]}
    if br != "HEAD" and not mg.ok("rev-parse", "--verify", "--quiet", f"refs/heads/{br}"):
        out.append(f"  note: no local branch {br} (it may live only on origin)")
    out.append(f"  OK: deleted {wt}, {freed} bytes freed; branch {br} intact" + (f"; {len(kept)} file(s) kept in {kdir}" if kept else ""))
    _log(root).append("system", name="worktree_deleted", actor="steward-watch", task=name if status else None,
                      note=f"{name} ({status}): deleted {wt}, {freed} bytes freed" + (f"; {len(kept)} file(s) kept -> {kdir}" if kept else ""),
                      data={"path": str(wt), "bytes": freed, "kept_dir": str(kdir) if kept else None,
                            "kept": kept[:KEPT_LISTED], "kept_count": len(kept), "links": sorted(links)})
    return {"ok": True, "code": 0, "out": out, "mode": mode, "bytes": freed, "kept": kept}


def _quarantine(root: Path, wl: Workload, name: str, wt: Path, dest: Path, files: list, links: set, execute: bool,
                br: str, out: list[str]) -> dict:
    """The move into <repo>-RETIRED/<stamp>-<name>/worktree, .lake/build removed first (regenerable, ~600 MB)."""
    mg = Git(wl.repo)
    build = wt / ".lake" / "build"
    keep = [rel for rel, _ in files if not (rel + "/").startswith(".lake/build/")]
    out.append(f"  {len(keep)} file(s) travel with the tree, .lake/build {'dropped' if build.exists() else 'absent'}; quarantine -> {dest / 'worktree'}")
    if not execute:
        return {"ok": True, "code": 0, "out": out + ["DRY RUN — nothing moved"], "mode": "quarantine"}
    if build.exists() or _is_link(build):
        _rmtree_nolinks(build)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(wt), str(dest / "worktree"))
    mg.run("worktree", "prune")
    meta = wl.repo / ".git" / "worktrees" / name
    if meta.exists():
        shutil.move(str(meta), str(dest / "git-worktree-meta"))
    n = len(_scan(dest / "worktree")[0])
    if n == 0 or n < len(keep):          # verify recoverability, not disappearance
        return {"ok": False, "code": 1, "out": out + [f"FAIL: quarantine holds {n} of {len(keep)} file(s) — stop and investigate"]}
    out.append(f"  OK: {n} file(s) readable in quarantine; branch {br} intact at {mg.run('rev-parse', '--short', br).out}")
    _log(root).append("system", name="worktree_retired", actor="steward-watch", note=f"{name} -> {dest}")
    return {"ok": True, "code": 0, "out": out, "mode": "quarantine"}


def worktrees_gc(root: Path, dry_run: bool = True, wl_name: str | None = None) -> list[dict]:
    """`scr worktrees gc` (T-0321): every worktree of every workload (or one) with its task status and size; without
    dry_run, retire() each whose task is merged / closed / rejected (delete, value guard and all) and drop the
    .lake/build of every tree already under <repo>-RETIRED/. Nothing else in the quarantine is touched."""
    store = Store(root)
    tasks = store.load_all()
    names = [wl_name] if wl_name else list(store.config("workloads") or {})
    rows: list[dict] = []
    seen: set[Path] = set()
    for wname in names:
        wl = workload(root, store, wname)
        if wl.wt_root.is_dir():
            for d in sorted(wl.wt_root.iterdir()):
                if not d.is_dir():
                    continue
                t = tasks.get(d.name)
                status = t.status if t else None
                link = _is_link(d)
                row = {"workload": wl.name, "worktree": d.name, "path": str(d), "status": status,
                       "bytes": 0 if link else tree_bytes(d), "action": "keep" if link or status not in DELETE_STATUSES else "delete",
                       "ok": True, "out": []}
                if not dry_run and row["action"] == "delete":
                    try:
                        r = retire(root, d.name, execute=True, wl_name=wl.name)
                    except RepoError as e:
                        r = {"ok": False, "code": "error", "out": [str(e)]}
                    row["ok"], row["out"] = r["ok"], r["out"]
                    row["action"] = "deleted" if r["ok"] else f"refused ({r['code']})"
                rows.append(row)
        retired = (wl.repo.parent / f"{wl.repo.name}-RETIRED").resolve()
        if retired in seen or not retired.is_dir():
            continue
        seen.add(retired)
        for q in sorted(retired.iterdir()):
            for b in (q / "worktree" / ".lake" / "build", q / ".lake" / "build"):
                if not (b.exists() or _is_link(b)):
                    continue
                row = {"workload": wl.name, "worktree": f"{retired.name}/{q.name}", "path": str(b), "status": "retired",
                       "bytes": 0 if _is_link(b) else tree_bytes(b), "action": "drop .lake/build", "ok": True, "out": []}
                if not dry_run:
                    _rmtree_nolinks(b)
                    row["action"] = "dropped .lake/build"
                rows.append(row)
    if not dry_run:
        freed = sum(r["bytes"] for r in rows if r["action"] in ("deleted", "dropped .lake/build"))
        _log(root).append("system", name="worktrees_gc", actor="steward-watch",
                          note=f"{sum(r['action'] == 'deleted' for r in rows)} worktree(s) deleted, "
                               f"{sum(r['action'] == 'dropped .lake/build' for r in rows)} quarantined .lake/build dropped, {freed} bytes freed")
    return rows


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
    Each task is routed by land_any (T-0119): a scriptorium task is refused (tooling lands as a PR in the scr repo)."""
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

