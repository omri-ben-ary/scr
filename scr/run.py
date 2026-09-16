"""Process helpers shared by the watcher, the launcher and the UI: run scr as a subprocess
(so each call journals itself), and start Claude Code sessions detached."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()


def scr_exe(root: Path) -> list[str]:
    exe = root / ".venv" / "Scripts" / "scr.exe"
    if exe.exists():
        return [str(exe)]
    exe = root / ".venv" / "bin" / "scr"
    if exe.exists():
        return [str(exe)]
    return [sys.executable, "-m", "scr"]


def service_argv(root: Path) -> list[str]:
    """How to run scr for a long-lived background service: on Windows through pythonw (no console
    window at all); elsewhere the normal scr."""
    if os.name == "nt":
        pyw = root / ".venv" / "Scripts" / "pythonw.exe"
        if pyw.exists():
            return [str(pyw), "-m", "scr"]
    return scr_exe(root)


def _env() -> dict:
    """When scr runs as `python -m scr` (no venv), the package dir must be importable from any cwd."""
    env = dict(os.environ)
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def scr_cmd(root: Path, actor: str, *args: str, timeout: int = 900) -> tuple[int, str]:
    cmd = scr_exe(root) + ["--root", str(root), "--actor", actor, *args]
    with _LOCK:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=root, timeout=timeout, env=_env())
        except subprocess.TimeoutExpired:
            return 124, f"scr {' '.join(args)} timed out"
    return r.returncode, (r.stdout + r.stderr).strip()


def claude_bin() -> str | None:
    """Prefer a real executable: Popen cannot start a .cmd shim directly (seen with loopy)."""
    return shutil.which("claude.exe") or shutil.which("claude") or shutil.which("claude.cmd")


def claude_argv(*args: str) -> list[str] | None:
    exe = claude_bin()
    if not exe:
        return None
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *args]
    return [exe, *args]


def start_claude(cwd: Path, brief_file: Path, transcript: Path, permission_mode: str = "acceptEdits",
                 model: str | None = None, extra: list[str] | None = None) -> int | None:
    """Detached `claude -p` reading the brief from stdin; stdout/stderr to the transcript. Returns the pid."""
    args = claude_argv("-p", "--permission-mode", permission_mode, "--output-format", "text")
    if not args:
        return None
    if model:
        args += ["--model", model]
    args += extra or []
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]  (+ CREATE_NO_WINDOW from scr/__init__)
    p = subprocess.Popen(args, cwd=cwd, stdin=brief_file.open("rb"), stdout=transcript.open("ab"),
                         stderr=transcript.with_suffix(transcript.suffix + ".err").open("ab"),
                         creationflags=flags, start_new_session=(os.name != "nt"))
    return p.pid


def start_detached(args: list[str], cwd: Path, log: Path | None = None) -> int:
    """Start a long-running helper (the watcher, the UI) detached from this process."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]  (+ CREATE_NO_WINDOW from scr/__init__)
    out = log.open("ab") if log else subprocess.DEVNULL
    p = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         creationflags=flags, start_new_session=(os.name != "nt"))
    return p.pid


def pids_matching(pattern: str) -> list[int]:
    """PIDs whose command line matches the regex `pattern` (Windows via CIM, POSIX via ps)."""
    import re
    pids: list[int] = []
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '" + pattern.replace("'", "''") +
                  "' -and $_.CommandLine -notmatch 'Get-CimInstance' } | ForEach-Object { $_.ProcessId }")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
            pids = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
        else:
            r = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True)
            rx = re.compile(pattern)
            for line in r.stdout.splitlines()[1:]:
                pid, _, cmd = line.strip().partition(" ")
                if pid.isdigit() and rx.search(cmd):
                    pids.append(int(pid))
    except Exception:
        pass
    return [p for p in pids if p != os.getpid()]


def kill(pid: int) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, 9)
        return True
    except Exception:
        return False
