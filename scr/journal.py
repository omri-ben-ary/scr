"""Journal mode: the scriptorium repo commits itself.

Every state-changing `scr` command ends with `journal(...)`: refresh the derived views, then
commit whatever is dirty with the actor and the command in the message. Nobody types `git` in
this folder; `git log` is the audit trail and `scr history` reads the snapshot chain from it.

Serialization: the UI, the watcher and several scribers call scr concurrently, so commits go
through a lock file (.scr/journal.lock). A lock older than LOCK_STALE_S is taken over.
Disable with SCR_NO_JOURNAL=1 (tests, nested calls) or `scr --no-journal ...`.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .gitops import Git, GitError

LOCK_STALE_S = 180
LOCK_WAIT_S = 60
# journal working dir; gitignored
SCR_DIR = ".scr"


def enabled() -> bool:
    return os.environ.get("SCR_NO_JOURNAL", "") not in ("1", "true", "yes")


class _Lock:
    def __init__(self, root: Path):
        self.path = root / SCR_DIR / "journal.lock"
        self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        gi = self.path.parent / ".gitignore"          # .scr/ ignores itself, whatever the root .gitignore says
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        deadline = time.time() + LOCK_WAIT_S
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f"{os.getpid()} {time.time():.0f}\n".encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_S:
                        self.path.unlink()          # stale: a crashed holder
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise TimeoutError(f"journal lock busy for {LOCK_WAIT_S}s: {self.path}")
                time.sleep(0.25)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except OSError:
            pass


def journal(root: Path, actor: str, summary: str, push: bool = False, force_refresh: bool = True) -> str | None:
    """Refresh derived views, then commit the scriptorium tree if dirty. Returns the short sha or None.

    Never raises on git trouble: the state on disk is the truth, the commit is the record of it;
    a failed commit is reported as a `journal_failed` line in .scr/journal.log and retried by the
    next journal call (which will include this change too).
    """
    if not enabled():
        return None
    g = Git(root)
    if not g.is_repo():
        return None
    try:
        with _Lock(root):
            if force_refresh:
                from .cli import refresh       # local import: cli imports this module
                refresh(root, quiet=True)
            if not g.dirty():
                return None
            changed = g.dirty()
            head = (root / "state" / "STATE.json")
            root_hash = ""
            try:
                import json
                root_hash = json.loads(head.read_text(encoding="utf-8")).get("state_root", "")[:12]
            except Exception:
                pass
            msg = f"[{actor}] {summary.strip()[:160]}\n\nstate_root {root_hash}; {len(changed)} file(s):\n" + "\n".join(f"  {c}" for c in changed[:40])
            sha = g.commit_all(msg)
            if sha and push:
                r = g.push(g.branch(), set_upstream=True)
                if not r.ok:
                    _log(root, f"push failed: {r.out[-300:]}")
            return sha
    except (GitError, TimeoutError, OSError) as e:
        _log(root, f"journal_failed [{actor}] {summary}: {e}")
        return None


def _log(root: Path, line: str) -> None:
    try:
        p = root / SCR_DIR / "journal.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def last_failure(root: Path) -> str | None:
    p = root / SCR_DIR / "journal.log"
    if not p.exists():
        return None
    lines = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines() if "journal_failed" in l or "push failed" in l]
    return lines[-1] if lines else None
