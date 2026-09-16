"""The Steward's mechanical half, for every running task at once (Python port of
hooks/steward_watch.ps1). `scr watch --once` for one tick; `scr watch --unattended` loops.

Per tick:
  journal      commit the scriptorium tree if scribers dirtied it (docs), push if configured
  verify       chain + replay; a failure is logged and the tick touches no records
  per running task (dispatched / in_progress / checkpoint):
    checkpoint-commit its worktree on its task branch (nothing may exist only in a worktree)
    scope drift  -> scope_drift events (once per file)
    HELP: lines  -> help events (once per line)
    wall budget  -> budget_wall (once)
    submission   report.md != template -> submitted
    heartbeat    checkpoint -> in_progress
  submitted     -> one headless review session (auto-review)
  accepted      -> land + merge + retire (auto-land)
  merges/night  -> intake (auto-intake)
  below cap     -> dispatch + launch headless (auto-dispatch)
Never deletes anything; never edits Lean; never answers a HELP: line itself.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import EventLog
from .gitops import Git
from .journal import journal
from .ops import checkpoint_commit, in_scope, land, retire, task_worktree, workload
from .run import scr_cmd, start_claude
from .state import State
from .store import Store

STATE_FILE = ".scr/watch.state.txt"
LOG_FILE = "reports/steward_watch.log"


class Watcher:
    def __init__(self, root: Path, *, stale_minutes: int = 45, auto_review=False, auto_decide=False, auto_dispatch=False,
                 allow_statements=False, auto_land=False, auto_intake=False, permission_mode="acceptEdits", push=False,
                 quiet=False):
        self.root = root
        self.store = Store(root)
        self.limits = self.store.config("limits") or {}
        self.wl = workload(root, self.store)
        self.stale = stale_minutes
        self.auto_review, self.auto_decide, self.auto_dispatch = auto_review, auto_decide, auto_dispatch
        self.allow_statements, self.auto_land, self.auto_intake = allow_statements, auto_land, auto_intake
        self.permission_mode, self.push, self.quiet = permission_mode, push, quiet
        self.flags: set[str] = set()
        sf = root / STATE_FILE
        if sf.exists():
            self.flags = {l.strip() for l in sf.read_text(encoding="utf-8").splitlines() if l.strip()}
        tpl = root / "templates" / "report.md"
        self.report_tpl = tpl.read_text(encoding="utf-8").strip() if tpl.exists() else ""
        self.needs: list[str] = []
        self.rows: list[dict] = []

    # ---- helpers ------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
        p = self.root / LOG_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if not self.quiet:
            print(line)

    def flag(self, key: str) -> bool:
        """True the first time a key is seen (persisted across ticks)."""
        if key in self.flags:
            return False
        self.flags.add(key)
        return True

    def unflag(self, key: str) -> None:
        self.flags.discard(key)

    def save_flags(self) -> None:
        p = self.root / STATE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(sorted(self.flags)) + "\n", encoding="utf-8")

    def scr(self, actor: str, *args: str) -> str | None:
        rc, out = scr_cmd(self.root, actor, *args)
        if rc != 0:
            self.log(f"scr {' '.join(args)} FAILED: {out[-300:]}")
            return None
        return out

    def touch_log(self) -> None:
        """The UI reads the log's mtime as 'last tick'; make sure every tick leaves a line."""
        self.log("tick")

    def started_at(self, tid: str) -> datetime | None:
        ts = None
        for e in EventLog(self.root / "events" / "events.jsonl").read():
            if e.get("kind") == "state" and e.get("task") == tid and e.get("to") == "in_progress" and e.get("from") == "dispatched":
                ts = e.get("ts")
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None

    @staticmethod
    def newest_change(d: Path) -> datetime | None:
        if not d.exists():
            return None
        newest = 0.0
        for p in d.rglob("*"):
            s = str(p)
            if "/.lake/" in s or "\\.lake\\" in s or "/.git/" in s or "\\.git\\" in s or ".loopy" in s:
                continue
            try:
                if p.is_file():
                    newest = max(newest, p.stat().st_mtime)
            except OSError:
                pass
        return datetime.fromtimestamp(newest, tz=timezone.utc) if newest else None

    # ---- one tick ------------------------------------------------------------------
    def tick(self) -> dict:
        now = datetime.now(timezone.utc)
        self.needs, self.rows = [], []
        if (self.root / ".scr" / "paused").exists():
            self.log("paused (resume from the UI)")
            return {"ok": True, "rows": [], "review": [], "needs": ["watcher paused"], "paused": True}
        self.touch_log()
        sha = journal(self.root, "steward-watch", "tick: docs/records changed", push=self.push)
        if sha:
            self.log(f"journaled {sha}")
        v = State(self.root).verify()
        ok = not v["errors"]
        if not ok:
            self.log("verify FAILED — not touching records this tick: " + "; ".join(v["errors"][:3]))
            self.scr("steward-watch", "event", "system", "--name", "verify_failed", "--note", "; ".join(v["errors"][:3])[:300], "--quiet")
        tasks = self.store.load_all()
        running = [t for t in tasks.values() if t.status in {"dispatched", "in_progress", "checkpoint"}]

        for t in running:
            wt = task_worktree(self.wl, t)
            log_md = t.dir / "log.md"
            newest = max([x for x in (self.newest_change(wt), datetime.fromtimestamp(log_md.stat().st_mtime, tz=timezone.utc) if log_md.exists() else None) if x], default=None)
            idle = int((now - newest).total_seconds() // 60) if newest else -1
            flags: list[str] = []
            scope = (t.rec.get("scope") or {}).get("files") or []

            if wt.exists():
                if ok:
                    try:
                        c = checkpoint_commit(self.root, t.id, push=self.push)
                        if c:
                            flags.append(f"ckpt:{c}")
                    except Exception as e:  # noqa: BLE001 — a checkpoint failure must not stop the tick
                        self.log(f"{t.id} checkpoint commit failed: {e}")
                        self.needs.append(f"{t.id} checkpoint commit failed: {str(e)[-200:]}")
                changed = [p for p in Git(wt).dirty() if not p.startswith((".loopy/", "missions/LOOPY/runs/"))]
                # after a checkpoint commit the tree is clean; drift is judged on the branch's diff to main instead
                if not changed:
                    r = Git(wt).run("diff", "--name-only", f"{self.wl.main}...HEAD")
                    changed = [l.strip() for l in r.out.splitlines() if l.strip()] if r.ok else []
                drift = [p for p in changed if scope and not in_scope(p, scope)]
                for p in drift:
                    if self.flag(f"drift:{t.id}:{p}"):
                        self.log(f"{t.id} SCOPE DRIFT: {p}")
                        self.scr(f"steward:{t.id}", "event", "system", "--name", "scope_drift", "--task", t.id, "--note", p, "--quiet")
                if drift:
                    flags.append(f"drift:{len(drift)}")
            else:
                flags.append("no-worktree")

            if log_md.exists():
                lines = log_md.read_text(encoding="utf-8", errors="replace").splitlines()
                for i, line in enumerate(lines):
                    if re.match(r"^\s*(-\s*)?HELP:", line):
                        answered = False
                        for nxt in lines[i + 1:]:
                            if "[steward]" in nxt:
                                answered = True; break
                            if re.match(r"^\s*(-\s*)?HELP:", nxt):
                                break
                        if not answered:
                            self.needs.append(f"{t.id} HELP (log.md:{i + 1}): {line.strip()}")
                            if self.flag(f"help:{t.id}:{i + 1}"):
                                self.scr(f"steward:{t.id}", "event", "system", "--name", "help", "--task", t.id, "--note", line.strip()[:300], "--quiet")

            hours = float((t.rec.get("budget") or {}).get("max_wall_hours", 6))
            started = self.started_at(t.id)
            wall = round((now - started).total_seconds() / 3600, 1) if started else None
            if wall and wall > hours:
                flags.append(f"OVER-WALL({wall}/{hours}h)")
                if self.flag(f"wall:{t.id}"):
                    self.log(f"{t.id} wall budget exhausted: {wall} h > {hours} h")
                    self.scr(f"steward:{t.id}", "event", "system", "--name", "budget_wall", "--task", t.id, "--note", f"{wall} h", "--quiet")
                self.needs.append(f"{t.id} over wall budget ({wall} h of {hours} h) — escalate or extend")

            rep = t.dir / "report.md"
            submitted = rep.exists() and rep.read_text(encoding="utf-8", errors="replace").strip() != self.report_tpl and self.report_tpl
            if ok and submitted and t.status != "submitted":
                if t.status == "checkpoint":
                    self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--quiet")
                if t.status == "dispatched":
                    self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--quiet")
                self.scr(f"steward:{t.id}", "task", "set", t.id, "submitted", "--note", "report.md written (watcher)", "--quiet")
                self.log(f"{t.id} -> submitted (report.md written)")
                flags.append("SUBMITTED")
            elif ok and t.status in {"in_progress", "checkpoint"}:
                if t.status == "in_progress":
                    self.scr(f"steward:{t.id}", "task", "set", t.id, "checkpoint", "--quiet")
                self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--quiet")
            if idle >= self.stale:
                flags.append(f"STALE {idle}m")
                self.needs.append(f"{t.id} silent for {idle} min — is its session alive?")
            self.rows.append({"task": t.id, "status": t.status, "idle_min": idle, "wall_h": wall, "flags": " ".join(flags), "title": t.rec.get("title", "")[:60]})

        tasks = self.store.load_all()
        review = [t for t in tasks.values() if t.status == "submitted"]
        for t in review:
            if self.auto_review and ok and self.flag(f"review:{t.id}"):
                self.start_review(t)

        merged_now = 0
        if self.auto_land and ok:
            for t in [t for t in tasks.values() if t.status == "accepted"]:
                if not self.flag(f"land:{t.id}"):
                    continue
                self.log(f"{t.id} auto-land: commit/push/PR/merge")
                try:
                    res = land(self.root, t.id, merge=True)
                except Exception as e:  # noqa: BLE001
                    res = {"ok": False, "url": None, "out": [str(e)]}
                for line in res["out"]:
                    self.log(f"  land: {line}")
                if not res["ok"]:
                    self.needs.append(f"{t.id} auto-land failed — see reports/steward_watch.log")
                    self.unflag(f"land:{t.id}")
                    continue
                merged_now += 1
                r = retire(self.root, t.id, execute=True)
                for line in r["out"]:
                    self.log(f"  retire: {line}")
                if not r["ok"]:
                    self.needs.append(f"{t.id} merged; worktree retire refused ({r['code']}) — see log")

        if self.auto_intake and ok:
            nightly = datetime.now().hour == 22 and self.flag(f"intake:{datetime.now():%Y-%m-%d}")
            if merged_now or nightly:
                self.log(f"intake run ({'nightly' if nightly else f'{merged_now} merge(s)'})")
                from .launch import intake
                for line in intake(self.root):
                    self.log(f"  intake: {line}")

        if self.auto_dispatch and ok:
            cap = int(self.limits.get("concurrency", 3))
            tasks = self.store.load_all()
            n_running = sum(1 for t in tasks.values() if t.status in {"dispatched", "in_progress", "checkpoint", "submitted"})
            if n_running < cap:
                args = ["dispatch", "--quiet"] + (["--allow-statements"] if self.allow_statements else [])
                out = self.scr("prioritizer", *args)
                if out and "dispatched T-" in out:
                    self.log("auto-dispatch: " + "; ".join(l for l in out.splitlines() if "dispatched T-" in l))
                    from .launch import launch
                    for line in launch(self.root, mode="headless", permission_mode="bypassPermissions"):
                        self.log(f"  launcher: {line}")

        self.save_flags()
        journal(self.root, "steward-watch", "tick: records changed", push=self.push)
        return {"ok": ok, "rows": self.rows, "review": [t.id for t in review], "needs": self.needs}

    def start_review(self, t) -> None:
        wt = task_worktree(self.wl, t)
        decide = (f"Then apply the decision with scr (--actor steward:{t.id}): accepted only if the statement diff is EMPTY and every "
                  "mechanical check passes; otherwise returned or escalated. Never merge a branch yourself; never edit Lean."
                  if self.auto_decide else "Do NOT change the task status — Omri applies the decision.")
        brief = (f"You are the Steward for Scriptorium task {t.id} (see {self.root / 'agents' / 'steward.md'} — follow its \"On submission\" section exactly).\n"
                 f"Record: {t.dir}  (spec.md, report.md, log.md; write review.md there).\n"
                 f"Worktree: {wt}   Workload config: {self.root / 'config' / 'workloads.yaml'} (done_when_checker, statement_diff, allowed_axioms, main_branch).\n"
                 f"Run the mechanical checks from the worktree, run statement_diff against {self.wl.main}, fill review.md completely, and end with a "
                 f"Decision line: accepted | returned (feedback) | escalated (Gate N, reason).\n{decide}\n"
                 f"Use `{self.root / '.venv' / 'Scripts' / 'scr.exe'} --actor steward:{t.id} ...` for every record change; never run git in the scriptorium folder (it journals itself).\n")
        bf = t.dir / "review-brief.txt"
        bf.write_text(brief, encoding="utf-8")
        tr = t.dir / f"review-session-{datetime.now():%Y%m%d-%H%M%S}.txt"
        if not wt.exists():
            self.log(f"{t.id} cannot start review: worktree missing"); self.unflag(f"review:{t.id}"); return
        pid = start_claude(wt, bf, tr, permission_mode=self.permission_mode)
        if pid:
            self.log(f"{t.id} review session started (pid {pid}) -> {tr}")
            self.scr(f"steward:{t.id}", "event", "system", "--name", "review_started", "--task", t.id, "--note", f"pid {pid}", "--quiet")
        else:
            self.log(f"{t.id} cannot start review: claude not on PATH"); self.unflag(f"review:{t.id}")

    # ---- loop ----------------------------------------------------------------------
    def print_dashboard(self, res: dict) -> None:
        if self.quiet:
            return
        print(f"\n================ scr watch {datetime.now():%Y-%m-%d %H:%M} ================")
        if res["rows"]:
            print(f"{'task':8} {'status':12} {'idle':>5} {'wall':>5}  flags / title")
            for r in res["rows"]:
                print(f"{r['task']:8} {r['status']:12} {r['idle_min']:>5} {str(r['wall_h'] or ''):>5}  {r['flags']}  {r['title']}")
        if res["review"]:
            print("REVIEW (submitted): " + ", ".join(res["review"]))
        if res["needs"]:
            print("NEEDS YOU:"); [print("  - " + n) for n in res["needs"]]
        else:
            print("Nothing needs you this tick.")

    def loop(self, interval_min: int, once: bool = False) -> None:
        from .ui import _code_signature
        sig = _code_signature()
        while True:
            try:
                res = self.tick()
                self.print_dashboard(res)
            except Exception as e:  # noqa: BLE001 — the watcher must survive its own bugs
                self.log(f"TICK CRASHED: {e!r}")
            if once:
                break
            # sleep in small steps so a code update restarts the watcher within a minute, not a tick
            for _ in range(max(1, interval_min * 2)):
                time.sleep(30)
                if _code_signature() > sig + 0.5:
                    self.log("code changed on disk — restarting the watcher")
                    from .run import service_argv, start_detached
                    import sys as _sys
                    start_detached(service_argv(self.root) + ["--root", str(self.root)] + [a for a in _sys.argv[1:] if a not in ("--root", str(self.root))],
                                   self.root, self.root / "reports" / "watch.log")
                    return
