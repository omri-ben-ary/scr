"""The PC runs releases, never source.

A runtime folder beside the state (default `<state root>/../scr-runtime`, or $SCR_RUNTIME) holds one non-editable
venv per release (`v0.2.0/`, `v0.2.1/`, ...) and a `current` link to the live one: a directory junction on Windows,
a symlink elsewhere. The logon tasks, the desktop shortcut and every session the system launches run
`current/Scripts/scr.exe` (scr/run.py scr_exe). Until the first `scr upgrade` there is no runtime folder and
everything resolves to the checkout's `.venv` as before.

`scr upgrade <tag>` installs the release beside the live one, smoke-checks it read-only against the live state,
stops the watcher and UI, runs the state migrations with the NEW code, points `current` at it, restarts, and
smoke-checks again. Any failure points `current` back, restarts, and raises one `upgrade_failed` card.
Every external effect goes through the Ops class so the sequence is tested without network or processes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .events import EventLog

REPO_URL = "https://github.com/omri-ben-ary/scr"
TAG = re.compile(r"^v\d+\.\d+\.\d+$")


def runtime_dir(root: Path) -> Path:
    env = os.environ.get("SCR_RUNTIME")
    return Path(env) if env else Path(root).resolve().parent / "scr-runtime"


def venv_bin(venv: Path, name: str = "scr") -> Path:
    return venv / "Scripts" / f"{name}.exe" if os.name == "nt" else venv / "bin" / name


def current_link(root: Path) -> Path:
    return runtime_dir(root) / "current"


def current_target(root: Path) -> Path | None:
    """The release folder `current` points at, or None (no runtime yet, or a dangling link)."""
    link = current_link(root)
    if not (link.exists() or link.is_symlink()):
        return None
    target = Path(os.path.realpath(link))          # follows a junction too (Path.resolve does on 3.10+)
    return target if target != link and target.is_dir() else None


def current_venv(root: Path) -> Path | None:
    """The live release's venv when a runtime is in use and its scr exists; else None (use the checkout's .venv)."""
    t = current_target(root)
    return current_link(root) if t is not None and venv_bin(t).exists() else None


def releases(root: Path) -> list[str]:
    rt = runtime_dir(root)
    if not rt.is_dir():
        return []
    found = [p.name for p in rt.iterdir() if p.is_dir() and TAG.match(p.name) and venv_bin(p).exists()]
    return sorted(found, key=lambda s: tuple(int(x) for x in s[1:].split(".")))


def history(root: Path) -> list[dict]:
    p = runtime_dir(root) / "history.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _note_switch(root: Path, frm: str | None, to: str, op: str, actor: str) -> None:
    p = runtime_dir(root) / "history.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "op": op, "from": frm, "to": to, "actor": actor}) + "\n")


def installed_version() -> tuple[str, str, str]:
    """(version, where, kind): the running package's version, its folder, and editable | installed."""
    from importlib.metadata import PackageNotFoundError, version
    here = Path(__file__).resolve().parent
    try:
        v = version("scriptorium")
    except PackageNotFoundError:
        v = "unknown"
    kind = "installed" if "site-packages" in here.parts or "dist-packages" in here.parts else "editable"
    return v, str(here), kind


class Ops:
    """The effects `upgrade` and `rollback` have. Tests replace methods on an instance."""

    def install(self, rt: Path, tag: str, repo_url: str) -> tuple[bool, str]:
        venv = rt / tag
        r = subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True)
        if r.returncode != 0:
            return False, (r.stdout + r.stderr).strip()[-400:]
        py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        r = subprocess.run([str(py), "-m", "pip", "install", "--quiet", f"git+{repo_url}@{tag}"], capture_output=True, text=True)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-400:]

    def run(self, argv: list[str], cwd: Path, timeout: int = 900) -> tuple[int, str]:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "SCR_NO_NOTIFY": "1"}
        try:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s: {' '.join(argv)}"
        except OSError as e:
            return 127, str(e)
        return r.returncode, (r.stdout + r.stderr).strip()

    def stop_services(self, root: Path) -> list[str]:
        from .admin import stop
        return stop(root, "ui") + stop(root, "watch")

    def start_services(self, root: Path) -> list[str]:
        from .admin import start
        return start(root, "ui") + start(root, "watch")

    def point_current(self, root: Path, target: Path) -> None:
        link = current_link(root)
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            if link.exists() or link.is_symlink():
                os.rmdir(link)                              # removes the junction itself, never the release behind it
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
            if r.returncode != 0:
                raise OSError(f"mklink /J failed: {(r.stdout + r.stderr).strip()}")
        else:
            tmp = link.with_name("current.new")
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            os.symlink(target, tmp, target_is_directory=True)
            os.replace(tmp, link)                           # atomic swap


def smoke(ops: Ops, exe: Path, root: Path) -> tuple[bool, list[str]]:
    """Read-only checks of a release against the live state: `verify`, then one watcher tick on a throwaway
    copy of the state (`watch --dry-run`)."""
    out = []
    for args in (["verify", "--quiet"], ["watch", "--dry-run"]):
        rc, text = ops.run([str(exe), "--root", str(root), *args], cwd=root)
        out.append(f"{' '.join(args)}: rc {rc}" + (f" — {text.splitlines()[-1][:200]}" if text.strip() else ""))
        if rc != 0:
            return False, out
    return True, out


def _event(root: Path, name: str, actor: str, note: str, **data) -> None:
    EventLog(root / "events" / "events.jsonl").append("system", name=name, actor=actor, note=note[:500], data=data or None)


def upgrade(root: Path, tag: str, actor: str = "omri", repo_url: str = REPO_URL, ops: Ops | None = None) -> tuple[int, list[str]]:
    """The one switch-over step. Returns (exit code, lines). See the module docstring for the steps."""
    ops = ops or Ops()
    root = Path(root).resolve()
    if not TAG.match(tag):
        return 2, [f"not a release tag: {tag!r} (want vX.Y.Z)"]
    rt = runtime_dir(root)
    target = rt / tag
    prev = current_target(root)
    out = [f"upgrade to {tag} (runtime {rt}; live: {prev.name if prev else 'checkout .venv'})"]
    if prev is not None and prev.name == tag:
        return 0, out + [f"{tag} is already live"]

    def fail(step: str, why: str, restore: bool) -> tuple[int, list[str]]:
        lines = [f"FAILED at {step}: {why}"]
        if restore:
            try:
                if prev is not None:
                    ops.point_current(root, prev)
                    lines.append(f"current -> {prev.name} again")
                else:
                    link = current_link(root)
                    if link.is_symlink() or link.exists():
                        (os.rmdir if os.name == "nt" else os.unlink)(link)
                    lines.append("current removed: the checkout .venv is live again")
                lines += ops.start_services(root)
            except Exception as e:  # noqa: BLE001 — the card below must still be raised
                lines.append(f"restore FAILED: {e!r} — start the services by hand (scr up)")
        _event(root, "upgrade_failed", actor, f"{tag}: {step}: {why}", tag=tag, step=step, back_to=prev.name if prev else None)
        return 1, out + lines

    # 1. install beside the live one
    if not venv_bin(target).exists():
        ok, text = ops.install(rt, tag, repo_url)
        if not ok or not venv_bin(target).exists():
            return fail("install", text or "scr missing after pip install", restore=False)
        out.append(f"installed {tag} into {target}")
    else:
        out.append(f"{tag} already installed at {target}")
    _event(root, "upgrade_step", actor, f"{tag}: installed", tag=tag, step="install")
    # 2. smoke the new release, read-only
    ok, lines = smoke(ops, venv_bin(target), root)
    out += [f"  pre-switch {l}" for l in lines]
    if not ok:
        return fail("pre-switch smoke", lines[-1], restore=False)
    _event(root, "upgrade_step", actor, f"{tag}: smoke passed", tag=tag, step="smoke")
    # 3. stop the watcher and the UI
    out += ops.stop_services(root)
    _event(root, "upgrade_step", actor, f"{tag}: services stopped", tag=tag, step="stop")
    # 4. migrations, run by the NEW code (it is the one that knows them)
    rc, text = ops.run([str(venv_bin(target)), "--root", str(root), "--actor", actor, "migrate"], cwd=root)
    out.append(f"  migrate: rc {rc}")
    if rc != 0:
        return fail("migrate", text.splitlines()[-1] if text.strip() else f"rc {rc}", restore=True)
    # 5. switch
    try:
        ops.point_current(root, target)
    except OSError as e:
        return fail("switch", str(e), restore=True)
    _note_switch(root, prev.name if prev else None, tag, "upgrade", actor)
    out.append(f"current -> {tag}")
    # 6. restart on the new release, 7. smoke again
    out += ops.start_services(root)
    ok, lines = smoke(ops, venv_bin(current_link(root)), root)
    out += [f"  post-switch {l}" for l in lines]
    if not ok:
        ops.stop_services(root)
        _note_switch(root, tag, prev.name if prev else "checkout", "auto-rollback", actor)
        return fail("post-switch smoke", lines[-1], restore=True)
    _event(root, "upgraded", actor, f"{prev.name if prev else 'checkout'} -> {tag}", tag=tag, previous=prev.name if prev else None)
    return 0, out + [f"{tag} is live"]


def rollback(root: Path, actor: str = "omri", ops: Ops | None = None) -> tuple[int, list[str]]:
    """Point `current` back at the release that was live before the last switch, and restart."""
    ops = ops or Ops()
    root = Path(root).resolve()
    cur = current_target(root)
    if cur is None:
        return 2, ["no runtime release is live; nothing to roll back"]
    back = next((h["from"] for h in reversed(history(root)) if h.get("to") == cur.name and h.get("from")), None)
    if not back or not venv_bin(runtime_dir(root) / back).exists():
        return 2, [f"{cur.name} is live and no earlier installed release is recorded"]
    out = ops.stop_services(root)
    ops.point_current(root, runtime_dir(root) / back)
    _note_switch(root, cur.name, back, "rollback", actor)
    out += ops.start_services(root)
    _event(root, "rolled_back", actor, f"{cur.name} -> {back}", tag=back, previous=cur.name)
    return 0, out + [f"current -> {back}"]


def version_lines(root: Path | None) -> list[str]:
    from .schema import SCHEMA_VERSION, current_version
    v, where, kind = installed_version()
    out = [f"scr {v} ({kind}: {where})", f"code schema version {SCHEMA_VERSION}"]
    if root is not None:
        out.append(f"state schema version {current_version(root)} ({root})")
        cur = current_target(root)
        rel = releases(root)
        out.append(f"runtime {runtime_dir(root)}: " + (", ".join(f"{r}{' (live)' if cur and cur.name == r else ''}" for r in rel) if rel else "none installed"))
    return out


def release(repo: Path, part: str = "patch", run=subprocess.run) -> tuple[int, list[str]]:
    """Tag the code repo's HEAD as the next release, with notes from the PRs merged since the last tag. The version
    comes from the tag (setuptools-scm), so a release is the tag alone: nothing is committed, and `main` stays
    PR-only. Prints the push command; never pushes."""
    repo = Path(repo)

    def git(*a: str) -> str:
        r = run(["git", *a], cwd=repo, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(a)}: {(r.stdout + r.stderr).strip()}")
        return r.stdout.strip()

    if git("status", "--porcelain", "--untracked-files=no"):
        return 2, ["the code repo has uncommitted changes; release from a clean main"]
    last = run(["git", "describe", "--tags", "--abbrev=0", "--match", "v*"], cwd=repo, capture_output=True, text=True)
    prev = last.stdout.strip() if last.returncode == 0 and TAG.match(last.stdout.strip()) else None
    major, minor, patch = (int(x) for x in (prev[1:] if prev else "0.1.0").split("."))
    major, minor, patch = (major, minor + 1, 0) if part == "minor" else (major, minor, patch + 1)
    tag = f"v{major}.{minor}.{patch}"
    notes = []
    for ln in git("log", "--merges", "--first-parent", "--format=%s%x1f%b%x1e", f"{prev}..HEAD" if prev else "HEAD").split("\x1e"):
        subj, _, body = ln.strip().partition("\x1f")
        pr = re.search(r"#(\d+)", subj)
        if pr:
            notes.append(f"- #{pr.group(1)} {body.strip().splitlines()[0] if body.strip() else subj}")
    message = f"{tag}\n\n" + ("\n".join(notes) if notes else "- (no merged PRs since the last tag)")
    git("tag", "-a", tag, "-m", message)
    return 0, [message, "", f"tagged {tag} locally on {git('rev-parse', '--short', 'HEAD')}. Publish it with:", f"  git push origin {tag}"]
