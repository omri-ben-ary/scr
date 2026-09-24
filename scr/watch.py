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
  submitted     -> a Decision written in review.md is applied as steward:<id> (auto-decide; T-0091)
                -> otherwise one headless review session (auto-review)
                   The review is written BEFORE anything lands (T-0081): `accepted` is refused by
                   `scr task set` and by `land` unless review.md's Decision reads `accepted` and its
                   mechanical table has a row (scr/review.py acceptance_problem); a refusal here is a
                   FAILED line in the log and a NEEDS-YOU line, never a transition. T-0021 was merged on
                   the blank template before this.
  stuck         -> escalated + one slot_freed event (auto-decide; T-0092): a task that cannot progress is
                   escalated, never held — submitted > review_stale_hours with no Decision written, or
                   in_progress/checkpoint with a blocking HELP line unanswered > help_stale_hours; the
                   escalated task no longer counts against the cap, so auto-dispatch below fills the slot
                   on the same tick. A running task's launched session is checkpointed, then stopped
                   (T-0124), so the refill never makes cap + 1 sessions
  dispatched    -> ready after dispatch_timeout_minutes with no `launch` event (auto-decide; T-0122): a launch
                   that failed after `scr dispatch` never holds a slot; one dispatch_timeout event, and the slot
                   goes to the next ready task on the same tick (a dispatched task WITH a launch event is the
                   launcher's and is left alone)
  accepted      -> land + merge + retire (auto-land)
  merges/night  -> intake (auto-intake)
  below cap     -> dispatch + launch headless (auto-dispatch)
  needs list    -> written to .scr/watch.needs.json at the end of every tick, empty or not, paused or not (T-0173):
                   one entry per NEEDS-YOU line with a stable key (`<kind>:<task>`, the flags' scheme), the task, the
                   text and the tick it was first raised; scr/ui.py needs_cards shows each as a card while it stands
  needs-you     -> every card the page shows (scr/ui.py needs_cards) that no earlier tick pushed goes out on
                   the channels of config/notify.yaml (toast, email; T-0090), once per card key (`notified:<key>`
                   in the flags file); an acked key is never sent; a failure is a `notify FAILED:` log line
Never deletes anything; never edits Lean; never answers a HELP: line itself.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import EventLog
from .gitops import Git
from .journal import journal
from .ops import LOG_PATHS, checkpoint, is_scriptorium_task, land, land_any, land_scriptorium, retire, scope_drift, self_workload, task_worktree, workload, workload_for
from .run import kill, pid_alive, scr_cmd, start_claude
from .state import State
from .store import Store

STATE_FILE = ".scr/watch.state.txt"
LOG_FILE = "reports/steward_watch.log"
NEEDS_FILE = ".scr/watch.needs.json"      # the tick's NEEDS-YOU list, read by scr/ui.py needs_cards (T-0173)
# claude -p writes this to stderr when it kills the session at its background-task ceiling (T-0067 owns the ceiling)
DEAD_LINE = "Background tasks still running after"
# Only a task whose session should be running is ever declared dead. `dispatched` has no session yet and
# cannot be returned; a `waiting` task (T-0069) is parked on purpose and is never dead.
DEAD_CHECK_STATUSES = {"in_progress", "checkpoint"}
# What holds a concurrency slot (the watcher's n_running and cmd_dispatch's `running` count exactly these).
# `escalated` and `returned` never do: an escalation is a queue item for Omri and its slot goes to the next
# ready task on the same tick (T-0092; 2026-09-19..21 T-0050 and T-0061 held both slots for 35 h).
SLOT_STATUSES = {"dispatched", "in_progress", "checkpoint", "submitted"}
REVIEW_STALE_HOURS, HELP_STALE_HOURS = 2.0, 3.0      # limits.yaml: review_stale_hours / help_stale_hours
DISPATCH_TIMEOUT_MINUTES = 30                        # limits.yaml: dispatch_timeout_minutes (T-0122)
# A HELP line the scriber cannot proceed past: plain `HELP:` or `HELP[blocks: …]:`. `HELP[proceeding: …]:`
# (T-0068) is a question asked while working, never a block.
_HELP_BLOCKING = re.compile(r"^\s*(-\s*)?HELP(\[blocks:[^\]]*\])?:")
# an answer under it: the Steward's line (`[steward]`, also what the UI's answer box writes) or Omri's
_HELP_ANSWER = re.compile(r"\[(steward|omri)(:[^\]]*)?\]")
_LINE_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?")

# ---- the Decision written in review.md (T-0091) lives in scr/review.py since T-0081, so that ops.land and
# `scr task set` read it the same way this watcher does; re-exported here so callers and tests keep working.
from .review import (DECISION_NOTE, VERDICTS, _ANY_HEADING, _DECISION_HEADING, _DECISION_LINE, _DECISION_VERDICT, _FENCE,  # noqa: E402,F401
                     _PLACEHOLDER, _SECTION_VERDICT, _unfenced, _verdict, acceptance_problem, decision_of, read_decision, review_digest)


# ---- a stuck task leaves the slot (T-0092): pure functions of (record, review.md, log.md, events, now) -------
def _event_ts(e: dict) -> datetime | None:
    ts = e.get("ts")
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
    except ValueError:
        return None


def _parse_ts(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def submitted_since(events: list[dict], tid: str) -> datetime | None:
    """When the task last became `submitted` (its current submission), from the event log."""
    since = None
    for e in events:
        if e.get("kind") == "state" and e.get("task") == tid and e.get("to") == "submitted":
            since = _event_ts(e) or since
    return since


def dispatch_stale(events: list[dict], tid: str, minutes: float, now: datetime) -> dict | None:
    """{since, age_min} when the task's CURRENT dispatch — its newest `dispatched` state event — is older than
    `minutes` and no `launch` system event follows it in the log (T-0122). The launcher (scr/launch.py)
    records `launch` right after it moves the task to in_progress, so a `dispatched` task with a later
    `launch` event is the launcher's and one without has no session: the launch failed after `scr dispatch`
    (claude not on PATH, a spawn failure, a watcher killed between the two). Only the newest dispatch counts:
    a redispatched task's earlier run, launch event and all, is history. An undatable dispatch -> None
    (never early)."""
    since, launched = None, False
    for e in events:
        if e.get("task") != tid:
            continue
        if e.get("kind") == "state" and e.get("to") == "dispatched":
            since, launched = _event_ts(e), False
        elif e.get("kind") == "system" and e.get("name") == "launch":
            launched = True
    if since is None or launched:
        return None
    age_min = (now - since).total_seconds() / 60
    if age_min <= minutes:
        return None
    return {"since": since, "age_min": round(age_min, 1)}


def submitted_stale(status: str, review_md: str, since: datetime | None, hours: float, now: datetime) -> dict | None:
    """{since, age_h} when a `submitted` task has waited more than `hours` and review.md carries NO written
    Decision. A written Decision — readable (T-0091 applies it) or not (T-0091 asks Omri to apply it by
    hand) — wins over the timeout: the reviewer finished, the wait is not the review's. Until T-0075 lands
    the wait is time-based only; with T-0075 the same check runs after its relaunch cap (review_relaunch_max)
    is reached, so a review is relaunched first and escalated after."""
    if status != "submitted" or since is None:
        return None
    verdict, problem = read_decision(review_md)
    if verdict or problem:
        return None
    age_h = (now - since).total_seconds() / 3600
    if age_h <= hours:
        return None
    return {"since": since, "age_h": round(age_h, 1)}


def help_stale(log_md: str, hours: float, now: datetime, seen: dict[str, datetime] | None = None,
               fallback: datetime | None = None) -> dict | None:
    """{line, text, since, age_h} when the NEWEST blocking HELP line of log.md (`HELP:` or `HELP[blocks: …]:`;
    a `HELP[proceeding: …]:` line is not a block) has no `[steward]` / `[omri]` line anywhere after it and is
    older than `hours`. The line is dated by `seen` (the watcher's first `help` event for that line text),
    else by a timestamp in the line itself, else by `fallback` (the caller's log.md mtime — right when the
    HELP line is the last thing written, later than the truth otherwise, so never early); undatable -> None.
    Once T-0069 lands, a blocked scriber exits with `WAITING:` and the task is `waiting`, not escalated —
    that transition is T-0069's, not implemented here."""
    lines = log_md.splitlines()
    hits = [i for i, l in enumerate(lines) if _HELP_BLOCKING.match(l)]
    if not hits:
        return None
    i = hits[-1]
    if any(_HELP_ANSWER.search(nxt) for nxt in lines[i + 1:]):
        return None
    text = lines[i].strip()
    since = (seen or {}).get(text[:300])
    if since is None:
        m = _LINE_TS.search(text)
        since = _parse_ts(m.group(0)) if m else None
    if since is None:
        since = fallback
    if since is None:
        return None
    age_h = (now - since).total_seconds() / 3600
    if age_h <= hours:
        return None
    return {"line": i + 1, "text": text, "since": since, "age_h": round(age_h, 1)}


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
        self.need_items: list[dict] = []           # the same lines with their stable keys, published each tick (T-0173)
        self.interval_min: int | None = None       # set by loop(); published so the page can tell a stopped watcher
        self.rows: list[dict] = []
        self.escalated: list[dict] = []            # the stuck tasks this tick moved to `escalated` (T-0092)
        self.timed_out: list[dict] = []            # the dispatches this tick moved back to `ready` (T-0122)
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

    def need(self, key: str, text: str, task: str | None = None) -> None:
        """One NEEDS-YOU line. `key` is `<kind>:<task>[:<detail>]` (the flags' scheme: stale:<id>, help:<id>:<line>):
        stable across ticks for as long as the condition stands, so the card, `scr ack` and `notified:<key>` hold."""
        self.needs.append(text)
        if not any(n["key"] == key for n in self.need_items):
            self.need_items.append({"key": key, "task": task, "text": text})

    def publish_needs(self, now: datetime) -> None:
        """Write this tick's needs list to NEEDS_FILE, always (an empty list clears the cards). `since` is carried
        over from the previous file for a key that is still raised, so a card keeps its key across ticks."""
        p = self.root / NEEDS_FILE
        try:
            prev = {n["key"]: n.get("since") for n in (json.loads(p.read_text(encoding="utf-8")).get("needs") or [])} if p.exists() else {}
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            prev = {}
        ts = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {"ts": ts, "interval_min": self.interval_min or int(self.limits.get("checkpoint_every_minutes", 30)),
                "needs": [{**n, "since": prev.get(n["key"]) or ts} for n in self.need_items]}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
            tmp.replace(p)
        except OSError as e:
            self.log(f"needs file FAILED: {e}")

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
        self.needs, self.need_items, self.rows, self.escalated, self.timed_out, self.events = [], [], [], [], [], None
        if (self.root / ".scr" / "paused").exists():
            self.log("paused (resume from the UI)")
            self.need("paused", "watcher paused")
            self.publish_needs(now)
            return {"ok": True, "rows": [], "review": [], "needs": self.needs, "paused": True}
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

            # A spawned worktree is checkpointed whatever its workload: until T-0078 (2026-09-21) a scriptorium
            # task was assumed to live in the checkout ("tooling") and its worktree was never committed, so
            # T-0063/T-0071 ended as uncommitted changes with no path to the trunk. Only a scriptorium task
            # WITHOUT a worktree is a hotfix made in the checkout, which the journal commits.
            if wt.exists():
                if ok:
                    try:
                        c = checkpoint(self.root, t.id, push=self.push)
                        if c and c.head:
                            flags.append(f"ckpt:{c.head}")
                        # T-0079: the checkpoint put these in a [checkpoint-drift] commit and raised the ONE
                        # scope_drift event for them; the drift block below must not raise a second one
                        for p in (c.drift if c else []):
                            self.flag(f"drift:{t.id}:{p}")
                    except Exception as e:  # noqa: BLE001 — a checkpoint failure must not stop the tick
                        self.log(f"{t.id} checkpoint commit failed: {e}")
                        self.need(f"checkpoint_failed:{t.id}", f"{t.id} checkpoint commit failed: {str(e)[-200:]}", t.id)
                g = Git(wt)
                changed = [p for p in g.dirty() if not p.startswith(LOG_PATHS)]
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
                drift = scope_drift(changed, t, repo=wt)        # the checkpoint's rule, verbatim (T-0079)
                new = [p for p in drift if self.flag(f"drift:{t.id}:{p}")]
                if new:
                    # ONE event per task per tick, listing the files (the UI shows one card per task and kind)
                    self.log(f"{t.id} SCOPE DRIFT: {', '.join(new)}")
                    note = ", ".join(new[:12]) + (f" (+{len(new) - 12} more)" if len(new) > 12 else "")
                    self.scr(f"steward:{t.id}", "event", "system", "--name", "scope_drift", "--task", t.id, "--note", note[:900], "--quiet")
                if drift:
                    flags.append(f"drift:{len(drift)}")
            elif is_scriptorium_task(self.root, self.store, t):
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
                            self.need(f"help:{t.id}:{i + 1}", f"{t.id} HELP (log.md:{i + 1}): {line.strip()}", t.id)
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
                self.need(f"wall:{t.id}", f"{t.id} over wall budget ({wall} h of {hours} h) — escalate or extend", t.id)

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
                self.need(f"dead:{t.id}", f"{t.id} session died ({dead}) — returned; no relaunch from here", t.id)
            elif ok and t.status in {"in_progress", "checkpoint"}:
                if t.status == "in_progress":
                    self.scr(f"steward:{t.id}", "task", "set", t.id, "checkpoint", "--quiet")
                self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--quiet")
            if idle >= self.stale and not dead:
                flags.append(f"STALE {idle}m")
                alive = f"pid {sess['pid']} is alive" if sess else "no recorded pid to check"
                self.need(f"stale:{t.id}", f"{t.id} silent for {idle} min ({alive}) — is its session hung?", t.id)
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
        decided: list[dict] = []
        for t in review:
            # A verdict the review wrote but could not execute (T-0061: `scr` denied, `escalated` sat 35 h)
            # is applied here; a task whose decision is applied needs no review session.
            if self.auto_decide and ok and self.apply_decision(t, decided):
                continue
            if self.auto_review and ok and self.flag(f"review:{t.id}"):
                self.start_review(t)
        if decided:
            tasks = self.store.load_all()                   # auto-land below sees an `accepted` applied this tick
            review = [t for t in tasks.values() if t.status == "submitted"]

        # Order of the slot blocks (T-0092): T-0091's Decision application above, then T-0075's review relaunch
        # (once it lands: relaunch first, escalate after its cap), then this escalation, then auto-dispatch, which
        # reloads the records and so fills the slot a stuck task freed on the SAME tick.
        # A dispatch the launcher never picked up (T-0122) goes back to `ready` first: its slot is then free for
        # the auto-dispatch below, which may hand it to the next ready task or to this one again.
        if self.auto_decide and ok and self.dispatch_checks(now, tasks):
            tasks = self.store.load_all()
        if self.auto_decide and ok and self.escalation_checks(now, tasks):
            tasks = self.store.load_all()
            review = [t for t in tasks.values() if t.status == "submitted"]

        merged_now = 0
        if self.auto_land and ok:
            for t in [t for t in tasks.values() if t.status == "accepted"]:
                if not self.flag(f"land:{t.id}"):
                    continue
                if is_scriptorium_task(self.root, self.store, t):
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
                    self.need(f"auto_land_failed:{t.id}", f"{t.id} auto-land failed — see reports/steward_watch.log", t.id)
                    self.unflag(f"land:{t.id}")
                    continue
                merged_now += 1
                r = retire(self.root, t.id, execute=True)
                for line in r["out"]:
                    self.log(f"  retire: {line}")
                if not r["ok"]:
                    self.retire_refused(t.id, r["code"])
        self.retire_refused_needs(tasks)

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
            n_running = sum(1 for t in tasks.values() if t.status in SLOT_STATUSES)   # escalated / returned never count (T-0092)
            if n_running < cap:
                args = ["dispatch", "--quiet"] + (["--allow-statements"] if self.allow_statements else [])
                out = self.scr("prioritizer", *args)
                if out and "dispatched T-" in out:
                    self.log("auto-dispatch: " + "; ".join(l for l in out.splitlines() if "dispatched T-" in l))
                    from .launch import launch
                    for line in launch(self.root, mode="headless", permission_mode=self.permission_mode):
                        self.log(f"  launcher: {line}")

        self.publish_needs(datetime.now(timezone.utc))      # before the push, so it carries the watcher's own lines too
        self.notify_cards()
        self.save_flags()
        journal(self.root, "steward-watch", "tick: records changed", push=self.push_journal)
        return {"ok": ok, "rows": self.rows, "review": [t.id for t in review], "decided": decided, "escalated": self.escalated,
                "timed_out": self.timed_out, "needs": self.needs}

    # ---- out-of-band push of NEW needs-you cards (T-0090) -------------------------------------------
    def notify_cards(self) -> list[dict]:
        """Push the cards the page shows (scr/ui.py needs_cards) that no earlier tick pushed; the dedup state is
        `notified:<key>` in the flags file. Any failure is a `notify FAILED:` log line, never a tick crash."""
        try:
            from . import notify, ui
            return notify.push(self.root, ui.needs_cards(self.root), self.flags, ui.acked_keys(self.root), self.log)
        except Exception as e:  # noqa: BLE001
            self.log(f"notify FAILED: {type(e).__name__}: {e}")
            return []

    # ---- a failed dispatch leaves the slot (T-0122) ---------------------------------------------------
    def dispatch_checks(self, now: datetime, tasks: dict) -> int:
        """`dispatched` -> `ready` for every task whose dispatch is older than dispatch_timeout_minutes with no
        `launch` event after it (`dispatch_stale`, a pure function of the log and `now`): the launcher never
        got to it, nothing will ever move it to in_progress, and `dispatched` is a slot status — until this
        check it held its slot forever (T-0092 review, item 2). The transition is lifecycle's sanctioned
        `dispatched -> ready` (dispatch_failed, redispatch), made as steward:<id>, then ONE `dispatch_timeout`
        system event whose note says how long it waited, the limit, and that the redispatch is automatic.
        Once per episode by construction: the task is no longer `dispatched`, and a redispatch writes a new
        `dispatched` event that starts a new clock — so a launch that keeps failing cycles
        ready -> dispatched -> ready at most once per limit, each round leaving its event in the journal.
        Auto-dispatch runs after this on the same tick and may pick the task again or the next ready one.
        A `dispatched` task WITH a launch event is the launcher's (it owns the in_progress transition) and is
        never touched here; a dead launched session is the dead check's business once it is in_progress
        (T-0063). Nothing is escalated: a failed dispatch needs no one. Returns how many were moved."""
        minutes = float(self.limits.get("dispatch_timeout_minutes", DISPATCH_TIMEOUT_MINUTES))
        events = self.read_events()
        n = 0
        for t in tasks.values():
            if t.status != "dispatched":
                continue
            row = dispatch_stale(events, t.id, minutes, now)
            if not row:
                continue
            note = (f"waited {row['age_min']} min in dispatched (dispatch_timeout_minutes {minutes:g}); "
                    "no launch event; redispatch is automatic")
            if self.scr(f"steward:{t.id}", "task", "set", t.id, "ready", "--note", f"dispatch timed out: {note}", "--quiet") is None:
                self.need(f"dispatch_timeout_unset:{t.id}", f"{t.id} dispatch timed out ({note}) but could not be set ready — see reports/steward_watch.log", t.id)
                continue
            self.scr(f"steward:{t.id}", "event", "system", "--name", "dispatch_timeout", "--task", t.id, "--note", note, "--quiet")
            self.log(f"{t.id} dispatch timed out -> ready ({note})")
            self.timed_out.append({"task": t.id, "since": row["since"].isoformat(), "age_min": row["age_min"]})
            for r in self.rows:
                if r["task"] == t.id:
                    r["flags"] = (r["flags"] + " DISPATCH-TIMEOUT").strip()
            n += 1
        return n

    # ---- a stuck task leaves the slot (T-0092) --------------------------------------------------------
    def escalation_checks(self, now: datetime, tasks: dict) -> int:
        """The one rule of agents/_common.md applied to every slot-holder: a task that cannot progress is
        escalated, never held. Two conditions, both pure functions above: `submitted` with no Decision written
        after review_stale_hours (T-0075's relaunch, once landed, runs before this and its cap replaces the
        clock); `in_progress`/`checkpoint` with the newest blocking HELP line unanswered after help_stale_hours
        (once T-0069 lands that scriber has exited with `WAITING:` and the task is `waiting`, not escalated;
        `waiting` is T-0069's transition and is not made here). Returns how many tasks were escalated. One
        escalation per episode: the persisted flag `slot_freed:<id>:<since>` names the episode (the submission
        time, the HELP line's first sighting), and only SLOT_STATUSES are looked at, so a task already
        `escalated` never gets a second event. Nothing is deleted: the worktree and the branch stay, and the note
        says where the work is; an in_progress/checkpoint task's launched session is stopped by `_escalate`
        after a checkpoint commit (T-0124)."""
        review_h = float(self.limits.get("review_stale_hours", REVIEW_STALE_HOURS))
        help_h = float(self.limits.get("help_stale_hours", HELP_STALE_HOURS))
        self.events = None                                     # the help events raised earlier this tick date the lines
        events = self.read_events()
        n = 0
        for t in tasks.values():
            if t.status == "submitted":
                rev = t.dir / "review.md"
                text = rev.read_text(encoding="utf-8", errors="replace") if rev.exists() else ""
                row = submitted_stale(t.status, text, submitted_since(events, t.id), review_h, now)
                if not row:
                    continue
                transcripts = sorted(p.name for p in t.dir.glob("review-session-*.txt"))
                reason = (f"submitted for {row['age_h']} h with no Decision written in review.md "
                          f"(review_stale_hours {review_h:g}); the review did not finish")
                action = ("read the review transcript, then decide in the gate card (accept / return), or re-review: "
                          f"delete `review:{t.id}` from .scr/watch.state.txt after `scr task set {t.id} returned` + `ready`")
            elif t.status in {"in_progress", "checkpoint"}:
                log_md = t.dir / "log.md"
                if not log_md.exists():
                    continue
                seen: dict[str, datetime] = {}
                for e in events:
                    if e.get("kind") == "system" and e.get("name") == "help" and e.get("task") == t.id and e.get("note") and e["note"] not in seen:
                        ts = _event_ts(e)
                        if ts:
                            seen[e["note"]] = ts
                fallback = datetime.fromtimestamp(log_md.stat().st_mtime, tz=timezone.utc)
                row = help_stale(log_md.read_text(encoding="utf-8", errors="replace"), help_h, now, seen, fallback)
                if not row:
                    continue
                sess = self.session_of(t)
                transcripts = [sess["transcript"]] if sess and sess.get("transcript") else sorted(p.name for p in t.dir.glob("session-*.txt"))[-1:]
                reason = (f"HELP line unanswered for {row['age_h']} h (log.md:{row['line']}: {row['text'][:160]}; "
                          f"help_stale_hours {help_h:g})")
                action = (f"answer log.md:{row['line']} in the record as [omri] and `scr task set {t.id} returned` + `ready` so it is "
                          "redispatched with the answer, or decide in the gate card")
            else:
                continue
            if self._escalate(t, row["since"], reason, action, transcripts):
                n += 1
        return n

    def _escalate(self, t, since: datetime, reason: str, action: str, transcripts: list[str]) -> bool:
        """submitted / in_progress / checkpoint -> escalated as steward:<id>, then ONE `slot_freed` event whose note
        is the needs-you card: the one action for Omri, the reason, where the work is, the transcript(s).
        An in_progress / checkpoint task whose launched session is alive is first checkpoint-committed HERE (not
        by the tick's earlier pass, so nothing written since is lost), then that pid is stopped with run.kill:
        otherwise auto-dispatch fills the freed slot and cap + 1 sessions run, and the old session's
        `task set submitted` is refused unexplained (T-0124, T-0092 review item 3). Only the pid session_of
        returns is ever stopped; a `submitted` task has no scriber session (its review is T-0075's). The
        escalation never depends on the stop: a failed checkpoint or kill is said in the note, not fatal."""
        key = f"slot_freed:{t.id}:{since.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
        if key in self.flags:
            return False
        wl = workload_for(self.root, self.store, t)
        wt = task_worktree(wl, t)
        branch = t.rec.get("branch") or "task/" + t.id
        where = f"work: {wt} on {branch}" + ("" if wt.exists() else " (worktree missing)")
        if t.status in DEAD_CHECK_STATUSES and (sess := self.session_of(t)):
            where = f"{self.stop_session(t, sess['pid'], wt, branch)}; {where}"
        tr = ", ".join(f"tasks/{t.dir.name}/{x}" for x in transcripts) if transcripts else "no transcript recorded"
        note = (f"stuck: {reason}; escalated by the watcher — a task that cannot progress is escalated, never held "
                f"(agents/_common.md); {where}; transcript(s): {tr}")
        if self.scr(f"steward:{t.id}", "task", "set", t.id, "escalated", "--note", note[:900], "--quiet") is None:
            self.need(f"escalate_failed:{t.id}", f"{t.id} is stuck ({reason}) but could not be escalated — see reports/steward_watch.log", t.id)
            return False
        self.flags.add(key)
        self.unflag(f"review:{t.id}")          # it left `submitted`: a later re-submission gets a fresh review
        card = (f"ACTION for Omri: {action}. Why: {reason}. Its slot is free (escalated never counts against the cap); "
                f"{where}. Transcript: {tr}")
        self.scr(f"steward:{t.id}", "event", "system", "--name", "slot_freed", "--task", t.id, "--note", card[:900], "--quiet")
        self.log(f"{t.id} stuck -> escalated ({reason}); slot freed")
        self.escalated.append({"task": t.id, "since": since.isoformat(), "reason": reason})
        for r in self.rows:
            if r["task"] == t.id:
                r["flags"] = (r["flags"] + " ESCALATED").strip()
        return True

    def stop_session(self, t, pid: int, wt: Path, branch: str) -> str:
        """Checkpoint-commit the worktree, then stop the launched session `pid` (T-0124). Returns the clause for
        the escalation note; never raises."""
        if not pid_alive(pid):
            return f"session pid {pid} was already gone"
        sha = None
        try:
            c = checkpoint(self.root, t.id, push=self.push) if wt.exists() else None
            sha = (c.head if c else None) or (Git(wt).head(short=True) if wt.exists() else None)
            ckpt = f"checkpoint {sha}" if sha else "checkpoint (no commit: no worktree)"
        except Exception as e:  # noqa: BLE001 — the uncommitted files stay in the worktree; the stop still happens
            self.log(f"{t.id} checkpoint before stopping pid {pid} failed: {e}")
            ckpt = f"a FAILED checkpoint ({str(e)[-120:]}; uncommitted files stay in the worktree)"
        try:
            stopped = kill(pid)
        except Exception as e:  # noqa: BLE001
            self.log(f"{t.id} stopping session pid {pid} raised: {e}")
            stopped = False
        if not stopped:
            self.log(f"{t.id} stopping session pid {pid} FAILED")
            return f"stopping session pid {pid} FAILED after {ckpt}: it may still be running, stop it by hand; the work is on {branch}"
        self.log(f"{t.id} session pid {pid} stopped after {ckpt}")
        return f"session pid {pid} stopped after {ckpt}; the work is on {branch}"

    def apply_decision(self, t, decided: list[dict]) -> bool:
        """A `submitted` task whose review.md states a verdict: apply it as steward:<id> (T-0091). True when
        the transition was made (appended to `decided`). No Decision -> False, nothing said. An unreadable
        or ambiguous one -> False, a log line and a NEEDS-YOU line. The note names the review text by
        digest, and a review already applied under that digest is never applied again: a task moved back
        to `submitted` with the old review.md still there gets a NEEDS-YOU line asking for a fresh review,
        not a second application of a stale verdict."""
        rev = t.dir / "review.md"
        if not rev.exists():
            return False
        text = rev.read_text(encoding="utf-8", errors="replace")
        verdict, problem = read_decision(text)
        if problem:
            self.log(f"{t.id} decision NOT applied: {problem}")
            self.need(f"decision_unreadable:{t.id}", f"{t.id} review.md has a Decision the watcher cannot read ({problem}) — apply it by hand: "
                      f"scr --actor omri task set {t.id} accepted|returned|escalated", t.id)
            return False
        if not verdict:
            return False
        digest = review_digest(text)
        note = f"{DECISION_NOTE} [review.md {digest}]"
        prior = next((e for e in self.read_events() if e.get("kind") == "state" and e.get("task") == t.id
                      and f"[review.md {digest}]" in (e.get("note") or "")), None)
        if prior:
            self.need(f"decision_stale:{t.id}", f"{t.id} is submitted again but review.md is the review already applied "
                      f"({prior.get('to')} at {prior.get('ts')}) — a fresh review is needed", t.id)
            return False
        if self.scr(f"steward:{t.id}", "task", "set", t.id, verdict, "--note", note, "--quiet") is None:
            self.need(f"decision_unapplied:{t.id}", f"{t.id} decision {verdict} from review.md could not be applied — see reports/steward_watch.log", t.id)
            return False
        self.log(f"{t.id} decision applied: {verdict}")
        decided.append({"task": t.id, "decision": verdict})
        return True

    def land_scriptorium_task(self, t) -> bool:
        """accepted scriptorium task -> merged (T-0078). No worktree: ops.land_any decides, as for `scr land`
        (T-0158) — a hotfix made in the checkout, whose commits the journal already put on the trunk, is merged
        without a merge; a task/<id> with commits not on the trunk is a lost worktree, a landing failure. A spawned
        worktree: ops.land_scriptorium (commit, merge the trunk in, tests, merge into the trunk), then
        retire, like any other workload. A failure (conflict, failing tests, checkout off the trunk) leaves
        the task accepted, logs the reason, raises ONE land_failed event per task-branch tip and a NEEDS-YOU
        line every tick; the attempt is repeated only when task/<id> changes (a fix commit), since the same
        tip would fail the same way — or when the branch reached the trunk by hand, which is then recorded."""
        wl = workload_for(self.root, self.store, t)
        wt = task_worktree(wl, t)
        # The retry key is the branch tip PLUS the record's scope (T-0079): a landing refused for out-of-scope
        # paths is retried when the Steward widens scope.files / scope.mission, not only on a new commit.
        sc = t.rec.get("scope") or {}
        scope_sig = hashlib.sha1(json.dumps([sc.get("files") or [], sc.get("mission") or ""], sort_keys=True).encode("utf-8")).hexdigest()[:8]
        failed = {k for k in self.flags if k.startswith(f"land_failed:{t.id}:")}
        if not wt.exists():
            # T-0158: ops.land_any owns the no-worktree landing (note, actor, cascade, events): a checkout hotfix
            # is recorded merged, a task/<id> with commits not on the trunk is refused (lost_worktree).
            try:
                res = land_any(self.root, t.id)
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "reason": "error", "out": [f"FAILED (error): {e}"], "sha": None, "tip": None}
            for line in res["out"]:
                self.log(f"  land: {line}")
            if not res["ok"]:
                return self.land_failed(t, res, failed, scope_sig)
            for k in failed:
                self.unflag(k)
            from .cli import refresh            # local import: cli imports this module
            refresh(self.root, quiet=True)      # the queues/STATE.json refresh the old `scr task set` subprocess did
            self.log(f"{t.id} tooling task (no worktree) -> merged")
            return True
        g, mg = Git(wt), Git(self.root)
        tip = f"{g.head()}+{scope_sig}" if not g.dirty() else None
        if tip and f"land_failed:{t.id}:{tip}" in failed and not mg.is_ancestor(f"task/{t.id}", wl.main):
            self.need(f"land_failed:{t.id}", f"{t.id} accepted but its landing failed at {tip[:8]} (event land_failed) — fix task/{t.id} (a new commit or a wider scope retries) or merge it into {wl.main} by hand", t.id)
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
            return self.land_failed(t, res, failed, scope_sig)
        for k in failed:
            self.unflag(k)
        try:
            r = retire(self.root, t.id, execute=True)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "code": "error", "out": [str(e)]}
        for line in r["out"]:
            self.log(f"  retire: {line}")
        if not r["ok"]:
            self.retire_refused(t.id, r["code"])
        return True

    def retire_refused(self, tid: str, code: str) -> None:
        """A merged task whose worktree retire was refused: its landing never runs again, so the line would stand for
        one tick only; the flag `retire_refused:<id>:<code>` keeps it raised while the worktree exists (T-0173)."""
        for k in [k for k in self.flags if k.startswith(f"retire_refused:{tid}:")]:
            self.unflag(k)
        self.flags.add(f"retire_refused:{tid}:{code}")

    def retire_refused_needs(self, tasks: dict) -> None:
        for k in sorted(k for k in self.flags if k.startswith("retire_refused:")):
            _, tid, code = k.split(":", 2)
            t = tasks.get(tid)
            if t is None or not task_worktree(workload_for(self.root, self.store, t), t).exists():
                self.unflag(k)
                continue
            self.need(f"retire_refused:{tid}", f"{tid} merged; worktree retire refused ({code}) — see log", tid)

    def land_failed(self, t, res: dict, failed: set, scope_sig: str) -> bool:
        """A refused landing (land_scriptorium or the no-worktree land_any): the task stays accepted, a NEEDS-YOU
        line every tick, ONE land_failed event per branch tip (+ scope). Returns False."""
        why = next((l for l in res["out"] if "FAILED" in l), res["reason"]).strip()
        nxt = next((l.split("— next: ", 1)[1] for l in res["out"] if "— next: " in l), None)   # land_any's closing line
        self.need(f"land_failed:{t.id}", f"{t.id} landing failed ({res['reason']}) — {why[:200]}" + (f"; next: {nxt}" if nxt else "") + "; see reports/steward_watch.log", t.id)
        key = f"land_failed:{t.id}:{res.get('tip') or 'unknown'}+{scope_sig}"
        for k in failed - {key}:                                        # only the current tip (+ scope) is remembered
            self.unflag(k)
        if self.flag(key):
            self.scr(f"steward:{t.id}", "event", "system", "--name", "land_failed", "--task", t.id, "--note", why[:900], "--quiet")
        self.unflag(f"land:{t.id}")
        return False

    def start_review(self, t) -> None:
        wl = workload_for(self.root, self.store, t)
        wt = task_worktree(wl, t)
        if is_scriptorium_task(self.root, self.store, t):
            return self.start_tooling_review(t)
        decide = (f"Then apply the decision with scr (--actor steward:{t.id}): accepted only if the statement diff is EMPTY and every "
                  "mechanical check passes; otherwise returned or escalated. Never merge a branch yourself; never edit Lean."
                  if self.auto_decide else "Do NOT change the task status — Omri applies the decision.")
        brief = (f"You are the Steward for Scriptorium task {t.id} (see {self.root / 'agents' / 'steward.md'} — follow its \"On submission\" section exactly).\n"
                 f"Record: {t.dir}  (spec.md, report.md, log.md; write review.md there).\n"
                 f"Worktree: {wt}   Workload config: {self.root / 'config' / 'workloads.yaml'} (done_when_checker, statement_diff, allowed_axioms, main_branch).\n"
                 f"Run the mechanical checks from the worktree, run check_done.py and statement_diff.py with --fetch and their default base (the merge-base of "
                 f"origin/{wl.main} and HEAD; never pass a bare {wl.main}: the stale local ref names every file merged since the branch point as out of scope), "
                 f"fill review.md completely, and end with a "
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
        self.interval_min = interval_min
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
