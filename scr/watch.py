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
    dead session -> the launched pid is gone or the transcript .err says claude terminated it: returned
                    + session_dead (once; T-0015 heartbeated 5 h on a session killed at 20:15)
    heartbeat    checkpoint -> in_progress
    stale        idle >= stale_minutes while the session is alive -> stale event (once per idle episode)
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
from .ops import checkpoint_commit, in_scope, land, land_scriptorium, retire, self_workload, task_worktree, workload, workload_for
from .run import pid_alive, scr_cmd, start_claude
from .state import State
from .store import Store

STATE_FILE = ".scr/watch.state.txt"
LOG_FILE = "reports/steward_watch.log"
# claude -p writes this to stderr when it kills the session at its background-task ceiling (T-0067 owns the ceiling)
DEAD_LINE = "Background tasks still running after"
# Only a task whose session should be running is ever declared dead. `dispatched` has no session yet and
# cannot be returned; a `waiting` task (T-0069) is parked on purpose and is never dead.
DEAD_CHECK_STATUSES = {"in_progress", "checkpoint"}


class Watcher:
    def __init__(self, root: Path, *, stale_minutes: int = 45, auto_review=False, auto_decide=False, auto_dispatch=False,
                 allow_statements=False, auto_land=False, auto_intake=False, permission_mode=None, push=False,
                 quiet=False):
        self.root = root
        self.store = Store(root)
        self.limits = self.store.config("limits") or {}
        self.wl = workload(root, self.store)
        self.stale = stale_minutes
        self.auto_review, self.auto_decide, self.auto_dispatch = auto_review, auto_decide, auto_dispatch
        self.allow_statements, self.auto_land, self.auto_intake = allow_statements, auto_land, auto_intake
        # None = the config default (scr/run.py permission_mode_for); a value here is an override for tests and `scr watch --permission-mode`
        self.permission_mode, self.push, self.quiet = permission_mode, push, quiet
        # The journal push is a property of the scriptorium's own workload entry (`push: true` in
        # config/workloads.yaml): the unattended watcher that `scr up` starts carries no flags, and until
        # T-0061 (2026-09-19) it ran with push=False, so no journal commit ever reached origin. --push on the
        # command line still turns it on, and alone decides the checkpoint push of a task branch (QI unchanged).
        self_wl = self_workload(root, self.store)
        self.push_journal = bool(push) or bool((self_wl.cfg if self_wl else {}).get("push", False))
        self.flags: set[str] = set()
        sf = root / STATE_FILE
        if sf.exists():
            self.flags = {l.strip() for l in sf.read_text(encoding="utf-8").splitlines() if l.strip()}
        tpl = root / "templates" / "report.md"
        self.report_tpl = tpl.read_text(encoding="utf-8").strip() if tpl.exists() else ""
        self.needs: list[str] = []
        self.rows: list[dict] = []
        self.events: list[dict] | None = None      # events.jsonl, read once per tick

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

    def read_events(self) -> list[dict]:
        if self.events is None:
            self.events = EventLog(self.root / "events" / "events.jsonl").read()
        return self.events

    def started_at(self, tid: str) -> datetime | None:
        ts = None
        for e in self.read_events():
            if e.get("kind") == "state" and e.get("task") == tid and e.get("to") == "in_progress" and e.get("from") == "dispatched":
                ts = e.get("ts")
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None

    def session_of(self, t) -> dict | None:
        """The headless session the launcher started for the task's CURRENT run: {pid, transcript, ts} from
        the last `launch` event since the last dispatched -> in_progress transition (the launcher sets
        in_progress first, then launches). None for an interactive tab (no pid) or a run launched before
        the pid was recorded; the pid comes from the event's data or its `headless pid N` note, the
        transcript from the data or the launcher's line in log.md."""
        launch = None
        for e in self.read_events():
            if e.get("task") != t.id:
                continue
            if e.get("kind") == "state" and e.get("to") == "in_progress" and e.get("from") == "dispatched":
                launch = None
            elif e.get("kind") == "system" and e.get("name") == "launch":
                launch = e
        if not launch:
            return None
        data = launch.get("data") or {}
        pid = data.get("pid")
        if pid is None:
            m = re.search(r"headless pid (\d+)", launch.get("note") or "")
            pid = int(m.group(1)) if m else None
        if not pid:
            return None
        transcript = data.get("transcript")
        if not transcript:
            log_md = t.dir / "log.md"
            for line in reversed(log_md.read_text(encoding="utf-8", errors="replace").splitlines() if log_md.exists() else []):
                m = re.search(rf"\[launcher\] pid {int(pid)}, transcript (.+?)\s*$", line)
                if m:
                    transcript = Path(m.group(1).strip()).name
                    break
        return {"pid": int(pid), "transcript": transcript, "ts": launch.get("ts")}

    @staticmethod
    def session_dead(t, sess: dict) -> str | None:
        """Why the session is dead, or None: the transcript's .err carries claude's termination line, or the
        launched pid no longer exists. A pid Windows has reused still counts as alive (the .err covers the
        common case); an alive-but-silent session is STALE, never dead."""
        if sess.get("transcript"):
            err = t.dir / (sess["transcript"] + ".err")
            try:
                text = err.read_text(encoding="utf-8", errors="replace") if err.exists() else ""
            except OSError:
                text = ""
            for line in text.splitlines():
                if DEAD_LINE in line:
                    return f"claude terminated it: {line.strip()[:160]}"
        if not pid_alive(sess["pid"]):
            return f"pid {sess['pid']} no longer exists"
        return None

    @staticmethod
    def newest_change(d: Path) -> datetime | None:
        if not d.exists():
            return None
        newest = 0.0
        for p in d.rglob("*"):
            s = str(p)
            if "/.lake/" in s or "\\.lake\\" in s or "/.git/" in s or "\\.git\\" in s or ".loopy" in s or "/.venv/" in s or "\\.venv\\" in s:
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
        self.needs, self.rows, self.events = [], [], None
        if (self.root / ".scr" / "paused").exists():
            self.log("paused (resume from the UI)")
            return {"ok": True, "rows": [], "review": [], "needs": ["watcher paused"], "paused": True}
        self.touch_log()
        try:
            from .hotfix import poll_requests
            for line in poll_requests(self.root):
                self.log(line)
        except Exception as e:  # noqa: BLE001
            self.log(f"hotfix requests: {e}")
        sha = journal(self.root, "steward-watch", "tick: docs/records changed", push=self.push_journal)
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
            wl = workload_for(self.root, self.store, t)     # the task's own workload, not the default
            wt = task_worktree(wl, t)
            log_md = t.dir / "log.md"
            newest = max([x for x in (self.newest_change(wt), datetime.fromtimestamp(log_md.stat().st_mtime, tz=timezone.utc) if log_md.exists() else None) if x], default=None)
            idle = int((now - newest).total_seconds() // 60) if newest else -1
            flags: list[str] = []
            scope = (t.rec.get("scope") or {}).get("files") or []

            # A spawned worktree is checkpointed whatever its workload: until T-0078 (2026-09-21) a scriptorium
            # task was assumed to live in the checkout ("tooling") and its worktree was never committed, so
            # T-0063/T-0071 ended as uncommitted changes with no path to the trunk. Only a scriptorium task
            # WITHOUT a worktree is a hotfix made in the checkout, which the journal commits.
            if wt.exists():
                if ok:
                    try:
                        c = checkpoint_commit(self.root, t.id, push=self.push)
                        if c:
                            flags.append(f"ckpt:{c}")
                    except Exception as e:  # noqa: BLE001 — a checkpoint failure must not stop the tick
                        self.log(f"{t.id} checkpoint commit failed: {e}")
                        self.needs.append(f"{t.id} checkpoint commit failed: {str(e)[-200:]}")
                g = Git(wt)
                changed = [p for p in g.dirty() if not p.startswith((".loopy/", "missions/LOOPY/runs/"))]
                # After a checkpoint commit the tree is clean, so drift is the branch's OWN commits: its diff to
                # the merge-base with the branch it was spawned from, origin/<main> (ops.spawn uses the same
                # rule). Never the local main: that can sit far behind origin and then every file merged since
                # looks like drift (2026-09-16: 465 false files on T-0029 -> 583 alarm cards). The scriptorium's
                # own worktrees are the mirror image: spawned from the checkout's HEAD (T-0061), so their base
                # is the local trunk, which origin may trail.
                if not changed:
                    if wl.repo == self.root.resolve():
                        base = wl.main
                    else:
                        base = f"origin/{wl.main}" if g.ok("rev-parse", "--verify", "--quiet", f"origin/{wl.main}") else wl.main
                    r = g.run("diff", "--name-only", f"{base}...HEAD")
                    changed = [l.strip() for l in r.out.splitlines() if l.strip()] if r.ok else []
                drift = [p for p in changed if scope and not in_scope(p, scope)]
                new = [p for p in drift if self.flag(f"drift:{t.id}:{p}")]
                if new:
                    # ONE event per task per tick, listing the files (the UI shows one card per task and kind)
                    self.log(f"{t.id} SCOPE DRIFT: {', '.join(new)}")
                    note = ", ".join(new[:12]) + (f" (+{len(new) - 12} more)" if len(new) > 12 else "")
                    self.scr(f"steward:{t.id}", "event", "system", "--name", "scope_drift", "--task", t.id, "--note", note[:900], "--quiet")
                if drift:
                    flags.append(f"drift:{len(drift)}")
            elif t.rec.get("workload") == "scriptorium":
                flags.append("tooling")          # a hotfix in the checkout; the journal records its commits
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
            # A finished session with a report is a submission, never a death; a session that is gone
            # without one has nothing left to submit -> returned (dispatch relaunches it when ready).
            sess = self.session_of(t) if t.status in DEAD_CHECK_STATUSES else None
            dead = self.session_dead(t, sess) if sess and not submitted else None
            if ok and submitted and t.status != "submitted":
                if t.status == "checkpoint":
                    self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--quiet")
                if t.status == "dispatched":
                    self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--quiet")
                self.scr(f"steward:{t.id}", "task", "set", t.id, "submitted", "--note", "report.md written (watcher)", "--quiet")
                self.log(f"{t.id} -> submitted (report.md written)")
                flags.append("SUBMITTED")
            elif ok and dead:
                flags.append(f"DEAD pid {sess['pid']}")
                key = f"dead:{t.id}:{sess['pid']}"
                if self.flag(key):
                    self.log(f"{t.id} session dead: {dead} -> returned")
                    self.scr(f"steward:{t.id}", "event", "system", "--name", "session_dead", "--task", t.id, "--note", f"pid {sess['pid']}: {dead}"[:300], "--quiet")
                    if self.scr(f"steward:{t.id}", "task", "set", t.id, "returned", "--note", f"session died: {dead}"[:300], "--quiet") is None:
                        self.unflag(key)                                  # retried next tick
                self.needs.append(f"{t.id} session died ({dead}) — returned; no relaunch from here")
            elif ok and t.status in {"in_progress", "checkpoint"}:
                if t.status == "in_progress":
                    self.scr(f"steward:{t.id}", "task", "set", t.id, "checkpoint", "--quiet")
                self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--quiet")
            if idle >= self.stale and not dead:
                flags.append(f"STALE {idle}m")
                alive = f"pid {sess['pid']} is alive" if sess else "no recorded pid to check"
                self.needs.append(f"{t.id} silent for {idle} min ({alive}) — is its session hung?")
                if self.flag(f"stale:{t.id}"):                             # once per idle episode (see below)
                    self.scr(f"steward:{t.id}", "event", "system", "--name", "stale", "--task", t.id, "--note",
                             f"silent for {idle} min (threshold {self.stale}); {alive}", "--quiet")
            else:
                self.unflag(f"stale:{t.id}")
            self.rows.append({"task": t.id, "status": t.status, "idle_min": idle, "wall_h": wall, "flags": " ".join(flags), "title": t.rec.get("title", "")[:60]})
        # a stale flag belongs to a running task: drop it when the task moved on, so a later run can raise it again
        for key in [k for k in self.flags if k.startswith("stale:") and k.split(":", 1)[1] not in {t.id for t in running}]:
            self.unflag(key)

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
                if t.rec.get("workload") == "scriptorium":
                    if self.land_scriptorium_task(t):
                        merged_now += 1
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
                    for line in launch(self.root, mode="headless", permission_mode=self.permission_mode):
                        self.log(f"  launcher: {line}")

        self.save_flags()
        journal(self.root, "steward-watch", "tick: records changed", push=self.push_journal)
        return {"ok": ok, "rows": self.rows, "review": [t.id for t in review], "needs": self.needs}

    def land_scriptorium_task(self, t) -> bool:
        """accepted scriptorium task -> merged (T-0078). No worktree: a hotfix made in the checkout, whose
        commits the journal already put on the trunk — merged without a merge, as before. A spawned
        worktree: ops.land_scriptorium (commit, merge the trunk in, tests, merge into the trunk), then
        retire, like any other workload. A failure (conflict, failing tests, checkout off the trunk) leaves
        the task accepted, logs the reason, raises ONE land_failed event per task-branch tip and a NEEDS-YOU
        line every tick; the attempt is repeated only when task/<id> changes (a fix commit), since the same
        tip would fail the same way — or when the branch reached the trunk by hand, which is then recorded."""
        wl = workload_for(self.root, self.store, t)
        wt = task_worktree(wl, t)
        if not wt.exists():
            self.scr(f"steward:{t.id}", "task", "set", t.id, "merged", "--note", "tooling task: change already on main (journal)", "--quiet")
            self.log(f"{t.id} tooling task (no worktree) -> merged")
            return True
        g, mg = Git(wt), Git(self.root)
        tip = g.head() if not g.dirty() else None
        failed = {k for k in self.flags if k.startswith(f"land_failed:{t.id}:")}
        if tip and f"land_failed:{t.id}:{tip}" in failed and not mg.is_ancestor(f"task/{t.id}", wl.main):
            self.needs.append(f"{t.id} accepted but its landing failed at {tip[:8]} (event land_failed) — fix task/{t.id} (a new commit retries) or merge it into {wl.main} by hand")
            self.unflag(f"land:{t.id}")
            return False
        self.log(f"{t.id} auto-land (scriptorium worktree): commit, merge {wl.main} in, tests, merge into {wl.main}")
        try:
            res = land_scriptorium(self.root, t.id)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "reason": "error", "out": [f"FAILED (error): {e}"], "sha": None, "tip": g.head()}
        for line in res["out"]:
            self.log(f"  land: {line}")
        if not res["ok"]:
            why = next((l for l in res["out"] if "FAILED" in l), res["reason"]).strip()
            self.needs.append(f"{t.id} landing failed ({res['reason']}) — {why[:200]}; see reports/steward_watch.log")
            for k in failed - {f"land_failed:{t.id}:{res.get('tip') or 'unknown'}"}:   # only the current tip is remembered
                self.unflag(k)
            if self.flag(f"land_failed:{t.id}:{res.get('tip') or 'unknown'}"):
                self.scr(f"steward:{t.id}", "event", "system", "--name", "land_failed", "--task", t.id, "--note", why[:900], "--quiet")
            self.unflag(f"land:{t.id}")
            return False
        for k in failed:
            self.unflag(k)
        try:
            r = retire(self.root, t.id, execute=True)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "code": "error", "out": [str(e)]}
        for line in r["out"]:
            self.log(f"  retire: {line}")
        if not r["ok"]:
            self.needs.append(f"{t.id} merged; worktree retire refused ({r['code']}) — see log")
        return True

    def start_review(self, t) -> None:
        wl = workload_for(self.root, self.store, t)
        wt = task_worktree(wl, t)
        if t.rec.get("workload") == "scriptorium":
            return self.start_tooling_review(t)
        decide = (f"Then apply the decision with scr (--actor steward:{t.id}): accepted only if the statement diff is EMPTY and every "
                  "mechanical check passes; otherwise returned or escalated. Never merge a branch yourself; never edit Lean."
                  if self.auto_decide else "Do NOT change the task status — Omri applies the decision.")
        brief = (f"You are the Steward for Scriptorium task {t.id} (see {self.root / 'agents' / 'steward.md'} — follow its \"On submission\" section exactly).\n"
                 f"Record: {t.dir}  (spec.md, report.md, log.md; write review.md there).\n"
                 f"Worktree: {wt}   Workload config: {self.root / 'config' / 'workloads.yaml'} (done_when_checker, statement_diff, allowed_axioms, main_branch).\n"
                 f"Run the mechanical checks from the worktree, run statement_diff against {wl.main}, fill review.md completely, and end with a "
                 f"Decision line: accepted | returned (feedback) | escalated (Gate N, reason).\n{decide}\n"
                 f"Use `{self.root / '.venv' / 'Scripts' / 'scr.exe'} --actor steward:{t.id} ...` for every record change; never run git in the scriptorium folder (it journals itself).\n")
        bf = t.dir / "review-brief.txt"
        bf.write_text(brief, encoding="utf-8")
        tr = t.dir / f"review-session-{datetime.now():%Y%m%d-%H%M%S}.txt"
        if not wt.exists():
            self.log(f"{t.id} cannot start review: worktree missing"); self.unflag(f"review:{t.id}"); return
        pid = start_claude(wt, bf, tr, permission_mode=self.permission_mode, root=self.root)
        if pid:
            self.log(f"{t.id} review session started (pid {pid}) -> {tr}")
            self.scr(f"steward:{t.id}", "event", "system", "--name", "review_started", "--task", t.id, "--note", f"pid {pid}", "--quiet")
        else:
            self.log(f"{t.id} cannot start review: claude not on PATH"); self.unflag(f"review:{t.id}")

    def start_tooling_review(self, t) -> None:
        """A hotfix / tooling task: the 'worktree' is the checkout itself; the review reads the commits the
        record's log.md names, runs the tests, judges the fix, and proposes follow-ups in review.md."""
        decide = (f"Then apply the decision with scr (--actor steward:{t.id}): `task set {t.id} accepted` if the tests pass and the fix "
                  f"addresses the symptom (the watcher then lands it: a hotfix made in the checkout is already on main; a task spawned into "
                  f"a worktree is committed, merged into main and tested first — T-0078); otherwise `returned` with feedback "
                  f"or `escalated` then `omri_queue`. Never run git here; never edit the tooling in this session — a further fix is a new "
                  f"`scr hotfix open`."
                  if self.auto_decide else "Do NOT change the task status — Omri applies the decision.")
        brief = (f"You are the Steward reviewing a TOOLING task of the Scriptorium itself: {t.id} — {t.rec.get('title', '')} "
                 f"(see {self.root / 'agents' / 'steward.md'}, section \"On submission\", adapted: no Lean, no statement diff).\n"
                 f"Record: {t.dir}  (spec.md = the symptom; log.md lists the journal commits that make up the fix; report.md = the fixer's summary "
                 f"and test output; write review.md there).\n"
                 f"Checkout: {self.root} (this IS the task's tree; `git log --oneline -20` and `git show <sha>` for the commits named in log.md are read-only and fine).\n"
                 f"Do: (1) read spec, report, log; (2) `git show` each commit; (3) run `{self.root / '.venv' / 'Scripts' / 'scr.exe'} hotfix test`; "
                 f"(4) judge: does the change fix the symptom, is it minimal, is there a test that would have caught it, what else has the same bug class; "
                 f"(5) fill review.md and add an `unlocks` block of follow-up tasks (title, why, files) — regression tests, root cause, the same pattern "
                 f"elsewhere — which Intake turns into records; (6) end with a Decision line: accepted | returned (feedback) | escalated (reason).\n{decide}\n"
                 f"Use `{self.root / '.venv' / 'Scripts' / 'scr.exe'} --actor steward:{t.id} ...` for every record change; the tree journals itself.\n")
        bf = t.dir / "review-brief.txt"
        bf.write_text(brief, encoding="utf-8")
        tr = t.dir / f"review-session-{datetime.now():%Y%m%d-%H%M%S}.txt"
        pid = start_claude(self.root, bf, tr, permission_mode=self.permission_mode, root=self.root)
        if pid:
            self.log(f"{t.id} tooling review session started (pid {pid}) -> {tr}")
            self.scr(f"steward:{t.id}", "event", "system", "--name", "review_started", "--task", t.id, "--note", f"pid {pid} (tooling)", "--quiet")
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
