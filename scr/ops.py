"""Workload-repo operations (quantum-insertion): worktrees, checkpoint commits, landing, sweeping,
retiring. Python ports of the former hooks/*.ps1 and retire_worktree.sh so they run and are
tested on any OS. Invariants: nothing is deleted (retire = quarantine by move), never force-push,
never touch the trusted branch.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .events import EventLog, now_iso
from .gitops import Git, GitError, NOISE, gh_available, pr_create, pr_merge, pr_url_for
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


def spawn(root: Path, tids: list[str] | None = None, lake: bool | None = None, wl_name: str | None = None) -> list[str]:
    """One worktree per dispatched/running task (or the ids given), branch task/<id> from origin/<main>."""
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
            out.append(f"  based on {base} @ {Git(wl.repo).run('rev-parse', '--short', base).out}")
        else:
            out.append("  exists, reusing")
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
        tokens = 0
        mt = re.search(r"(?i)(\d[\d,.]*)\s*(k|m)?\s*tokens", report)
        if mt:
            n = float(mt.group(1).replace(",", "")); n *= {"k": 1e3, "m": 1e6}.get((mt.group(2) or "").lower(), 1); tokens = int(n)
        from . import lifecycle
        t = store.get(tid)
        old = t.status
        lifecycle.apply(t.rec, "merged"); t.rec["tokens_used"] = int(t.rec.get("tokens_used", 0)) + tokens; t.save()
        log.append("state", task=tid, **{"from": old}, to="merged", actor=f"steward:{tid}", tokens=tokens or None, note=f"auto-land: {url}")
        out.append(f"  merged -> {wl.main}; {tid} marked merged")
        for d in lifecycle.cascade(store, log, tid, "merged", actor=f"steward:{tid}"):
            out.append(f"  unblocked {d} -> ready")
    return {"ok": True, "url": url, "out": out}


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

