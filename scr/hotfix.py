"""Hotfix: the pattern for fixing Scriptorium's OWN tooling from outside the task machinery.

Why: the tooling (scr/, ui/, tests/, agents/, templates/, config/, hooks/) sometimes breaks in a way
that only an edit to the live checkout can cure — a Discuss button that will not launch, a watcher
that crashes every tick. Such a fix is made by an external agent (Claude in Cowork dropping files,
a local Claude Code session, Omri) directly in the checkout, not in a worktree, and until 2026-09-16
that left no record, no review and no follow-up. This module makes every such fix a task:

  open      `scr hotfix open "<symptom>"` (or the UI's "Fix a bug…" button, or AUTOMATICALLY the
            first time the journal commits a change under a tooling path with no hotfix open):
            a record T-xxxx in workload `scriptorium`, kind refactor, status in_progress, actor
            hotfix:<who>; spec.md holds the symptom; .scr/hotfix/open names it. The journal appends
            every commit it makes while the hotfix is open to the record's log.md.
  fix       whoever fixes edits the checkout; every scr call journals; nothing else to do.
  close     `scr hotfix close [<tid>] --summary "<what was wrong, what changed>"` (or the card's
            Close button, or the fixing agent itself): runs the workload's test command, writes
            report.md (Result = summary + files changed + test result, an `unlocks` block with the
            follow-ups the fixer proposes), sets scope.files to the changed files, status submitted.
  review    the watcher's auto-review starts a Steward session (in the scriptorium root — the
            "worktree" of a scriptorium task IS the checkout) that reads the diff, the tests and the
            report, judges it, and its Decision moves the record on; `unlocks` proposals become
            tasks through Intake (regression test, root cause, the same class of bug elsewhere).
  land      nothing to merge (the fix is already on main); accepted -> merged directly.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .events import EventLog, now_iso
from .gitops import Git
from .ops import LAND_TEST_TIMEOUT_KEY, land_test_timeout, self_workload
from .run import resolve_test_argv
from .store import Store

TOOLING = ("scr/", "ui/", "tests/", "agents/", "templates/", "config/", "hooks/", "pyproject.toml", "README.md", ".gitignore")
OPEN_FILE = ".scr/hotfix/open"
WORKLOAD = "scriptorium"     # fallback only: the checkout's workload is ops.self_workload (repo: .), whatever its key (T-0256)


def tooling_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if p.startswith(TOOLING) or p in TOOLING]


def current(root: Path) -> str | None:
    p = root / OPEN_FILE
    if not p.exists():
        return None
    tid = p.read_text(encoding="utf-8").strip()
    try:
        t = Store(root).get(tid)
        if t.status in {"in_progress", "checkpoint", "dispatched"}:
            return tid
    except Exception:  # noqa: BLE001
        pass
    p.unlink()
    return None


def _ensure_workload(root: Path) -> None:
    """config/workloads.yaml must have a `scriptorium` entry (the device got one on 2026-09-16 via T-0043)."""
    import yaml
    p = root / "config" / "workloads.yaml"
    txt = p.read_text(encoding="utf-8")
    if re.search(r"(?m)^scriptorium:\s*$", txt):
        return
    txt = txt.rstrip("\n") + (
        "\n\nscriptorium:\n"
        "  repo: .                                  # the operation's own tooling (scr/, ui/, tests/, agents/, config/)\n"
        "  main_branch: main\n"
        "  worktrees: ../scriptorium-worktrees      # outside the tree (the tree journals itself)\n"
        "  tests: .venv/Scripts/python.exe -m pytest tests -q   # the mechanical gate for tooling tasks\n")
    p.write_text(txt, encoding="utf-8")
    yaml.safe_load(txt)   # must still parse


def open_hotfix(root: Path, symptom: str, who: str = "omri", files: list[str] | None = None, auto: bool = False) -> dict:
    """Create the record and mark it open. Returns {'id', 'dir', 'brief'}."""
    store = Store(root)
    log = EventLog(root / "events" / "events.jsonl")
    tid = current(root)
    if tid:
        return {"id": tid, "dir": str(store.get(tid).dir), "brief": str(root / ".scr" / "hotfix" / f"{tid}.md"), "already_open": True}
    _ensure_workload(root)
    from . import lifecycle
    tid = store.next_id()
    wl = self_workload(root, store)
    title = f"hotfix: {symptom.strip()[:70]}"
    rec = {
        "id": tid, "title": title, "queue": "scribers", "kind": "refactor", "status": "proposed", "workload": wl.name if wl else WORKLOAD,
        "created": now_iso(), "scope": {"files": files or [], "decls": [], "may_touch_statements": False},
        "done_when": {"mechanical": ["tests pass: `scr hotfix close` runs the scriptorium workload's test command"],
                      "judgment": ["the Steward's review finds the fix addresses the symptom and proposes follow-ups"]},
        "budget": {"max_attempts": 1, "max_tokens": 500_000, "max_wall_hours": 6},
        "priority": {"omri_weight": 1.0, "verifiability": 1.0, "risk": 0.3},
        "blocked_by": [], "origin": "omri", "related": [], "supersedes": [], "unlocks": {"on_success": [], "on_failure": []},
        "attempts": 1, "tokens_used": 0, "worktree": ".", "branch": "main",
    }
    t = store.new_task(rec)
    log.append("state", task=tid, **{"from": None}, to="proposed", actor=f"hotfix:{who}", note="hotfix record")
    for st in ("triaged", "ready", "dispatched", "in_progress"):
        old = t.rec["status"]
        lifecycle.apply(t.rec, st)
        log.append("state", task=tid, **{"from": old}, to=st, actor=f"hotfix:{who}", note="hotfix" if st == "in_progress" else None)
    t.rec["attempts"] = 1
    t.save()
    head = Git(root).head(short=True) if Git(root).is_repo() else None
    spec = (f"# {title}\n\n## Goal\n\nRestore the tooling: {symptom.strip()}\n\n"
            f"## Scope\n\nThe live scriptorium checkout (`{root}`), tooling paths only ({', '.join(TOOLING)}). "
            f"Records, events and the workload repo are out of scope.\n\n"
            f"## Definition of done\n\n- mechanical: `scr hotfix close {tid}` passes the workload's test command\n"
            f"- judgment: the Steward's review confirms the symptom is gone and lists follow-ups\n\n"
            f"## Context\n\nOpened {'automatically by the journal (tooling files changed with no hotfix open)' if auto else 'by ' + who} "
            f"at {now_iso()}; checkout at {head}. Fix in place — the tree journals itself after every scr call, "
            f"and the journal appends each commit it makes to this record's log.md.\n")
    (t.dir / "spec.md").write_text(spec, encoding="utf-8")
    with (t.dir / "log.md").open("a", encoding="utf-8") as f:
        f.write(f"\n- {now_iso()} [hotfix:{who}] opened at {head}: {symptom.strip()}\n")
    hf = root / ".scr" / "hotfix"
    hf.mkdir(parents=True, exist_ok=True)
    (root / OPEN_FILE).write_text(tid, encoding="utf-8")
    brief = hf / f"{tid}.md"
    brief.write_text(fixer_brief(root, tid, symptom), encoding="utf-8")
    log.append("system", name="hotfix_open", task=tid, actor=f"hotfix:{who}", note=symptom.strip()[:300], data={"head": head, "auto": auto})
    return {"id": tid, "dir": str(t.dir), "brief": str(brief), "already_open": False}


def fixer_brief(root: Path, tid: str, symptom: str) -> str:
    from .run import scr_exe
    scr = " ".join(scr_exe(root))
    return f"""# Hotfix {tid}: {symptom.strip()}

You are the Toolsmith: you fix Scriptorium's own tooling, in place, in this checkout ({root}).
Rules:
1. Reproduce first. Read the error, the code path, `reports/ui.log`, `reports/watch.log`, `.scr/journal.log`,
   `tests/`. Say in three lines what is wrong and where before changing anything.
2. Fix minimally, in the tooling paths only ({', '.join(TOOLING)}). Never touch tasks/, events/, state/, queues/,
   or the workload repo. Never run git here: the tree journals itself after every `scr` call, and the journal
   appends its commits to this record ({Store(root).get(tid).dir / 'log.md'}).
3. Add or extend a test that fails before your change and passes after (tests/test_*.py). Run
   `{scr} hotfix test` (the workload's test command) until green.
4. Close it yourself: `{scr} --actor hotfix:agent hotfix close {tid} --summary "<what was wrong; what changed; what to watch>"`
   and, if you see follow-ups (root cause elsewhere, same class of bug in another place, a missing check),
   add them with `--unlock "<title> | <why>"` (repeatable). Closing runs the tests, writes report.md and
   submits the record for the Steward's review; Intake turns the unlocks into tasks.
5. The UI and the watcher restart themselves when tooling files change; you need not restart anything.
"""


def run_tests(root: Path, timeout: int | None = None) -> tuple[int, str]:
    store = Store(root)
    if timeout is None:     # the landing's limit (limits.yaml land_test_timeout_seconds): same suite, same budget (T-0186 fold)
        timeout = land_test_timeout(store)
    wl = self_workload(root, store)     # the workload whose repo is this checkout, not a hard-coded key (T-0256)
    cmd = (wl.cfg or {}).get("tests") if wl else None
    if not cmd:
        cmd = f"{root / '.venv' / 'Scripts' / 'python.exe'} -m pytest tests -q" if os.name == "nt" else "python -m pytest tests -q"
    argv, note = resolve_test_argv(cmd, root)     # relative program resolved against root, not the process cwd (T-0131, T-0255)
    lead = f"{note}\n" if note else ""
    env = {**os.environ, "SCR_NO_JOURNAL": "1"}
    try:
        r = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=timeout, env=env, encoding="utf-8", errors="replace")
        return r.returncode, lead + (r.stdout + r.stderr)[-6000:]
    except subprocess.TimeoutExpired:
        return 124, lead + f"tests timed out after {timeout}s (raise {LAND_TEST_TIMEOUT_KEY} in config/limits.yaml)"
    except FileNotFoundError as e:
        return 127, lead + f"test command not found: {argv[0]} (from `tests: {cmd}` in config/workloads.yaml, resolved against {root}): {e}"


PENDING = ".scr/hotfix/pending.json"    # {"who": ..., "symptom": ...} dropped by an external agent BEFORE it edits files
CLOSE_REQ = ".scr/hotfix/close.json"    # {"who": ..., "summary": ..., "unlocks": [...]} dropped by it when done


def _take(root: Path, rel: str) -> dict | None:
    p = root / rel
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        d = None
    try:
        p.unlink()
    except OSError:
        pass
    return d if isinstance(d, dict) else None


def poll_requests(root: Path) -> list[str]:
    """Honour the file requests an agent without a shell here can leave: pending.json opens (or names) the
    hotfix its edits belong to; close.json closes the open one. Called by the journal and by every watcher tick."""
    out: list[str] = []
    pend = _take(root, PENDING)
    if pend and not current(root):
        r = open_hotfix(root, pend.get("symptom") or "external fix", who=pend.get("who") or "external", files=[])
        out.append(f"hotfix {r['id']} opened from pending.json ({pend.get('who')})")
    # close only once the fix itself has been journaled: with tooling files still dirty, closing now would
    # let the following commit open a second, empty hotfix for the same change
    if (root / CLOSE_REQ).exists() and current(root) and tooling_paths(Git(root).dirty()):
        return out
    req = _take(root, CLOSE_REQ)
    if req and current(root):
        r = close_hotfix(root, None, req.get("summary") or "closed by external agent", who=req.get("who") or "external", unlocks=req.get("unlocks") or [])
        out.append(f"hotfix {r.get('id')} closed from close.json: {'tests pass' if r['ok'] else 'TESTS FAIL'}")
    return out


def journal_note(root: Path, sha: str, changed: list[str], actor: str) -> str | None:
    """Called by the journal after each commit. Appends tooling commits to the open hotfix's log; opens a
    hotfix automatically when tooling changed and none is open. Returns the hotfix id if one is involved."""
    tool = tooling_paths(changed)
    if not tool:
        return None
    tid = current(root)
    if not tid:
        pend = _take(root, PENDING) or {}
        who = pend.get("who") or (actor.split(":", 1)[-1] if actor.startswith("hotfix:") else actor)
        symptom = pend.get("symptom") or f"tooling changed outside a task by {who}: {', '.join(tool[:4])}{' …' if len(tool) > 4 else ''}"
        r = open_hotfix(root, symptom, who=who, files=tool, auto=not pend)
        tid = r["id"]
    t = Store(root).get(tid)
    with (t.dir / "log.md").open("a", encoding="utf-8") as f:
        f.write(f"- {now_iso()} [journal] {sha} [{actor}] tooling: {', '.join(tool[:12])}{' …' if len(tool) > 12 else ''}\n")
    files = set((t.rec.get("scope") or {}).get("files") or []) | set(tool)
    t.rec.setdefault("scope", {})["files"] = sorted(files)
    t.save()
    return tid


def close_hotfix(root: Path, tid: str | None, summary: str, who: str = "omri", unlocks: list[str] | None = None) -> dict:
    store = Store(root)
    log = EventLog(root / "events" / "events.jsonl")
    tid = tid or current(root)
    if not tid:
        return {"ok": False, "output": "no hotfix is open"}
    t = store.get(tid)
    rc, out = run_tests(root)
    g = Git(root)
    opened = None
    for e in log.read():
        if e.get("name") == "hotfix_open" and e.get("task") == tid:
            opened = (e.get("data") or {}).get("head")
    stat = g.run("diff", "--stat", f"{opened}..HEAD").out if opened else ""
    changed = sorted(set((t.rec.get("scope") or {}).get("files") or []) | set(tooling_paths(g.dirty())))
    unlock_rows = "\n".join(f"  - title: {u.split('|', 1)[0].strip()}\n    why: {(u.split('|', 1) + [''])[1].strip()}\n    kind: refactor\n    files: []" for u in (unlocks or []))
    report = (f"# Report {tid}: {t.rec.get('title', '')}\n\n## Result\n\n{summary.strip()}\n\n"
              f"Files changed ({len(changed)}): {', '.join(changed) or '(see git log)'}\n\n"
              f"Tests: {'PASS' if rc == 0 else 'FAIL (rc ' + str(rc) + ')'}\n\n```\n{out[-2500:]}\n```\n\n"
              f"Commits since open ({opened}):\n```\n{stat[-2000:]}\n```\n\n"
              f"## Statement changes\n\nnone (tooling)\n\n## Checks\n\n- tests: {'pass' if rc == 0 else 'FAIL'}\n\n"
              f"## unlocks\n\n```yaml\non_success:\n{unlock_rows or '  []'}\non_failure: []\n```\n")
    (t.dir / "report.md").write_text(report, encoding="utf-8")
    with (t.dir / "log.md").open("a", encoding="utf-8") as f:
        f.write(f"- {now_iso()} [hotfix:{who}] closed: {summary.strip()[:300]}; tests {'pass' if rc == 0 else 'FAIL'}\n")
    t.rec.setdefault("scope", {})["files"] = changed
    from . import lifecycle
    if t.status == "checkpoint":
        lifecycle.apply(t.rec, "in_progress"); log.append("state", task=tid, **{"from": "checkpoint"}, to="in_progress", actor=f"hotfix:{who}")
    old = t.rec["status"]
    target = "submitted" if rc == 0 else "escalated"
    lifecycle.apply(t.rec, target)
    t.save()
    log.append("state", task=tid, **{"from": old}, to=target, actor=f"hotfix:{who}", note=("tests pass; " if rc == 0 else "TESTS FAIL; ") + summary.strip()[:200])
    if rc != 0:
        lifecycle.apply(t.rec, "omri_queue"); t.save()
        log.append("state", task=tid, **{"from": "escalated"}, to="omri_queue", actor=f"hotfix:{who}", note="tests fail after hotfix")
    log.append("system", name="hotfix_closed", task=tid, actor=f"hotfix:{who}", note=f"tests {'pass' if rc == 0 else 'FAIL'}; {summary.strip()[:200]}")
    p = root / OPEN_FILE
    if p.exists():
        p.unlink()
    return {"ok": rc == 0, "id": tid, "tests_rc": rc, "output": f"{tid} -> {target}; tests {'pass' if rc == 0 else 'FAIL'}\n{out[-1500:]}"}


def status(root: Path) -> dict | None:
    tid = current(root)
    if not tid:
        return None
    t = Store(root).get(tid)
    return {"id": tid, "title": t.rec.get("title", ""), "files": (t.rec.get("scope") or {}).get("files") or [], "status": t.status,
            "brief": str(root / ".scr" / "hotfix" / f"{tid}.md")}
