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
import re
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
        if not self.cwd.is_dir():       # a missing workload repo or worktree: Windows raises NotADirectoryError (267) here
            return Result(128, f"not a directory: {self.cwd}")
        try:
            r = subprocess.run(self._base() + list(args), cwd=self.cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except FileNotFoundError:
            return Result(127, "git not found on PATH")
        except OSError as e:            # NotADirectoryError and friends: a failed git call, never a crashed tick
            return Result(128, f"git could not start in {self.cwd}: {e}")
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
        """Changed/untracked paths (porcelain), noise filtered, exact: read NUL-separated (`-z`) straight from
        the process, not through `run`, which strips the output — a modified tracked file on the FIRST line
        (` M QuantumInsertion/A.lean`) lost its status column and came back as `uantumInsertion/A.lean`
        (T-0082's report, item 4). A staged rename/copy lists both its new and its old path."""
        try:
            r = subprocess.run(self._base() + ["status", "--porcelain", "-z", "--untracked-files=all"], cwd=self.cwd,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        except (OSError, subprocess.SubprocessError):
            return []
        if r.returncode != 0:
            return []
        rows: list[str] = []
        fields = r.stdout.split("\0")
        i = 0
        while i < len(fields):
            entry = fields[i]
            i += 1
            if len(entry) < 4:
                continue
            paths = [entry[3:]]
            if entry[0] in "RC" and i < len(fields):    # `R  new\0old`: the original path is the next field
                paths.append(fields[i])
                i += 1
            for path in paths:
                path = path.replace("\\", "/")
                if not path or (ignore_noise and any(n in "/" + path for n in NOISE_RE)):
                    continue
                if path not in rows:
                    rows.append(path)
        return rows

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def remote_contains(self, remote_branch: str, branch: str) -> bool:
        r = self.run("branch", "-r", "--contains", branch)
        return r.ok and any(l.strip() == remote_branch for l in r.out.splitlines())

    def branch_exists(self, name: str) -> bool:
        """rc-based and exact: `run` folds stderr into `.out`, so the old `bool(branch --list <name>)` read git's
        `warning: ignoring ref with broken name refs/heads/main (1)` as a hit, and every spawn of a new branch
        ran `worktree add` without -b -> `invalid reference: task/<id>` (2026-09-25 06:08 on, T-0244)."""
        return self.ok("show-ref", "--verify", "--quiet", f"refs/heads/{name}")

    def broken_refs(self) -> list[str]:
        """The refs under refs/heads that git ignores with a warning (a broken name such as `main (1)`, left by a
        file-sync conflict copy, or unreadable contents), e.g. ["refs/heads/main (1)"]. Git only reports them."""
        r = self.run("for-each-ref", "--format=%(refname)", "refs/heads")
        found: list[str] = []
        for line in r.out.splitlines():
            m = re.match(r"warning: ignoring (?:ref with broken name|broken ref) (refs/heads/.+?)\s*$", line)
            if m and m.group(1) not in found:
                found.append(m.group(1))
        return found

    def remote_url(self) -> str:
        return self.run("remote", "get-url", "origin").out

    # ---- mutations (never destructive) ----------------------------------------
    def fetch(self) -> Result:
        return self.run("fetch", "--quiet", "origin", timeout=600)

    def commit_all(self, message: str, exclude: tuple[str, ...] = NOISE) -> str | None:
        """Stage everything (minus noise), commit if anything is staged. Returns the sha or None."""
        # Add WITHOUT exclude pathspecs, then unstage the noise: on git 2.31 any `:(exclude)` pathspec next to
        # `.` makes `git add -A` refuse with "paths are ignored: .lake" when an ignored directory exists
        # (T-0015's worktree, every tick from 2026-09-16 20:58 — T-0041); plain `add -A -- .` just skips it.
        add = self.run("add", "-A", "--", ".")
        if not add.ok:
            raise GitError(f"git add: {add.out}")
        if exclude:
            noise = [e.replace(":(exclude)", ":(glob)") for e in exclude]
            self.run("reset", "-q", "--", *noise, *[n.replace(":(glob)", ":(glob)**/") for n in noise if not n.startswith(":(glob)**/")])
        return self._commit_staged(message)

    def commit_paths(self, message: str, paths: list[str]) -> str | None:
        """Stage exactly `paths` — their additions, modifications and deletions, nothing else — and commit
        if anything is staged. Returns the sha or None. The index is reset to HEAD first, so a stray `git add`
        of some other path never rides along; the working tree is untouched. The pathspec goes through a
        NUL-separated file inside .git (`--pathspec-from-file`, every entry `:(literal)`): no command-line
        length limit, and no glob magic in a scriber's file names. T-0079: `checkpoint_commit` and `land`
        stage the in-scope and the out-of-scope parts of a worktree as two commits with this."""
        paths = [p.replace("\\", "/") for p in dict.fromkeys(paths) if p and p.strip()]
        if not paths:
            return None
        reset = self.run("reset", "-q")
        if not reset.ok and "unknown revision" not in reset.out:   # an unborn HEAD has nothing to reset to
            raise GitError(f"git reset: {reset.out}")
        spec_file = Path(self.git_dir()) / "SCR_PATHSPEC"          # inside .git: never part of the tree
        spec_file.write_text("\0".join(":(literal)" + p for p in paths) + "\0", encoding="utf-8")
        try:
            add = self.run("add", "-A", "--pathspec-from-file=" + str(spec_file), "--pathspec-file-nul")
        finally:
            try:
                spec_file.unlink()
            except OSError:
                pass
        if not add.ok:
            raise GitError(f"git add: {add.out}")
        return self._commit_staged(message)

    def _commit_staged(self, message: str) -> str | None:
        """Commit the index if it differs from HEAD (the message via a file inside .git). Sha or None."""
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
    except OSError as e:
        return Result(128, f"gh could not start in {cwd}: {e}")
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
