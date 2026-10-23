"""Process helpers shared by the watcher, the launcher and the UI: run scr as a subprocess
(so each call journals itself), and start Claude Code sessions detached."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import uuid
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


def resolve_test_argv(cmd: str, cwd: Path) -> tuple[list[str], str | None]:
    """A workload `tests:` command as the argv to launch IN `cwd`, plus a one-line note when a missing
    `python*` interpreter was replaced by sys.executable (None otherwise). The one resolver behind
    scr.hotfix.run_tests (cwd = the checkout) (T-0255)."""
    cwd = Path(cwd)
    argv = shlex.split(cmd, posix=(os.name != "nt"))
    if os.name == "nt":
        argv = [a.strip('"') for a in argv]
    # Resolve the program against cwd, not the process cwd: `.venv/Scripts/python.exe` in workloads.yaml
    # is relative to the checkout, and on Windows CreateProcess does not resolve a relative forward-slash
    # program name even when cwd is set (T-0131: WinError 2 from any directory). An absolute, native path
    # launches everywhere; a bare name (`python`, `pytest`) stays a PATH lookup.
    note = None
    exe = Path(argv[0])
    if not exe.is_absolute() and (len(exe.parts) > 1 or not shutil.which(argv[0])):
        if (cwd / exe).exists():
            argv[0] = os.path.abspath(cwd / exe)         # absolute + native separators; abspath keeps a linked .venv under cwd (resolve would follow the junction)
        elif exe.name.lower().startswith("python"):      # a missing python.exe: this interpreter (same venv on a linked worktree)
            note = f"note: tests: interpreter {argv[0]} not found under {cwd}; using {sys.executable}"
            argv[0] = sys.executable
    return argv, note


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


DEFAULT_PERMISSION_MODE = "auto"        # the one default; the reason is in permission_mode_for's docstring
PERMISSION_MODE_KEY = "claude_permission_mode"   # top-level key in config/limits.yaml that overrides it


def permission_mode_for(root: Path | None) -> str:
    """The permission mode every Claude session the operation starts runs in: config/limits.yaml
    `claude_permission_mode` if set, else DEFAULT_PERMISSION_MODE = `auto` (T-0077).

    Why `auto`: headless sessions (`claude -p`: scribers, Steward reviews, tooling reviews, the nightly
    Intake, the Concierge, a future Steward help-session) have nobody at the keyboard to answer a permission
    prompt. Under `acceptEdits` every scr.exe / venv python.exe / lake call and every read outside the
    worktree was denied, so three reviews in a row (T-0056, T-0063, T-0071) wrote a Decision they could not
    apply and the nightly Intake of 2026-09-16 created nothing. `auto` lets a session run the tools its brief
    names and apply its own decision; in exchange the briefs forbid what the mode would otherwise allow:
    never merge a branch, never edit a Lean statement, never run git in the scriptorium root (scr/brief.py,
    agents/steward.md). The sessions Omri opens from the UI (Discuss) take the same default ("every session
    I open should be on auto mode", 2026-09-20). The other values Claude Code accepts (`acceptEdits`,
    `bypassPermissions`, `dontAsk`, `manual`, `plan`) are overrides, never defaults:
    `scr config set limits claude_permission_mode <mode>`, or `--permission-mode` on `scr launch` / `scr watch`
    for one run. A missing or unreadable limits.yaml must not stop a launch: it means the default."""
    if root is None:
        return DEFAULT_PERMISSION_MODE
    p = Path(root) / "config" / "limits.yaml"
    if not p.exists():
        return DEFAULT_PERMISSION_MODE
    try:
        import yaml
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001  (scr verify reports a broken config; the launch goes on with the default)
        return DEFAULT_PERMISSION_MODE
    mode = cfg.get(PERMISSION_MODE_KEY) if isinstance(cfg, dict) else None
    return str(mode).strip() if mode else DEFAULT_PERMISSION_MODE


def new_session_id() -> str:
    """A fresh Claude Code session id (`--session-id` wants a valid UUID; claude 2.1.283, T-0247)."""
    return str(uuid.uuid4())


def session_argv(cwd: Path, root: Path | None = None, permission_mode: str | None = None, model: str | None = None,
                 extra: list[str] | None = None, session_id: str | None = None, resume: str | None = None) -> list[str] | None:
    """The argv of a headless session: `claude -p --session-id <uuid> --permission-mode <mode> --output-format text`,
    plus `--add-dir <root>` when it runs somewhere other than the scriptorium root (a task worktree) so it can
    read tasks/<id>/ and agents/. `permission_mode=None` means the config default (permission_mode_for).
    `session_id=None` means a new one: every session gets an id the launcher knows, so it can be resumed
    (`scr takeover`, T-0247); a caller that records the id passes it in (new_session_id)."""
    mode = permission_mode or permission_mode_for(root)
    # `resume` = continue an existing session (the job runner hands a finished job back to the session that
    # submitted it, scr/jobs.py): `claude -p --resume <id>` instead of a fresh `--session-id`
    ident = ["--resume", resume] if resume else ["--session-id", session_id or new_session_id()]
    args = claude_argv("-p", *ident, "--permission-mode", mode, "--output-format", "text")
    if not args:
        return None
    if root is not None and Path(cwd).resolve() != Path(root).resolve():
        args += ["--add-dir", str(root)]
    if model:
        args += ["--model", model]
    args += extra or []
    return args


def argv_value(argv: list[str], flag: str) -> str | None:
    """The value after `flag` in an argv, or None."""
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


def argv_values(argv: list[str], flag: str) -> list[str]:
    """Every value after a repeatable `flag` (`--add-dir`)."""
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


def record_launch(root: Path, kind: str, task: str | None, pid: int | None, session_id: str | None, cwd: Path,
                  argv: list[str] | None = None, note: str | None = None, actor: str | None = None,
                  data: dict | None = None) -> None:
    """The ONE `launch` system event of a headless session (T-0247): `kind` (scriber | review | tooling-review |
    intake | concierge, T-0108's names), `session_id`, `pid`, `cwd` (resolved: a session is stored under its
    project folder and only resumes from there), and the `agent` / `add_dirs` / `permission_mode` its argv had,
    so `scr takeover` can reopen it exactly, and the pid's `created` (process_created, unless `data` gives it)
    so a pid reused by another process is never taken for the session (T-0147). `data` adds the caller's own
    fields (the scriber's transcript, created, bg_wait_minutes). The watcher reads only `kind` scriber (or no kind: older events) as a task's
    scriber session."""
    argv = argv or []
    body = {"kind": kind, "session_id": session_id, "pid": pid, "cwd": str(Path(cwd).resolve()),
            "agent": argv_value(argv, "--agent"), "add_dirs": argv_values(argv, "--add-dir"),
            "permission_mode": argv_value(argv, "--permission-mode"), **(data or {})}
    if pid and "created" not in body:
        body["created"] = process_created(pid)                   # every kind: takeover --stop checks it (T-0147)
    args = ["event", "system", "--name", "launch", "--note", note or f"{kind} pid {pid}, session {session_id}",
            "--data", json.dumps(body), "--quiet"]
    if task:
        args[4:4] = ["--task", task]
    scr_cmd(root, actor or (f"steward:{task}" if task else "steward-watch"), *args)


def start_claude(cwd: Path, brief_file: Path, transcript: Path, permission_mode: str | None = None,
                 model: str | None = None, extra: list[str] | None = None, root: Path | None = None,
                 env_extra: dict[str, str] | None = None, session_id: str | None = None, resume: str | None = None) -> int | None:
    """Detached `claude -p` reading the brief from stdin; stdout/stderr to the transcript. Returns the pid.
    Every call site passes `root` and leaves `permission_mode` to the config default (see session_argv).
    `env_extra` is added to the inherited environment (the launcher's per-task background-wait ceiling,
    scr/bgwait.py); None leaves the environment as it is. `session_id` is the `--session-id` the caller records
    with record_launch (None: a fresh one nobody records)."""
    args = session_argv(cwd, root=root, permission_mode=permission_mode, model=model, extra=extra, session_id=session_id, resume=resume)
    if not args:
        return None
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]  (+ CREATE_NO_WINDOW from scr/__init__)
    env = {**os.environ, **env_extra} if env_extra else None
    p = subprocess.Popen(args, cwd=cwd, stdin=brief_file.open("rb"), stdout=transcript.open("ab"),
                         stderr=transcript.with_suffix(transcript.suffix + ".err").open("ab"),
                         creationflags=flags, start_new_session=(os.name != "nt"), env=env)
    return p.pid


def start_detached(args: list[str], cwd: Path, log: Path | None = None) -> int:
    """Start a long-running helper (the watcher, the UI, a job wrapper) detached from this process. The child
    can always import this `scr` package (PYTHONPATH carries its parent): a `python -m scr...` wrapper started
    from a cwd outside the checkout (a test world, 2026-10-14) failed silently without it."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]  (+ CREATE_NO_WINDOW from scr/__init__)
    out = log.open("ab") if log else subprocess.DEVNULL
    env = dict(os.environ)
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    p = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         creationflags=flags, start_new_session=(os.name != "nt"), env=env)
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


def pid_alive(pid: int | None) -> bool:
    """Is a process with this pid still running? No new dependencies: Win32 via ctypes (OpenProcess +
    GetExitCodeProcess; an access-denied handle still means "exists"), POSIX via kill(pid, 0). The
    watcher uses it on the pid the launcher recorded for a scriber session (T-0063). A pid Windows has
    already reused for another process counts as alive; the transcript `.err` check covers that gap."""
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32                                     # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE, ERROR_ACCESS_DENIED = 0x1000, 259, 5
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED or k32.GetLastError() == ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True                                              # exists but unreadable: assume alive
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_created(pid: int | None) -> int | None:
    """The creation time of the process with this pid, as a value only compared with `==`, or None when it
    cannot be read (no such process, access denied, no /proc). Win32: GetProcessTimes' creation FILETIME as
    an int, via ctypes; POSIX: the start time field of /proc/<pid>/stat. The launcher records it with the
    pid, and the watcher compares it before trusting a pid again (T-0147): a pid Windows has reused for
    another process has a different creation time."""
    if not pid or int(pid) <= 0:
        return None
    pid = int(pid)
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32                                 # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return None
            try:
                created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                value = (created.dwHighDateTime << 32) | created.dwLowDateTime
                return value or None
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rpartition(")")[2].split()[19])                  # field 22 (starttime); comm may hold spaces
    except (OSError, ValueError, IndexError):
        return None


def kill(pid: int) -> bool:
    """Stop a process and, on Windows, its children (`taskkill /T /F`). True when the stop was made; False
    when taskkill refused it (no such pid, access denied) or raised (T-0124: the escalation note says so)."""
    try:
        if os.name == "nt":
            return subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True).returncode == 0
        os.kill(pid, 9)
        return True
    except Exception:
        return False
