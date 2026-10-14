"""Jobs: long-running commands that run DETACHED under the watcher, outside any Claude session.

Why (Omri, 2026-10-10): hour-long work (lake build in check_done.py, the test suite, hotfix test, LP and certificate
runs) used to run INSIDE a headless `claude -p` session. That session holds a concurrency slot, burns tokens while
idle, and Claude Code kills its background jobs at a ceiling (600 s by default) and exits — reviews ended with
"I'll wait for the checker" and no review.md (T-0287, T-0232, T-0263, T-0292, T-0299, T-0302), hotfix close timed out
(T-0285), T-0293's 16 100 s rows sat behind a live session.

How:
  scr job submit --task T-0299 --name check_done --timeout 4h -- <command ...>
      starts `<command>` detached through a tiny wrapper (`python -m scr.jobs --run <root> <job>`), records
      .scr/jobs/<job>.json {id, task, name, cmd, cwd, log, pid, started, deadline, status, rc, ended, session}
      and returns at once. The wrapper writes the command's stdout+stderr to .scr/jobs/<job>.log and, when the
      command ends, its rc and `ended` into the record. Over `jobs_concurrency` (config/limits.yaml, default 2)
      a job is `queued`; the watcher starts it when a slot frees.
  WAITING: job J-0001        — the LAST line a scriber writes in log.md before ending its turn. The watcher then sets
      the task `waiting` (a status that frees the concurrency slot, scr/lifecycle.py) instead of `returned`.
  The watcher polls every tick (poll): a running job whose wrapper pid is gone with no rc is `failed`; one past its
      deadline is killed and `timeout`; queued jobs start when a slot frees. For a finished job whose task is
      `waiting` it resumes the task's session by id (`claude -p --resume <session>`) with the result as the prompt
      and sets the task in_progress again (Watcher.jobs_tick). job_submitted / job_done / job_failed events go to
      the event log; a failed or timed-out job is a needs-you card.
  scr job list [--all] | tail <job> [-n N] | kill <job> | wait <job>
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import now_iso
from .run import kill, pid_alive, start_detached

JOBS_DIR = ".scr/jobs"
DEFAULT_TIMEOUT_MIN = 240
DEFAULT_CONCURRENCY = 2
AUTH_LIMIT_MINUTES = 12 * 60          # the bgwait rule: longer than 12 h needs Omri (scr/bgwait.py)
WAIT_RE = re.compile(r"^\s*(?:-\s*)?WAITING:\s*job\s+(J-\d{4,})\b", re.IGNORECASE)
FINISHED = {"done", "failed", "timeout", "killed"}
ACTIVE = {"running", "queued"}


def jobs_dir(root: Path) -> Path:
    d = Path(root) / JOBS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(root: Path, jid: str) -> Path:
    return jobs_dir(root) / f"{jid}.json"


def load(root: Path, jid: str) -> dict | None:
    p = _path(root, jid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(root: Path, job: dict) -> None:
    p = _path(root, job["id"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def list_jobs(root: Path, status: str | None = None, active_only: bool = False) -> list[dict]:
    out = []
    for p in sorted(jobs_dir(root).glob("J-*.json")):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if status and j.get("status") != status:
            continue
        if active_only and j.get("status") not in ACTIVE:
            continue
        out.append(j)
    return out


def next_id(root: Path) -> str:
    n = 0
    for p in jobs_dir(root).glob("J-*.json"):
        m = re.match(r"J-(\d+)", p.stem)
        if m:
            n = max(n, int(m.group(1)))
    return f"J-{n + 1:04d}"


def parse_timeout(value) -> int:
    """Minutes from '4h', '90m', '30' (minutes), '1.5h'. ValueError otherwise; always > 0."""
    if value is None or value == "":
        return DEFAULT_TIMEOUT_MIN
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        m = float(value)
    else:
        s = str(value).strip().lower()
        mt = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hmd]?)", s)
        if not mt:
            raise ValueError(f"timeout must look like 4h, 90m or 30 (minutes), got {value!r}")
        m = float(mt.group(1)) * {"": 1, "m": 1, "h": 60, "d": 1440}[mt.group(2)]
    if m <= 0:
        raise ValueError(f"timeout must be > 0, got {value!r}")
    return int(round(m))


def concurrency(root: Path) -> int:
    try:
        from .store import Store
        return int((Store(root).config("limits") or {}).get("jobs_concurrency", DEFAULT_CONCURRENCY))
    except Exception:  # noqa: BLE001 — a job must start even when config cannot be read
        return DEFAULT_CONCURRENCY


def waiting_job_in(text: str, last_lines: int = 8) -> str | None:
    """The job id named by a `WAITING: job J-xxxx` line among the last lines of a log (the scriber's hand-off)."""
    lines = [l for l in (text or "").splitlines() if l.strip()][-last_lines:]
    for line in reversed(lines):
        m = WAIT_RE.match(line)
        if m:
            return m.group(1).upper()
    return None


def _wrapper_argv(root: Path, jid: str) -> list[str]:
    return [sys.executable, "-m", "scr.jobs", "--run", str(Path(root).resolve()), jid]


def _start(root: Path, job: dict) -> dict:
    """Start a queued/new job's wrapper detached; the record becomes `running`."""
    log = jobs_dir(root) / f"{job['id']}.log"
    log.touch()
    pid = start_detached(_wrapper_argv(root, job["id"]), cwd=Path(root), log=None)
    now = datetime.now(timezone.utc)
    job.update({"status": "running", "pid": pid, "started": now_iso(),
                "deadline": (now + timedelta(minutes=int(job["timeout_min"]))).strftime("%Y-%m-%dT%H:%M:%SZ")})
    save(root, job)
    return job


def submit(root: Path, cmd: list[str], *, task: str | None = None, name: str | None = None, cwd: str | Path | None = None,
           timeout: str | int | None = None, session: str | None = None, actor: str = "scr") -> dict:
    """Record + start (or queue) a job. `cmd` is an argv list (never a shell string)."""
    if not cmd:
        raise ValueError("job submit: a command is required after `--`")
    minutes = parse_timeout(timeout)
    if minutes > AUTH_LIMIT_MINUTES and actor != "omri":
        raise ValueError(f"timeout {minutes} min is over {AUTH_LIMIT_MINUTES // 60} h; only Omri may submit that (--actor omri)")
    if cmd[0].startswith("-") or (name or "").startswith("--"):
        # J-0001 (2026-10-10): `scr job submit --root <live> ...` read `--root` as the job name and the flag list as the
        # command (rc 127 in 0 s). A program never starts with `-`; global flags go BEFORE `job`.
        raise ValueError(f"job submit: the command/name may not start with `-` (got {cmd[0] if cmd[0].startswith('-') else name!r}); "
                         "put global flags before `job`: scr --root <live root> job submit --task <id> --name <n> -- <command>")
    root = Path(root)
    jid = next_id(root)
    # cwd = the CALLER's directory (T-0335): a scriber submits from its worktree with `--root <live root>`; the job is
    # recorded under the live root but must run where the scriber works, not in the scriptorium folder.
    job = {"id": jid, "task": task, "name": name or Path(cmd[0]).name, "cmd": list(cmd),
           "cwd": str(Path(cwd or os.getcwd()).resolve()), "log": str((jobs_dir(root) / f"{jid}.log").resolve()),
           "timeout_min": minutes, "session": session, "submitted": now_iso(), "actor": actor,
           "status": "queued", "pid": None, "started": None, "deadline": None, "rc": None, "ended": None, "resumed": None}
    save(root, job)
    if len(list_jobs(root, status="running")) < concurrency(root):
        _start(root, job)
    return job


def run_wrapper(root: Path, jid: str) -> int:
    """The detached wrapper: run the command, stream its output to the log, record rc + ended. Exit code = rc."""
    root = Path(root)
    job = load(root, jid)
    if not job:
        return 2
    log = Path(job["log"])
    env = {**os.environ, "SCR_JOB": jid, "SCR_ROOT": str(root)}
    if job.get("task"):
        env["SCR_TASK"] = job["task"]
    with log.open("ab") as out:
        out.write(f"[job {jid}] {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} start: {' '.join(job['cmd'])} (cwd {job['cwd']})\n".encode())
        out.flush()
        try:
            p = subprocess.Popen(job["cmd"], cwd=job["cwd"], stdout=out, stderr=subprocess.STDOUT, env=env)
            job["child_pid"] = p.pid
            save(root, job)
            rc = p.wait()
        except FileNotFoundError as e:
            out.write(f"[job {jid}] command not found: {e}\n".encode())
            rc = 127
        except Exception as e:  # noqa: BLE001
            out.write(f"[job {jid}] wrapper error: {e}\n".encode())
            rc = 126
        out.write(f"[job {jid}] {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} end: rc {rc}\n".encode())
    job = load(root, jid) or job
    if job.get("status") == "running":            # a kill/timeout the watcher applied first keeps its verdict
        job.update({"status": "done" if rc == 0 else "failed", "rc": rc, "ended": now_iso()})
        save(root, job)
    return rc


def seconds_of(job: dict) -> int | None:
    if not job.get("started"):
        return None
    end = job.get("ended") or now_iso()
    try:
        a = datetime.fromisoformat(job["started"].replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return int((b - a).total_seconds())
    except ValueError:
        return None


def kill_job(root: Path, jid: str, status: str = "killed", note: str = "") -> dict | None:
    job = load(root, jid)
    if not job:
        return None
    if job["status"] == "running" and job.get("pid"):
        kill(int(job["pid"]))                          # taskkill /T takes the child tree with it
    if job["status"] in ACTIVE:
        job.update({"status": status, "ended": now_iso(), "note": note or job.get("note")})
        save(root, job)
    return job


def poll(root: Path, now: datetime | None = None) -> list[dict]:
    """One watcher pass over the jobs: finish dead wrappers, enforce deadlines, start queued jobs. Returns the jobs
    whose status changed this pass (each with `transition`: failed | timeout | started)."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    changed: list[dict] = []
    for job in list_jobs(root, status="running"):
        # the wrapper may have finished between the listing and here: re-read before judging
        job = load(root, job["id"]) or job
        if job["status"] != "running":
            continue
        deadline = job.get("deadline")
        if deadline and datetime.fromisoformat(deadline.replace("Z", "+00:00")) <= now:
            kill_job(root, job["id"], status="timeout", note=f"deadline {deadline} passed ({job['timeout_min']} min)")
            job = load(root, job["id"]) or job
            job["transition"] = "timeout"
            changed.append(job)
            continue
        if job.get("pid") and not pid_alive(int(job["pid"])):
            job = load(root, job["id"]) or job              # the wrapper writes rc right before it exits
            if job["status"] == "running":
                job.update({"status": "failed", "ended": now_iso(), "note": f"wrapper pid {job['pid']} gone with no exit code"})
                save(root, job)
                job["transition"] = "failed"
                changed.append(job)
    cap = concurrency(root)
    running = len(list_jobs(root, status="running"))
    for job in list_jobs(root, status="queued"):
        if running >= cap:
            break
        _start(root, job)
        running += 1
        job["transition"] = "started"
        changed.append(job)
    return changed


def tail(root: Path, jid: str, n: int = 40) -> str:
    job = load(root, jid)
    if not job:
        return f"{jid}: no such job"
    p = Path(job["log"])
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-n:])


def describe(job: dict) -> str:
    secs = seconds_of(job)
    dur = f"{secs // 60} min" if secs is not None and secs >= 120 else (f"{secs} s" if secs is not None else "-")
    rc = f" rc {job['rc']}" if job.get("rc") is not None else ""
    return (f"{job['id']} {job['status']:<8} {job.get('task') or '-':<7} {job.get('name') or ''}{rc} {dur}"
            + (f"  [{job['note']}]" if job.get("note") else ""))


def wait(root: Path, jid: str, poll_seconds: float = 10.0, timeout_seconds: float | None = None) -> dict | None:
    """Block until the job leaves running/queued (for a caller that stays in the foreground, e.g. a reviewer)."""
    t0 = time.monotonic()
    while True:
        job = load(root, jid)
        if not job or job["status"] in FINISHED:
            return job
        if timeout_seconds is not None and time.monotonic() - t0 > timeout_seconds:
            return job
        time.sleep(poll_seconds)


if __name__ == "__main__":                       # the detached wrapper entry: python -m scr.jobs --run <root> <job>
    if len(sys.argv) == 4 and sys.argv[1] == "--run":
        sys.exit(run_wrapper(Path(sys.argv[2]), sys.argv[3]))
    print("usage: python -m scr.jobs --run <root> <job id>", file=sys.stderr)
    sys.exit(2)
