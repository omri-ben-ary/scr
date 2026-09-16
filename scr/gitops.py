"""Git and GitHub operations used by the journal, the watcher and the landing commands.

Everything here is a thin, testable wrapper around the git CLI. Two rules:
  * never delete or force: no `rm -rf`, no `push --force`, no `worktree remove`;
  * a stale lock never stops the operation: an EMPTY *.lock file older than STALE_LOCK_S
    under the repo's git dir is removed (and reported) before retrying once. Crashed git
    processes leave these behind (seen 2026-09-07 and 2026-09-15: index.lock, refs/heads/qi23.lock,
    worktrees/qi-mission-23/HEAD.lock), and a lock a live process holds is never empty for a minute.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

STALE_LOCK_S = 60
NOISE = (":(exclude)build.log", ":(exclude)**/__pycache__/**", ":(exclude)**/.pytest_cache/**",
         ":(exclude).lake/**", ":(exclude).loopy/**", ":(exclude)**/*.pyc", ":(exclude)*.lock",
         # the entries THEMSELVES, not only their contents: a worktree's .lake is a junction, which git sees as
         # a single ignored entry named ".lake", and `git add -A -- .` then refused with "paths are ignored"
         # (T-0015, every tick from 2026-09-16 20:58 — T-0041)
         ":(exclude).lake", ":(exclude).loopy")
NOISE_RE = ("/.lake/", "__pycache__", ".pytest_cache", "build.log", "/.loopy/", ".lake/")


class GitError(RuntimeError):
    pass


@dataclass
class Result:
    rc: int
    out: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class Git:
    """git bound to one working directory (a repo or a worktree)."""

    def __init__(self, cwd: Path, identity: tuple[str, str] | None = ("scriptorium", "scriptorium@local")):
        self.cwd = Path(cwd)
        self.identity = identity

    # ---- plumbing --------------------------------------------------------------
    def _base(self) -> list[str]:
        cmd = ["git"]
        if self.identity and not self.has_identity():
            cmd += ["-c", f"user.name={self.identity[0]}", "-c", f"user.email={self.identity[1]}"]
        return cmd

    _ident_cache: dict[str, bool] = {}

    def has_identity(self) -> bool:
        key = str(self.cwd)
        if key not in Git._ident_cache:
            r = subprocess.run(["git", "config", "user.email"], cwd=self.cwd, capture_output=True, text=True)
            Git._ident_cache[key] = bool(r.stdout.strip())
        return Git._ident_cache[key]

    def run(self, *args: str, timeout: int = 300, retry_on_lock: bool = True) -> Result:
        try:
            r = subprocess.run(self._base() + list(args), cwd=self.cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except FileNotFoundError:
            return Result(127, "git not found on PATH")
        except subprocess.TimeoutExpired:
            return Result(124, f"git {' '.join(args)} timed out after {timeout}s")
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0 and retry_on_lock and ".lock" in out and ("File exists" in out or "Unable to create" in out):
            removed = self.clear_stale_locks()
            if removed:
                r2 = self.run(*args, timeout=timeout, retry_on_lock=False)
                return Result(r2.rc, f"(removed stale lock(s): {', '.join(removed)})\n{r2.out}")
        return Result(r.returncode, out)

    def out(self, *args: str) -> str:
        r = self.run(*args)
        if not r.ok:
            raise GitError(f"git {' '.join(args)}: {r.out}")
        return r.out

    def ok(self, *args: str) -> bool:
        return self.run(*args).ok

    # ---- queries ---------------------------------------------------------------
    def is_repo(self) -> bool:
        return self.ok("rev-parse", "--git-dir")

    def git_dir(self) -> Path:
        return (self.cwd / self.out("rev-parse", "--git-dir")).resolve()

    def common_dir(self) -> Path:
        return (self.cwd / self.out("rev-parse", "--git-common-dir")).resolve()

    def head(self, short: bool = False) -> str | None:
        r = self.run("rev-parse", *(["--short"] if short else []), "HEAD")
        return r.out if r.ok else None

    def branch(self) -> str:
        return self.out("rev-parse", "--abbrev-ref", "HEAD")

    def dirty(self, ignore_noise: bool = True) -> list[str]:
        """Changed/untracked paths (porcelain), noise filtered."""
        r = self.run("status", "--porcelain", "--untracked-files=all")
        if not r.ok:
            return []
        rows = []
        for line in r.out.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip().strip('"').replace("\\", "/")
            if ignore_noise and any(n in "/" + path for n in NOISE_RE):
                continue
            rows.append(path)
        return rows

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def remote_contains(self, remote_branch: str, branch: str) -> bool:
        r = self.run("branch", "-r", "--contains", branch)
        return r.ok and any(l.strip() == remote_branch for l in r.out.splitlines())

    def branch_exists(self, name: str) -> bool:
        return bool(self.run("branch", "--list", name).out.strip())

    def remote_url(self) -> str:
        return self.run("remote", "get-url", "origin").out

    # ---- mutations (never destructive) ----------------------------------------
    def fetch(self) -> Result:
        return self.run("fetch", "--quiet", "origin", timeout=600)

    def commit_all(self, message: str, exclude: tuple[str, ...] = NOISE) -> str | None:
        """Stage everything (minus noise), commit if anything is staged. Returns the sha or None."""
        add = self.run("add", "-A", "--", ".", *exclude)
        if not add.ok:
            raise GitError(f"git add: {add.out}")
        if not self.run("diff", "--cached", "--quiet").ok:      # rc 1 = something staged
            msg_file = Path(self.git_dir()) / "SCR_COMMIT_MSG"   # inside .git: never part of the tree
            msg_file.write_text(message, encoding="utf-8")
            r = self.run("commit", "-q", "-F", str(msg_file))
            try:
                msg_file.unlink()
            except OSError:
                pass
            if not r.ok:
                raise GitError(f"git commit: {r.out}")
            return self.head(short=True)
        return None

    def is_worktree(self) -> bool:
        return self.git_dir() != self.common_dir()

    def push(self, branch: str, set_upstream: bool = True) -> Result:
        return self.run("push", *(["-u"] if set_upstream else []), "origin", branch, timeout=600)

    def worktree_add(self, path: Path, branch: str, base: str | None) -> Result:
        if self.branch_exists(branch):
            return self.run("worktree", "add", str(path), branch)
        return self.run("worktree", "add", "-b", branch, str(path), base or "HEAD")

    def clear_stale_locks(self, max_age_s: int = STALE_LOCK_S) -> list[str]:
        """Remove EMPTY *.lock files older than max_age_s under the common git dir (covers worktrees)."""
        removed: list[str] = []
        try:
            gd = self.common_dir()
        except GitError:
            return removed
        now = time.time()
        for lock in gd.rglob("*.lock"):
            if "_to_delete" in lock.parts or not lock.is_file():
                continue
            try:
                st = lock.stat()
                if st.st_size == 0 and now - st.st_mtime > max_age_s:
                    lock.unlink()
                    removed.append(str(lock.relative_to(gd)))
            except OSError:
                pass
        return removed


# ---- GitHub CLI ------------------------------------------------------------------
def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh(*args: str, cwd: Path | None = None, timeout: int = 300) -> Result:
    try:
        r = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return Result(r.returncode, (r.stdout + r.stderr).strip())
    except FileNotFoundError:
        return Result(127, "gh not on PATH")
    except subprocess.TimeoutExpired:
        return Result(124, "gh timed out")


def pr_url_for(branch: str, cwd: Path) -> str | None:
    r = gh("pr", "list", "--head", branch, "--json", "url", "--jq", ".[0].url", cwd=cwd)
    return r.out.strip() if r.ok and r.out.strip().startswith("http") else None


def pr_create(base: str, head: str, title: str, body_file: Path, cwd: Path, draft: bool = False) -> str | None:
    args = ["pr", "create", "--base", base, "--head", head, "--title", title, "--body-file", str(body_file)]
    if draft:
        args.append("--draft")
    r = gh(*args, cwd=cwd)
    for line in reversed(r.out.splitlines()):
        if "/pull/" in line:
            return line.strip()
    return None


def pr_merge(url: str, cwd: Path) -> Result:
    return gh("pr", "merge", url, "--merge", "--delete-branch=false", cwd=cwd)
