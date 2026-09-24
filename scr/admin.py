"""Administration: apply an update bundle, migrate a checkout to journal mode, and start/stop/register
the two long-running services (the watcher and the UI). Python so the same code runs on Windows
and on the cloud mirror; the only OS-specific parts are the scheduled-task registration and
process discovery, both isolated here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

from .events import EventLog
from .gitops import Git
from .run import kill, pids_matching, scr_exe, service_argv, start_detached

UPDATABLE = ("scr", "ui", "hooks", "agents", "templates", "docs", "README.md", ".gitignore", "pyproject.toml", "tests")
GITIGNORE_LINES = [
    "__pycache__/", "*.egg-info/", ".pytest_cache/", ".venv/", ".scr/",
    "reports/chat/", "reports/steward_watch.log", "reports/steward_watch.state.txt", "reports/ui.acks.txt",
    "reports/concierge-*.txt", "reports/intake-*.txt", "reports/ui.log", "reports/watch.log",
    "tasks/*/session-*.txt", "tasks/*/session-*.txt.err", "tasks/*/brief.txt", "tasks/*/review-brief.txt",
    "tasks/*/review-session-*.txt", "tasks/*/review-session-*.txt.err", "tasks/*/commit-msg.txt", "tasks/*/pr-body.md",
    "*.zip", "_to_delete*",
]


# ---- update --------------------------------------------------------------------------
def update(root: Path, src: Path, actor: str = "omri") -> list[str]:
    """Copy a bundle (zip or directory) over the checkout — only paths under UPDATABLE — then migrate.
    The running UI notices its files changed and restarts itself; the watcher restarts at its next tick."""
    out: list[str] = []
    src = Path(src)
    if src.is_file() and src.suffix == ".zip":
        tmp = root / ".scr" / f"update-{datetime.now():%Y%m%d-%H%M%S}"
        tmp.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp)
        # a zip may wrap everything in one top-level folder
        entries = [p for p in tmp.iterdir() if p.name != "__MACOSX"]
        src = entries[0] if len(entries) == 1 and entries[0].is_dir() and not (tmp / "scr").exists() else tmp
    if not src.is_dir():
        raise FileNotFoundError(f"no such bundle: {src}")
    copied = 0
    for top in UPDATABLE:
        s = src / top
        if not s.exists():
            continue
        if s.is_file():
            shutil.copy2(s, root / top); copied += 1; continue
        for f in s.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                dst = root / f.relative_to(src)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst); copied += 1
    out.append(f"copied {copied} file(s) from {src.name}")
    out += migrate(root)
    EventLog(root / "events" / "events.jsonl").append("system", name="update", actor=actor, note=f"{src.name}: {copied} file(s)")
    return out


# ---- migrate -------------------------------------------------------------------------
def migrate(root: Path) -> list[str]:
    """Idempotent: bring a checkout to journal mode. Safe to run any number of times."""
    out: list[str] = []
    (root / ".scr").mkdir(exist_ok=True)
    gi = root / ".gitignore"
    have = set(gi.read_text(encoding="utf-8").splitlines()) if gi.exists() else set()
    missing = [l for l in GITIGNORE_LINES if l not in have]
    if missing:
        with gi.open("a", encoding="utf-8") as f:
            f.write("\n# journal mode working files\n" + "\n".join(missing) + "\n")
        out.append(f".gitignore: added {len(missing)} pattern(s)")
    hook = root / ".git" / "hooks" / "pre-commit"
    if hook.exists() and "scriptorium pre-commit" in hook.read_text(encoding="utf-8", errors="replace"):
        hook.rename(hook.with_name("pre-commit.disabled-by-journal-mode"))
        out.append("pre-commit hook retired (journal mode verifies before it writes, not at commit time)")
    g = Git(root)
    if g.is_repo():
        removed = g.clear_stale_locks()
        if removed:
            out.append("removed stale git locks: " + ", ".join(removed))
        # git on Windows: no CRLF chatter, and identity for the journal commits if none is set
        g.run("config", "core.safecrlf", "false")
        if not g.has_identity():
            g.run("config", "user.name", "scriptorium"); g.run("config", "user.email", "scriptorium@local")
            out.append("git identity set for journal commits (scriptorium@local)")
    # working files that the old UI tracked must leave the index (they are ignored now)
    for pat in ("reports/chat", "reports/ui.acks.txt", "reports/steward_watch.log", "reports/steward_watch.state.txt"):
        if (root / pat).exists() and g.is_repo():
            g.run("rm", "-r", "-q", "--cached", "--ignore-unmatch", pat)
    if g.is_repo():
        out += ensure_main(root, g)
    # the PowerShell hooks that became scr subcommands move aside (never deleted)
    retired = root / "hooks" / "retired"
    for name in ("steward_watch.ps1", "land_task.ps1", "launch_dispatched.ps1", "spawn_worktrees.ps1", "sweep_worktrees.ps1", "intake_run.ps1", "retire_worktree.sh"):
        src = root / "hooks" / name
        if src.exists():
            retired.mkdir(exist_ok=True)
            dst = retired / name
            if dst.exists():
                dst = retired / f"{name}.{datetime.now():%Y%m%d%H%M%S}"
            shutil.move(str(src), str(dst))
            out.append(f"hooks/{name} -> hooks/retired/ (now `scr` subcommand)")
    out.append("migrate: ok")
    return out


def ensure_main(root: Path, g: Git, main: str = "main", push: bool = False) -> list[str]:
    """Work on `main`, not a side branch: if HEAD is elsewhere and main is an ancestor, fast-forward
    main to HEAD and check it out; otherwise merge the side branch into main. Never discards commits.
    With push=True, `main` then goes to origin (also when nothing had to move), so origin/<main> carries
    every journal commit: `scr ensure-main --push` (T-0061; on 2026-09-19 main sat on c55540f while the
    checkout journaled 1075 commits on worktree-retirement). The push is never forced."""
    out = _fold_main(g, main)
    if push and g.branch() == main:
        r = g.push(main)
        out.append(f"pushed {main} to origin ({g.head(short=True)})" if r.ok else f"push of {main} failed: {r.out[-200:]}")
    return out


def _fold_main(g: Git, main: str) -> list[str]:
    cur = g.branch()
    if cur == main or cur == "HEAD":
        return []
    if g.dirty():
        return [f"on branch {cur}: tree dirty, not switching to {main} this time (the next journal commit cleans it)"]
    if not g.branch_exists(main):
        r = g.run("checkout", "-q", "-b", main)
        return [f"created {main} from {cur}" if r.ok else f"could not create {main}: {r.out[-200:]}"]
    if g.is_ancestor(main, "HEAD"):
        r1 = g.run("branch", "-f", main, "HEAD")
        r2 = g.run("checkout", "-q", main)
        if r1.ok and r2.ok:
            g.run("branch", "-u", f"origin/{main}", main)
            return [f"{main} fast-forwarded to {cur} and checked out ({g.head(short=True)})"]
        return [f"could not switch to {main}: {(r1.out + r2.out)[-200:]}"]
    r = g.run("checkout", "-q", main)
    if not r.ok:
        return [f"could not check out {main}: {r.out[-200:]}"]
    m = g.run("merge", "-q", "--no-edit", cur)
    if m.ok:
        return [f"merged {cur} into {main} ({g.head(short=True)})"]
    g.run("merge", "--abort")
    g.run("checkout", "-q", cur)
    return [f"{cur} and {main} have diverged and the merge conflicts — staying on {cur}; tell Claude"]


def desktop_shortcut(root: Path, port: int = 8765) -> list[str]:
    """Windows: Scriptorium.lnk on the Desktop (and in the root) that runs `scr up`."""
    if os.name != "nt":
        return []
    argv = service_argv(root)
    exe, pre = argv[0], " ".join(argv[1:])
    targets = [root / "Scriptorium.lnk"]
    desk = Path.home() / "Desktop"
    if desk.exists():
        targets.append(desk / "Scriptorium.lnk")
    out = []
    for lnk in targets:
        if lnk.exists():
            continue
        ps = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); $s.TargetPath = '{exe}'; "
              f"$s.Arguments = '{pre} --root \"{root}\" up --port {port}'; $s.WorkingDirectory = '{root}'; "
              f"$s.WindowStyle = 7; $s.Description = 'Scriptorium: start the UI and the watcher'; $s.IconLocation = '%SystemRoot%\\System32\\shell32.dll,165'; $s.Save()")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        out.append(f"shortcut: {lnk}" if r.returncode == 0 else f"shortcut failed ({lnk}): {(r.stdout + r.stderr).strip()[-200:]}")
    return out


# ---- services ------------------------------------------------------------------------
# command lines look like: "<...>\\scr.exe" --root "<root>" ui --port 8765 --no-browser [--child]
UI_PATTERN = r"scr(\.exe)?\"?(\s.*)?\sui(\s|$)"
WATCH_PATTERN = r"scr(\.exe)?\"?(\s.*)?\swatch(\s|$)|steward_watch\.ps1"   # the second alternative is the retired PowerShell watcher


def _pattern(p: str) -> str:
    return p


STOP_FAILED = "FAILED"      # the prefix of a `stop` line whose kill was refused; read it only through stop_failed


def stop(root: Path, what: str) -> list[str]:
    pat = {"ui": UI_PATTERN, "watch": WATCH_PATTERN}[what]
    out = []
    for pid in pids_matching(_pattern(pat)):
        # T-0148: kill's return is truthful (T-0124); a refused stop must not read as made
        out.append(f"stopped {what} pid {pid}" if kill(pid) else
                   f"{STOP_FAILED} to stop {what} pid {pid} (it may still be running; stop it by hand)")
    return out or [f"no {what} running"]


def stop_failed(lines: list[str]) -> bool:
    """True when a `stop` line reports a process that survived (T-0156: shared by ui._quit and `scr up --stop`)."""
    return any(l.startswith(STOP_FAILED) for l in lines)


def start(root: Path, what: str, port: int = 8765) -> list[str]:
    pat = {"ui": UI_PATTERN, "watch": WATCH_PATTERN}[what]
    if pids_matching(_pattern(pat)):
        return [f"{what} already running"]
    (root / "reports").mkdir(exist_ok=True)
    if what == "ui":
        args = service_argv(root) + ["--root", str(root), "ui", "--port", str(port), "--no-browser"]
        log = root / "reports" / "ui.log"
    else:
        args = service_argv(root) + ["--root", str(root), "watch", "--unattended", "--quiet"]
        log = root / "reports" / "watch.log"
    pid = start_detached(args, root, log)
    time.sleep(1.5)
    return [f"started {what} (pid {pid}; log {log.name})"]


def register(root: Path, port: int = 8765) -> list[str]:
    """Windows: two logon tasks (Scriptorium\\UI, Scriptorium\\Watcher). Elsewhere: print what to add to cron."""
    exe = " ".join(f'"{a}"' if (" " in a or a.endswith(".exe")) else a for a in service_argv(root))
    ui = f'{exe} --root "{root}" ui --port {port} --no-browser'
    watch = f'{exe} --root "{root}" watch --unattended --quiet'
    if os.name != "nt":
        return ["not Windows; to run at login add to your session manager:", "  " + ui, "  " + watch]
    out = []
    for name, cmd in (("Scriptorium\\UI", ui), ("Scriptorium\\Watcher", watch)):
        r = subprocess.run(["schtasks", "/Create", "/F", "/TN", name, "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", cmd], capture_output=True, text=True)
        out.append(f"registered {name}" if r.returncode == 0 else f"schtasks failed for {name}: {(r.stdout + r.stderr).strip()[-200:]}")
    return out


def unregister(root: Path) -> list[str]:
    if os.name != "nt":
        return []
    out = []
    for name in ("Scriptorium\\UI", "Scriptorium\\Watcher"):
        r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], capture_output=True, text=True)
        if r.returncode == 0:
            out.append(f"logon task removed: {name}")
    return out


def up(root: Path, *, ui_only: bool = False, restart: bool = False, register_logon: bool = False, port: int = 8765, open_browser: bool = True) -> list[str]:
    """Start (or restart) the UI and the watcher. Nothing runs at logon unless register_logon: the
    Desktop shortcut is how Scriptorium starts, and Quit in the UI is how it stops."""
    out: list[str] = migrate(root)
    out += register(root, port) if register_logon else unregister(root)
    out += desktop_shortcut(root, port)
    if restart:
        out += stop(root, "ui")
        if not ui_only:
            out += stop(root, "watch")
        time.sleep(1)
    out += start(root, "ui", port)
    if not ui_only:
        out += start(root, "watch")
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    out.append(f"UI: {url}")
    return out
