"""The Steward's mechanical half, for every running task at once (Python port of
hooks/steward_watch.ps1). `scr watch --once` for one tick; `scr watch --unattended` loops.

Per tick:
  journal      commit the scriptorium tree if scribers dirtied it (docs), push if configured
  verify       chain + replay; a failure is logged and the tick touches no records
  per running task (dispatched / in_progress / checkpoint):
    checkpoint-commit its worktree on its task branch (nothing may exist only in a worktree)
    scope drift  -> scope_drift events (once per file)
    HELP: lines  -> help events (once per line)
    wall budget  -> budget_wall (once per in_progress start); with auto-decide the task is then escalated below
    submission   report.md != template -> submitted, unless report_unfinished (a *_PLACEHOLDER token, an empty
                    or template-prompt `## Result`; T-0180): then one needs line, and the status stays
    dead session -> the launched pid is gone or the transcript .err says claude terminated it: returned
                    + session_dead (once; T-0015 heartbeated 5 h on a session killed at 20:15); a session that did
                    no work (no checkpoint since its launch, no [scriber] line) is then set ready, once per episode;
                    one whose transcript ends in the credit-exhaustion line is no death at all (T-0187): ready with
                    its attempt refunded, and ONE credits_exhausted event pauses auto-dispatch and reviews (one
                    `credits` needs line) for credits_pause_minutes or until a credits_resumed / watcher_resumed event
    takeover     a `takeover` event (scr takeover, T-0247) makes the run Omri's: no dead check, no stale, no HELP
                    escalation, no dispatch timeout, no relaunch; one needs line once its terminal closed unsubmitted;
                    a terminal closed with X (its recorded pid dead or reused, T-0273) -> takeover_ended by the watcher;
                    a takeover of a submitted task's REVIEW session (T-0274) holds its review the same way: no relaunch,
                    no review_stale while it stands; ended with no Decision -> `review:<id>` cleared once, so the next
                    tick relaunches the review (its review_stale clock counts from the end)
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
                   in_progress/checkpoint with a blocking HELP line unanswered > help_stale_hours, or
                   in_progress/checkpoint past its own budget.max_wall_hours (T-0121); the
                   escalated task no longer counts against the cap, so auto-dispatch below fills the slot
                   on the same tick. A running task's launched session is checkpointed, then stopped
                   (T-0124), so the refill never makes cap + 1 sessions
  dispatched    -> ready after dispatch_timeout_minutes with no `launch` event (auto-decide; T-0122): a launch
                   that failed after `scr dispatch` never holds a slot; one dispatch_timeout event, and the slot
                   goes to the next ready task on the same tick (a dispatched task WITH a launch event is the
                   launcher's and is left alone); after dispatch_timeout_rounds such rounds in a row with no launch,
                   one needs line quoting the launcher's reason from this log (T-0127; no attempt was charged)
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

from . import bgwait, jobs
from .events import EventLog
from .gitops import Git
from .journal import journal
from .lifecycle import requeue
from .ops import (LOG_PATHS, HYGIENE_KINDS, checkpoint, hygiene_digest, is_scriptorium_task, land, land_any, land_scriptorium, retire,
                  scope_drift, self_workload, sweep, task_frozen, task_worktree, workload, workload_for)
from .run import kill, new_session_id, pid_alive, process_created, record_launch, scr_cmd, service_argv, session_argv, start_claude
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
SLOT_STATUSES = {"dispatched", "in_progress", "checkpoint", "submitted"}   # `waiting` (a job runs for it, scr/jobs.py) holds no slot
REVIEW_BG_WAIT_MINUTES = 120   # a review session's background-wait ceiling (limits.yaml review_bg_wait_minutes): check_done's lake build
                               # outran Claude Code's 600 s default and every review of 9-10 Oct ended with no Decision
REVIEW_STALE_HOURS, HELP_STALE_HOURS = 2.0, 3.0      # limits.yaml: review_stale_hours / help_stale_hours
DISPATCH_TIMEOUT_MINUTES = 30                        # limits.yaml: dispatch_timeout_minutes (T-0122)
DISPATCH_TIMEOUT_ROUNDS = 3                          # limits.yaml: dispatch_timeout_rounds (T-0127)
LAUNCH_LOG_TAIL_BYTES = 256_000                      # how much of LOG_FILE launcher_reason reads (T-0127)
# A launcher line that says why a task did not launch (scr/launch.py: "claude ... not on PATH — nothing launched",
# "worktree missing — skipped", "=== T-x NOT launched: ...", spawn's "worktree add failed: ...").
_LAUNCH_FAILURE = re.compile(r"not on PATH|NOT launched|missing|skipped|fail|error|refused|denied|no record|Traceback", re.I)
CREDITS_PAUSE_MINUTES = 60                           # limits.yaml: credits_pause_minutes (T-0187)
# What a headless claude prints, alone, when the account has no usage credits left (T-0121's transcript,
# 2026-09-23): the session never ran. Anchored at a line start and read only in a transcript's last lines, so
# a report or log that merely quotes it is not a credit death.
CREDITS_LINE = re.compile(r"^\W{0,4}(You['’]re|You are) out of usage credits|^\W{0,4}Credit balance is too low", re.I)
# The events that end a credit pause before its clock does: Omri's own, or the page's Resume button.
CREDITS_RESUME_EVENTS = {"credits_resumed", "watcher_resumed"}
# A HELP line the scriber cannot proceed past: plain `HELP:` or `HELP[blocks: …]:`. `HELP[proceeding: …]:`
# (T-0068) is a question asked while working, never a block.
_HELP_BLOCKING = re.compile(r"^\s*(-\s*)?HELP(\[blocks:[^\]]*\])?:")
# an answer under it: the Steward's line (`[steward]`, also what the UI's answer box writes) or Omri's
_HELP_ANSWER = re.compile(r"\[(steward|omri)(:[^\]]*)?\]")
_LINE_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?")

# ---- the Decision written in review.md (T-0091) lives in scr/review.py since T-0081, so that ops.land and
# `scr task set` read it the same way this watcher does; re-exported here so callers and tests keep working.
from .review import (DECISION_NOTE, VERDICTS, _ANY_HEADING, _DECISION_HEADING, _DECISION_LINE, _DECISION_VERDICT, _FENCE,  # noqa: E402,F401
                     _PLACEHOLDER, _SECTION_VERDICT, _unfenced, _verdict, acceptance_problem, additions_only_gate1, decision_of, read_decision, review_digest)


# ---- a half-written report.md is not a submission (T-0180) ---------------------------------------------------
# T-0172 was submitted with a literal FULL_SUITE_<PLACEHOLDER> token where the full-suite result belonged.
_REPORT_PLACEHOLDER = re.compile(r"\b[A-Z][A-Z0-9_]*_PLACEHOLDER\b")
_REPORT_HEADING = re.compile(r"^##\s")
_RESULT_HEADING = re.compile(r"^##\s+Result\s*$")
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _result_body(text: str) -> list[str] | None:
    """The non-blank lines under `## Result` up to the next `## ` heading; None when there is no such heading."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _RESULT_HEADING.match(line.strip()):
            body = []
            for nxt in lines[i + 1:]:
                if _REPORT_HEADING.match(nxt):
                    break
                if nxt.strip():
                    body.append(nxt.strip())
            return body
    return None


def report_unfinished(text: str, tpl: str) -> str | None:
    """A short reason when report.md is not finished yet, else None: it still holds an ALL-CAPS `*_PLACEHOLDER`
    token, or its `## Result` section is empty or only the template's prompt line. A report with no
    `## Result` heading at all is not judged here (None). A token quoted inside a code fence or inline backticks
    is a mention, not a placeholder (T-0181); the Result checks still read the whole text."""
    m = _REPORT_PLACEHOLDER.search(_INLINE_CODE.sub("", "\n".join(_unfenced(text))))
    if m:
        return f"placeholder {m.group(0)} is still in it; replace it with the real text"
    body = _result_body(text)
    if body is None:
        return None
    if not body:
        return "## Result is empty; write what was produced and which done_when lines pass"
    prompt = set(_result_body(tpl) or [])
    if prompt and all(line in prompt for line in body):
        return "## Result is still the template prompt; replace it with what was produced and which done_when lines pass"
    return None


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


def is_scriber_launch(e: dict) -> bool:
    """A `launch` event of a task's SCRIBER session. Since T-0247 every headless session records one (`data.kind`
    review / tooling-review / concierge / intake too); only `scriber`, or no kind at all (the events recorded
    before T-0247), is the run the dead check, the dispatch timeout and the escalation stop are about."""
    return (e.get("kind") == "system" and e.get("name") == "launch"
            and (e.get("data") or {}).get("kind") in (None, "scriber"))


def takeover_of(events: list[dict], tid: str) -> dict | None:
    """The takeover standing on the task's current run, or None (T-0247): {ts, session_id, ended} from the newest
    `takeover` event, `ended` the ts of a later `takeover_ended` (the resumed terminal closed) or None. A new run
    (dispatched -> in_progress), a new scriber launch or a `takeover_aborted` (the headless pid could not be
    stopped, so nothing was resumed) ends it. While one stands the task is Omri's: its headless session ending is
    no death, it is not relaunched, costs no attempt, is never stale and never escalated for a HELP line.
    T-0273 adds `pid` / `created` (the terminal's process, from its `takeover_terminal` event; None until the launch
    script recorded it) and `ended_by` (the actor of the `takeover_ended`: `watcher` when the terminal was closed)."""
    cur: dict | None = None
    for e in events:
        if e.get("task") != tid:
            continue
        cur = _takeover_step(cur, e)
    return cur


def _takeover_step(cur: dict | None, e: dict) -> dict | None:
    """One event applied to the takeover standing on one run (takeover_of's rule; open_takeovers' for sessions
    with no task)."""
    name, d = e.get("name"), e.get("data") or {}
    if e.get("kind") == "state" and e.get("to") == "in_progress" and e.get("from") == "dispatched":
        return None
    if is_scriber_launch(e) or (e.get("kind") == "system" and name == "takeover_aborted"):
        return None
    if e.get("kind") != "system":
        return cur
    if name == "takeover":
        return {"ts": e.get("ts"), "task": e.get("task"), "session_id": d.get("session_id"), "ended": None, "ended_by": None,
                "pid": None, "created": None, "kind": d.get("kind")}
    if name == "takeover_terminal" and cur and not cur["ended"] and d.get("session_id") in (None, cur["session_id"]):
        cur["pid"], cur["created"] = d.get("pid"), d.get("created")
    elif name == "takeover_ended" and cur and not cur["ended"]:
        cur["ended"], cur["ended_by"] = e.get("ts"), e.get("actor")
    return cur


REVIEW_KINDS = {"review", "tooling-review"}     # the `launch` kinds of a submitted task's review session


def review_takeover(events: list[dict], tid: str) -> dict | None:
    """The takeover of the review of the task's CURRENT submission, or None (T-0274): takeover_of's takeover when
    its session kind is a review's and it was taken after the newest `submitted`. While it is open the review is
    Omri's (no relaunch, no review_stale); once it ended without a Decision the watcher clears `review:<id>` so
    the next tick relaunches the review, and the review_stale clock counts from the end."""
    tk = takeover_of(events, tid)
    if not tk or tk.get("kind") not in REVIEW_KINDS:
        return None
    since, at = submitted_since(events, tid), _event_ts({"ts": tk["ts"]})
    return tk if since and at and at >= since else None


_ENDED_SESSION = re.compile(r"session (\S+) closed")


def open_takeovers(events: list[dict]) -> list[dict]:
    """Every takeover still open (no `takeover_ended` yet), task or not (T-0273): a task's current one by
    takeover_of's rule; a session with no task (intake) by its session id, which its `takeover_ended` carries only
    in the note (`takeover terminal of session <id> closed`)."""
    tids = {e.get("task") for e in events if e.get("kind") == "system" and e.get("name") == "takeover" and e.get("task")}
    out = [tk for tid in sorted(tids) if (tk := takeover_of(events, tid)) and not tk["ended"]]
    loose: dict[str, dict | None] = {}
    for e in events:
        if e.get("kind") != "system" or e.get("task") or e.get("name") not in {"takeover", "takeover_terminal", "takeover_ended", "takeover_aborted"}:
            continue
        sid = (e.get("data") or {}).get("session_id")
        if not sid and e.get("name") == "takeover_ended":
            m = _ENDED_SESSION.search(e.get("note") or "")
            sid = m.group(1) if m else None
        if sid:
            loose[sid] = _takeover_step(loose.get(sid), e)
    return out + [tk for tk in loose.values() if tk and not tk["ended"]]


def takeover_need_text(tid: str, tk: dict) -> str:
    """The one needs line of a task whose takeover ended without a submission (T-0247); T-0273: when the watcher
    ended it, the terminal was closed without its takeover_ended line."""
    how = ("takeover terminal was closed (the watcher ended the takeover)" if tk.get("ended_by") == "watcher"
           else "takeover session ended")
    return (f"{tid} {how} without report.md — resume it again (Take over), "
            f"or `scr task set {tid} submitted` / `returned`")


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
        elif is_scriber_launch(e) or (e.get("kind") == "system" and e.get("name") == "takeover"):
            launched = True                          # a task Omri took over is his, never timed out (T-0247)
    if since is None or launched:
        return None
    age_min = (now - since).total_seconds() / 60
    if age_min <= minutes:
        return None
    return {"since": since, "age_min": round(age_min, 1)}


def dispatch_rounds(events: list[dict], tid: str) -> int:
    """How many dispatches of the task in a row timed out with no launch (T-0127): the `dispatch_timeout` system
    events of `tid` after its newest scriber `launch` (or `takeover`, which dispatch_stale also counts as the
    task being run). A launch resets the count to 0; the whole log counts for a task that never launched."""
    n = 0
    for e in events:
        if e.get("task") != tid:
            continue
        if is_scriber_launch(e) or (e.get("kind") == "system" and e.get("name") == "takeover"):
            n = 0
        elif e.get("kind") == "system" and e.get("name") == "dispatch_timeout":
            n += 1
    return n


def launcher_reason(log_text: str, tid: str) -> str | None:
    """The launcher's own words for why `tid` did not launch, from reports/steward_watch.log (T-0127): in the
    newest launcher run that names the task (the auto-dispatch branch logs every line of scr/launch.py's output
    as `launcher: <line>`, starting with `launching: <ids> (mode ...)`), the last failure line that belongs to the
    run as a whole (claude not on PATH) or to the task's own `=== <id>` section (worktree missing — skipped, a
    spawn failure, NOT launched: ...), with that line's timestamp. None when no launcher run names the task or
    the run names no failure for it."""
    lines = log_text.splitlines()
    tid_re = re.compile(rf"\b{re.escape(tid)}\b")
    start = max((i for i, l in enumerate(lines) if "launcher: launching:" in l and tid_re.search(l.split("launching:", 1)[1])), default=None)
    if start is None:
        return None
    found, section = None, None
    for line in lines[start + 1:]:
        if "launcher:" not in line:
            break                                            # the run's block ends at the first non-launcher line
        ts, _, text = line.partition("launcher:")
        text = text.strip()
        if text.startswith("=== "):
            section = text.split()[1] if len(text.split()) > 1 else None
            if section == tid and _LAUNCH_FAILURE.search(text):
                found = (text, ts.strip())
            continue
        if section in (None, tid) and _LAUNCH_FAILURE.search(text):
            found = (text, ts.strip())
    return f"{found[0]} (launcher, {found[1]})" if found else None


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


def wall_hours(rec: dict) -> float:
    """The task's own wall budget, `budget.max_wall_hours` (templates/task.yaml: 6). Per task, never limits.yaml."""
    return float((rec.get("budget") or {}).get("max_wall_hours", 6))


def wall_stale(started: datetime | None, hours: float, now: datetime) -> dict | None:
    """{since, age_h} when a running task's wall — the hours since its `dispatched -> in_progress` start — is
    over `budget.max_wall_hours` (T-0121). Out of budget is stuck (agents/_common.md: budgets are hard), so the
    tick escalates it through `_escalate` like a stale review or an unanswered HELP. Undated (no start event,
    e.g. still `dispatched`) is never early. The age is rounded the way the tick's OVER-WALL flag and the
    `budget_wall` note round it, so the flag and the escalation are raised on the same tick."""
    if started is None:
        return None
    age_h = round((now - started).total_seconds() / 3600, 1)
    if age_h <= hours:
        return None
    return {"since": started, "age_h": age_h}


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


# ---- out of usage credits is not a session death (T-0187) ----------------------------------------------------
def credits_line(*paths: Path) -> str | None:
    """The credit-exhaustion line when one of the last three non-blank lines of any of the files matches
    CREDITS_LINE (a session's transcript and its .err), else None. Unreadable or missing files are skipped."""
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        except OSError:
            continue
        for line in [l.strip() for l in text.splitlines() if l.strip()][-3:]:
            if CREDITS_LINE.search(line):
                return line[:200]
    return None


def credits_pause(events: list[dict], now: datetime, minutes: float) -> dict | None:
    """{since, until} while auto-dispatch is paused for exhausted credits: the newest `credits_exhausted` system
    event has no `credits_resumed` / `watcher_resumed` event after it and is younger than `minutes`. When the
    clock runs out the next tick's dispatch is the probe: a session that dies of credits again opens a new pause."""
    since = None
    for e in events:
        if e.get("kind") != "system":
            continue
        if e.get("name") == "credits_exhausted":
            since = _event_ts(e) or since
        elif e.get("name") in CREDITS_RESUME_EVENTS and since is not None:
            since = None
    if since is None:
        return None
    until = since + timedelta(minutes=minutes)
    return {"since": since, "until": until} if now < until else None


def did_work(events: list[dict], tid: str, since: str | None, log_md: str, pid: int) -> bool:
    """A launched session did work: a `checkpoint_commit` event for the task at or after its launch (`since`, the
    launch event's ts), or a `[scriber]` line in log.md after the launcher's line for `pid` (the whole log when
    that line is missing). An undatable launch counts every checkpoint of the task (never a false 'no work')."""
    start = _parse_ts(since) if since else None
    for e in events:
        if e.get("kind") == "system" and e.get("name") == "checkpoint_commit" and e.get("task") == tid:
            ts = _event_ts(e)
            if start is None or ts is None or ts >= start:
                return True
    lines = log_md.splitlines()
    at = max((i for i, l in enumerate(lines) if re.search(rf"\[launcher\] pid {int(pid)}\b", l)), default=-1)
    return any("[scriber]" in l for l in lines[at + 1:])


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
        self.credit_hits: list[str] = []           # the sessions this tick found dead of exhausted credits (T-0187)

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
        transcript from the data or the launcher's line in log.md. `created` (the process creation time the
        launcher read right after starting it, T-0147) is added when the event recorded one; `sess.get("created")`
        is None for older events and for a process that died before it could be read."""
        launch = None
        for e in self.read_events():
            if e.get("task") != t.id:
                continue
            if e.get("kind") == "state" and e.get("to") == "in_progress" and e.get("from") == "dispatched":
                launch = None
            elif is_scriber_launch(e):
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
        sess = {"pid": int(pid), "transcript": transcript, "ts": launch.get("ts")}
        if data.get("created") is not None:
            sess["created"] = data["created"]
        return sess

    @staticmethod
    def pid_reused(sess: dict) -> bool:
        """The session's pid now belongs to another process: the launch recorded a creation time and the live
        process with that pid has a different one (T-0147). False whenever either side is unknown (an old
        launch event, a process whose times cannot be read): then the pid is trusted exactly as before."""
        recorded = sess.get("created")
        if recorded is None:
            return False
        live = process_created(sess["pid"])
        return live is not None and live != recorded

    @staticmethod
    def session_dead(t, sess: dict) -> str | None:
        """Why the session is dead, or None: the transcript's .err carries claude's termination line, the
        launched pid no longer exists, or it was reused by another process (its creation time differs from the
        one the launch recorded, T-0147; without a recorded one a reused pid still counts as alive). An
        alive-but-silent session is STALE, never dead."""
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
        if Watcher.pid_reused(sess):
            return f"pid {sess['pid']} was reused by another process"
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

    @staticmethod
    def terminal_gone(tk: dict) -> str | None:
        """Why an open takeover's terminal is gone, or None: its recorded pid no longer exists, or the pid now
        belongs to another process (another creation time, T-0147). None when no terminal was recorded yet."""
        if not tk.get("pid"):
            return None
        if not pid_alive(tk["pid"]):
            return f"pid {tk['pid']} no longer exists"
        if Watcher.pid_reused(tk):
            return f"pid {tk['pid']} was reused by another process"
        return None

    def end_closed_takeovers(self, tasks: dict, running: set[str]) -> None:
        """T-0273: a takeover terminal closed with X never runs its `takeover_ended` line, so the task stayed Omri's
        forever. Every open takeover whose recorded terminal pid is dead or reused gets that event here, once (actor
        `watcher`, note "terminal closed"), and ONE needs line: the running loop's `takeover:<id>` line for a
        running task (every tick while it stands), this one, on the tick it is ended, for any other task or a session
        with no task. Nothing is relaunched; Omri decides what the task does next."""
        for tk in open_takeovers(self.read_events()):
            why = self.terminal_gone(tk)
            if not why:
                continue
            who = tk["task"] or f"session {tk['session_id']}"
            key = f"takeover_closed:{tk['task'] or tk['session_id']}:{tk['ts']}"
            if not self.flag(key):
                continue
            data = {"session_id": tk["session_id"], "by": "watcher", "pid": tk["pid"], "reason": why}
            if self.scr("watcher", "event", "system", "--name", "takeover_ended", *(["--task", tk["task"]] if tk["task"] else []),
                        "--note", "terminal closed", "--data", json.dumps(data), "--quiet") is None:
                self.unflag(key)                                     # retried next tick
                continue
            self.log(f"{who} takeover terminal closed without takeover_ended ({why}): takeover ended by the watcher")
            self.events = None                                       # the running loop below reads the new event
            if not tk["task"]:
                self.need(f"takeover:{tk['session_id']}", f"session {tk['session_id']} takeover terminal closed ({why}); "
                          "the watcher ended the takeover — resume it again (Take over) if it was not finished")
            elif tk["task"] not in running:
                t = tasks.get(tk["task"])
                then = ("with no Decision in review.md the review is relaunched (T-0274)" if tk.get("kind") in REVIEW_KINDS
                        else "resume it again (Take over) if it was not finished")
                self.need(f"takeover:{tk['task']}", f"{tk['task']} ({t.status if t else 'no record'}) takeover terminal of session "
                          f"{tk['session_id']} closed ({why}); the watcher ended the takeover — {then}", tk["task"])

    # ---- one tick ------------------------------------------------------------------
    def tick(self) -> dict:
        now = datetime.now(timezone.utc)
        self.needs, self.need_items, self.rows, self.escalated, self.timed_out, self.events = [], [], [], [], [], None
        self.credit_hits = []
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
        if ok:
            self.end_closed_takeovers(tasks, {t.id for t in running})

        for t in running:
            wl = workload_for(self.root, self.store, t)     # the task's own workload, not the default
            wt = task_worktree(wl, t)
            log_md = t.dir / "log.md"
            newest = max([x for x in (self.newest_change(wt), datetime.fromtimestamp(log_md.stat().st_mtime, tz=timezone.utc) if log_md.exists() else None) if x], default=None)
            idle = int((now - newest).total_seconds() // 60) if newest else -1
            flags: list[str] = []
            ckpt_now = False                                # this tick's checkpoint committed something (T-0187)

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
                            ckpt_now = True
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

            hours = wall_hours(t.rec)
            started = self.started_at(t.id)
            wall = round((now - started).total_seconds() / 3600, 1) if started else None
            if wall_stale(started, hours, now):
                flags.append(f"OVER-WALL({wall}/{hours}h)")
                # ONE diagnostic `budget_wall` per episode (the episode is this in_progress start, as for
                # `slot_freed`); the enforcement is `escalation_checks` later this tick (T-0121), which escalates
                # the task through `_escalate` unless the submission or dead branches below route it first.
                if self.flag(f"wall:{t.id}:{started.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"):
                    self.log(f"{t.id} wall budget exhausted: {wall} h > {hours} h")
                    self.scr(f"steward:{t.id}", "event", "system", "--name", "budget_wall", "--task", t.id, "--note", f"{wall} h", "--quiet")
                if not (self.auto_decide and ok) or takeover_of(self.read_events(), t.id):
                    # held (no auto-decide, or Omri's own takeover run): the line says so every tick
                    self.need(f"wall:{t.id}", f"{t.id} over wall budget ({wall} h of {hours} h) — escalate or extend", t.id)

            rep = t.dir / "report.md"
            rep_text = rep.read_text(encoding="utf-8", errors="replace").strip() if rep.exists() else ""
            submitted = rep.exists() and rep_text != self.report_tpl and self.report_tpl
            # A report still holding a *_PLACEHOLDER token or no Result is not a submission (T-0180): the task
            # keeps its status, so the dead-session and wall-budget paths below still route it.
            unfinished = report_unfinished(rep_text, self.report_tpl) if submitted and t.status != "submitted" else None
            if unfinished:
                submitted = False
                flags.append("REPORT-UNFINISHED")
                self.need(f"report:{t.id}", f"{t.id} report.md unfinished: {unfinished}", t.id)
                if self.flag(f"report_unfinished:{t.id}"):
                    self.log(f"{t.id} report.md unfinished, not submitted: {unfinished}")
            else:
                self.unflag(f"report_unfinished:{t.id}")
            # A finished session with a report is a submission, never a death; a session that is gone
            # without one has nothing left to submit -> returned (dispatch relaunches it when ready).
            # A task Omri took over (T-0247) is his: no dead check (its headless pid was stopped on purpose), no
            # stale alarm; report.md still submits it. The log is re-read before a death is acted on, so a takeover
            # written after this tick read the events still wins over the pid it stopped.
            tk = takeover_of(self.read_events(), t.id) if t.status in DEAD_CHECK_STATUSES else None
            sess = self.session_of(t) if t.status in DEAD_CHECK_STATUSES and not tk else None
            dead = self.session_dead(t, sess) if sess and not submitted else None
            if dead and (tk := takeover_of(EventLog(self.root / "events" / "events.jsonl").read(), t.id)):
                dead = None
            if tk:
                flags.append("TAKEOVER" + (" ended" if tk["ended"] else ""))
                if tk["ended"] and not submitted:
                    self.need(f"takeover:{t.id}", takeover_need_text(t.id, tk), t.id)
            if ok and submitted and t.status != "submitted":
                if t.status == "checkpoint":
                    self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--quiet")
                if t.status == "dispatched":
                    self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--quiet")
                self.scr(f"steward:{t.id}", "task", "set", t.id, "submitted", "--note", "report.md written (watcher)", "--quiet")
                self.log(f"{t.id} -> submitted (report.md written)")
                flags.append("SUBMITTED")
                self.unflag(f"nowork:{t.id}")                         # a submission ends the no-work episode (T-0187)
            elif ok and dead:
                flags.append(f"DEAD pid {sess['pid']}")
                key = f"dead:{t.id}:{sess['pid']}"
                # T-0187: a session that printed the credit-exhaustion line never ran — not a death, not an attempt
                credit = credits_line(t.dir / sess["transcript"], t.dir / (sess["transcript"] + ".err")) if sess.get("transcript") else None
                if credit:
                    flags.append("CREDITS")
                    if self.flag(key) and not self.credit_death(t, sess, credit, now):
                        self.unflag(key)                                  # retried next tick
                elif (jid := self.waiting_job(t)):
                    # the scriber handed off to a job (scr/jobs.py): the session ended ON PURPOSE; the task waits for the
                    # job without a slot and the jobs tick resumes the session when it ends
                    flags.append(f"WAITING {jid}")
                    if self.flag(f"waiting:{t.id}:{jid}"):
                        self.log(f"{t.id} session ended with WAITING: job {jid} -> waiting")
                        if self.scr(f"steward:{t.id}", "task", "set", t.id, "waiting", "--note", f"waiting on job {jid}", "--quiet") is None:
                            self.unflag(f"waiting:{t.id}:{jid}")
                else:
                    requeued = False
                    if self.flag(key):
                        self.log(f"{t.id} session dead: {dead} -> returned")
                        self.scr(f"steward:{t.id}", "event", "system", "--name", "session_dead", "--task", t.id, "--note", f"pid {sess['pid']}: {dead}"[:300], "--quiet")
                        if self.scr(f"steward:{t.id}", "task", "set", t.id, "returned", "--note", f"session died: {dead}"[:300], "--quiet") is None:
                            self.unflag(key)                              # retried next tick
                        else:
                            requeued = self.requeue_no_work(t, sess, ckpt_now, dead)
                            if not requeued:
                                requeued = self.relaunch_dead(t, dead)
                    if not requeued:
                        self.need(f"dead:{t.id}", f"{t.id} session died ({dead}) — returned; attempts spent ({t.rec.get('attempts')}/"
                                  f"{(t.rec.get('budget') or {}).get('max_attempts', 3)}); raise budget.max_attempts and set ready, or decide", t.id)
            elif ok and t.status in {"in_progress", "checkpoint"}:
                if t.status == "in_progress":
                    self.scr(f"steward:{t.id}", "task", "set", t.id, "checkpoint", "--quiet")
                self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--quiet")
            if idle >= self.stale and not dead and not tk:
                flags.append(f"STALE {idle}m")
                alive = f"pid {sess['pid']} is alive" if sess else "no recorded pid to check"
                if not sess:                # 2026-10-14: a live pid building Lean for an hour is not Omri's problem
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

        if ok:
            self.jobs_tick(now)

        tasks = self.store.load_all()
        review = [t for t in tasks.values() if t.status == "submitted"]
        decided: list[dict] = []
        held = self.frozen_held(tasks)
        for t in review:
            # A verdict the review wrote but could not execute (T-0061: `scr` denied, `escalated` sat 35 h)
            # is applied here; a task whose decision is applied needs no review session.
            if self.auto_decide and ok and self.apply_decision(t, decided):
                continue
            # T-0274: a review Omri took over is his while the takeover stands (its `review:` flag holds, nothing is
            # relaunched); once it ended with no Decision applied above, the flag goes ONCE, so the next tick
            # launches a fresh review — no hand edit of watch.state
            if (rt := review_takeover(self.read_events(), t.id)) and not rt["ended"]:
                continue
            rev = t.dir / "review.md"
            written = any(read_decision(rev.read_text(encoding="utf-8", errors="replace"))) if rev.exists() else False
            if ok and rt and not written and self.flag(f"review_takeover_ended:{t.id}:{rt['ts']}"):
                self.unflag(f"review:{t.id}")
                self.log(f"{t.id} review takeover ended with no Decision: review:{t.id} cleared, the next tick relaunches the review")
                continue
            # T-0187: a review session that printed the credit-exhaustion line never reviewed; its flag goes, so a
            # fresh review starts once the credit pause is over (never while it stands)
            tr = self.newest_review_transcript(t)
            if ok and tr and f"review:{t.id}" in self.flags and (line := credits_line(tr, tr.with_name(tr.name + ".err"))):
                if self.flag(f"credits_review:{t.id}:{tr.name}"):
                    self.log(f"{t.id} review session out of usage credits ({tr.name}): review relaunched after the pause")
                    self.unflag(f"review:{t.id}")
                    self.credits_seen(t.id, line, now)
            # a frozen workload's task gets no review (T-0272); its `review:` flag stays unset for the thaw
            if self.auto_review and ok and t.id not in held and not self.credits_paused(now) and self.flag(f"review:{t.id}"):
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
                if task_frozen(self.store, t):          # T-0272: held for the thaw; `land:` stays unset
                    continue
                # 2026-10-14: a landing runs as a detached job (scr/jobs.py), never inside the tick: a 2 h test run used
                # to freeze dispatch, reviews and the whole watcher for 2 h (T-0058 on 2026-10-14 15:51)
                if self.land_via_job(t) == "merged":
                    merged_now += 1
        self.retire_refused_needs(tasks)

        if ok:
            self.hygiene_pass()
        self.broken_ref_check()

        if self.auto_intake and ok:
            nightly = datetime.now().hour == 22 and self.flag(f"intake:{datetime.now():%Y-%m-%d}")
            if merged_now or nightly:
                self.log(f"intake run ({'nightly' if nightly else f'{merged_now} merge(s)'})")
                from .launch import intake
                for line in intake(self.root):
                    self.log(f"  intake: {line}")

        if ok and (self.auto_dispatch or self.auto_review or self.auto_land):
            self.log_frozen(self.store.load_all())
        pause = self.credits_paused(now)
        if pause:
            self.need("credits", f"Out of usage credits (since {pause['since']:%Y-%m-%d %H:%M}Z): auto-dispatch and reviews are paused; "
                      "the sessions that hit it went back to ready without spending an attempt. Resumes by itself at "
                      f"{pause['until']:%H:%M}Z (that tick's dispatch is the probe; another credit death pauses again), or now: "
                      "top up at claude.ai/settings/usage, then Pause + Resume on the page, or "
                      "`scr --actor omri event system --name credits_resumed`")
        if self.auto_dispatch and ok and pause:
            self.log("auto-dispatch held: out of usage credits")
        elif self.auto_dispatch and ok:
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
                elif out is not None:
                    self.statements_held_need()

        self.publish_needs(datetime.now(timezone.utc))      # before the push, so it carries the watcher's own lines too
        self.notify_cards()
        self.save_flags()
        journal(self.root, "steward-watch", "tick: records changed", push=self.push_journal)
        return {"ok": ok, "rows": self.rows, "review": [t.id for t in review], "decided": decided, "escalated": self.escalated,
                "timed_out": self.timed_out, "needs": self.needs}

    # ---- statement tasks held by auto-dispatch (T-0302) ---------------------------------------------------------------
    def statements_held_need(self) -> None:
        """After a dispatch step that dispatched nothing: ONE needs line (key `statements_held`) naming the ready
        may_touch_statements tasks `scr dispatch` held back, either because this watcher runs without
        --allow-statements or because limits.yaml statement_window is closed now. Recomputed every tick, so it
        clears itself once a dispatch happens or no ready task is held. Frozen tasks are the freeze's, not this."""
        from .cli import statement_window_open
        from .graph import Graph
        from .score import rank_ready
        window = self.limits.get("statement_window")
        if self.allow_statements and statement_window_open(window):
            return
        tasks = self.store.load_all()
        ready = [r["task"] for r in rank_ready(Graph.build(tasks), self.store.config("weights"), "scribers")]
        held = [tid for tid in ready if (tasks[tid].rec.get("scope") or {}).get("may_touch_statements")
                and not task_frozen(self.store, tasks[tid])]
        if not held:
            return
        why = (f"statement_window {window} is closed now; they dispatch when it opens (or widen it in config/limits.yaml)"
               if self.allow_statements else
               "the watcher runs without --allow-statements and config/limits.yaml sets no statement_window: "
               "set statement_window (e.g. 00:00-23:59) and restart the watcher")
        self.need("statements_held", f"{len(held)} statement task(s) held ({', '.join(held)}): {why}")

    # ---- the freeze switch (T-0272) ---------------------------------------------------------------------------------
    def frozen_held(self, tasks: dict) -> set[str]:
        """The ids of the submitted tasks whose workload is frozen (`frozen: true` in config/workloads.yaml,
        ops.is_frozen): auto-review skips them."""
        return {tid for tid, t in tasks.items() if t.status == "submitted" and task_frozen(self.store, t)}

    def log_frozen(self, tasks: dict) -> None:
        """ONE log line per tick for what the freeze holds: the ready, submitted and accepted tasks of each frozen
        workload, as a count per workload ("frozen: 3 scriptorium tasks held (2 ready, 1 submitted)"), never a
        line per task. Auto-dispatch skips them inside `scr dispatch`, auto-review and auto-land in tick()."""
        per: dict[str, dict[str, int]] = {}
        for t in tasks.values():
            if t.status in {"ready", "submitted", "accepted"} and task_frozen(self.store, t):
                c = per.setdefault(workload_for(self.root, self.store, t).name, {})
                c[t.status] = c.get(t.status, 0) + 1
        if per:
            self.log("frozen: " + "; ".join(
                f"{sum(c.values())} {name} tasks held ({', '.join(f'{n} {st}' for st, n in c.items())})" for name, c in per.items()))

    # ---- the daily hygiene pass (docs/hygiene-protocols.md §3, §10; T-0202) ------------------------------------------
    def hygiene_pass(self) -> Path | None:
        """Once per local date (flag `hygiene:<date>`, like `intake:<date>`): `sweep --all` as a classification
        (fetch, no commit), `reports/hygiene/<date>.md`, and one `hygiene_digest` event. Informational only: no
        needs line, chore or task comes from it. A failure leaves the flag unset, so the next tick retries."""
        day = f"{datetime.now():%Y-%m-%d}"
        key = f"hygiene:{day}"
        if key in self.flags:
            return None
        try:
            rows = sweep(self.root, dry_run=True, all_workloads=True, fetch=True)
            path = hygiene_digest(self.root, rows, day)
        except Exception as e:  # noqa: BLE001 — a hygiene failure is a log line, never a tick crash
            self.log(f"hygiene pass FAILED: {e}")
            return None
        self.flags.add(key)
        rel = path.relative_to(self.root).as_posix()
        counts = ", ".join(f"{k} {n}" for k in HYGIENE_KINDS if (n := sum(1 for r in rows if r["kind"] == k)))
        EventLog(self.root / "events" / "events.jsonl").append("system", name="hygiene_digest", actor="steward-watch",
                                                                note=f"{rel}: {counts or 'no worktrees'}")
        self.log(f"hygiene digest {rel}: {counts or 'no worktrees'}")
        return path

    # ---- a broken ref under refs/heads (T-0244) -----------------------------------------------------------------------
    def broken_ref_check(self) -> list[str]:
        """Every tick: the refs git ignores under refs/heads in the checkout and each workload repo. A file like
        `.git/refs/heads/main (1)` (a sync conflict copy) made git warn on every branch listing and, until T-0244,
        broke every spawn of a new branch. Each one is ONE standing needs line (key `broken_ref:<repo>:<ref>`, so
        the card and its push are deduped) naming the file and the hand step, and ONE `broken_ref` system event
        the first time it is seen (flag); the flag is dropped when the ref is gone. Never removes anything."""
        repos = {self.root.resolve()}
        for name in (self.store.config("workloads") or {}):
            try:
                repos.add(workload(self.root, self.store, name).repo)
            except Exception:  # noqa: BLE001 — a bad workload entry is someone else's needs line
                pass
        seen: list[str] = []
        for repo in sorted(repos):
            if not (repo / ".git").exists():
                continue
            g = Git(repo)
            refs = g.broken_refs()
            if not refs:
                continue
            gd = g.common_dir()
            for ref in refs:
                key = f"broken_ref:{repo.name}:{ref}"
                seen.append(key)
                loose = gd / ref
                where = str(loose) if loose.exists() else f"{gd / 'packed-refs'} (line `{ref}`)"
                text = (f"Broken git ref `{ref}` in {repo.name}: git ignores it and warns on every branch listing. "
                        f"By hand: move {where} aside under {gd / '_to_delete'}/ (never delete it)")
                self.need(key, text)
                if self.flag(key):
                    EventLog(self.root / "events" / "events.jsonl").append("system", name="broken_ref", actor="steward-watch", note=text)
                    self.log(f"broken ref: {repo.name} {ref}")
        for k in [k for k in self.flags if k.startswith("broken_ref:") and k not in seen]:
            self.unflag(k)
        return seen

    # ---- out of usage credits is not a session death (T-0187) ------------------------------------------
    def credits_paused(self, now: datetime) -> dict | None:
        return credits_pause(self.read_events(), now, float(self.limits.get("credits_pause_minutes", CREDITS_PAUSE_MINUTES)))

    def credits_seen(self, tid: str, line: str, now: datetime) -> None:
        """A session (scriber or review) printed the credit-exhaustion line: ONE `credits_exhausted` event opens the
        pause; a hit while the pause stands (the same tick's second session, a later one) raises nothing more."""
        self.credit_hits.append(tid)
        if self.credits_paused(now):
            return
        mins = float(self.limits.get("credits_pause_minutes", CREDITS_PAUSE_MINUTES))
        self.log(f"OUT OF USAGE CREDITS ({tid}: {line}) — auto-dispatch and reviews paused for {mins:g} min or until resumed")
        self.scr("steward-watch", "event", "system", "--name", "credits_exhausted", "--note",
                 f"{tid}: {line}; auto-dispatch paused (credits_pause_minutes {mins:g}); resume: credits_resumed event or the page's Resume"[:300],
                 "--data", json.dumps({"task": tid}), "--quiet")
        self.events = None

    def credit_death(self, t, sess: dict, line: str, now: datetime) -> bool:
        """A running task whose session printed the credit-exhaustion line goes back to `ready` with its attempt
        refunded (lifecycle.requeue), and the pause opens (credits_seen). No session_dead event, no relaunch from
        here: dispatch stays the normal path. False when the record could not be changed (retried next tick)."""
        note = f"out of usage credits (pid {sess['pid']}): not an attempt; ready again"
        try:
            done = requeue(self.store, EventLog(self.root / "events" / "events.jsonl"), t.id, note, actor=f"steward:{t.id}", refund=True)
        except Exception as e:  # noqa: BLE001
            self.log(f"{t.id} credit death: requeue FAILED: {e}")
            return False
        self.events = None
        from .cli import refresh            # local import: cli imports this module
        refresh(self.root, quiet=True)
        self.log(f"{t.id} session out of usage credits (pid {sess['pid']}) -> ready, attempt refunded")
        self.credits_seen(t.id, line, now)
        return done

    def relaunch_dead(self, t, dead: str) -> bool:
        """2026-10-14: a session that died (pid gone, no report) is relaunched from its checkpoint while the task has
        attempts left (budget.max_attempts, default 3) — T-0288 died twice and sat `returned` for a day. The scriber's
        brief reads log.md, where the [steward] line says it is a relaunch."""
        attempts = int(t.rec.get("attempts") or 0)
        max_attempts = int((t.rec.get("budget") or {}).get("max_attempts") or 3)
        if attempts >= max_attempts:
            return False
        return self.autofix(t, "dead", f"session died ({dead[:200]}); relaunch from the branch's last checkpoint, attempt {attempts + 1}/{max_attempts}", returned=True)

    def requeue_no_work(self, t, sess: dict, ckpt_now: bool, dead: str) -> bool:
        """A task just returned by a session death that did no work (no checkpoint commit since its launch, not this
        tick's either, and no `[scriber]` line after the launcher's) goes back to `ready` — once per episode: the flag
        `nowork:<id>` stays until the task is submitted or a session of it does work, so a second no-work death
        leaves it `returned` for Omri. The attempt stays counted (max_attempts still bounds it). Only on evidence:
        the session's transcript must exist (the launcher always creates it; without it nothing says the session
        never ran), and a session claude killed at its background-task ceiling (DEAD_LINE) or one that wrote
        report.md did work."""
        key = f"nowork:{t.id}"
        log_md = t.dir / "log.md"
        text = log_md.read_text(encoding="utf-8", errors="replace") if log_md.exists() else ""
        rep = t.dir / "report.md"
        reported = rep.exists() and rep.read_text(encoding="utf-8", errors="replace").strip() != self.report_tpl
        unseen = not sess.get("transcript") or not (t.dir / sess["transcript"]).exists()
        if unseen or reported or DEAD_LINE in dead or ckpt_now or did_work(self.read_events(), t.id, sess.get("ts"), text, sess["pid"]):
            self.unflag(key)
            return False
        if not self.flag(key):
            self.log(f"{t.id} died again without doing any work: stays returned")
            return False
        try:
            done = requeue(self.store, EventLog(self.root / "events" / "events.jsonl"), t.id,
                           f"session pid {sess['pid']} did no work (no checkpoint, no [scriber] line): ready again, once", actor=f"steward:{t.id}")
        except Exception as e:  # noqa: BLE001
            self.log(f"{t.id} no-work requeue FAILED: {e}")
            self.unflag(key)
            return False
        self.events = None
        from .cli import refresh
        refresh(self.root, quiet=True)
        self.log(f"{t.id} returned session did no work -> ready (once)")
        return done

    @staticmethod
    def newest_review_transcript(t) -> Path | None:
        trs = sorted(t.dir.glob("review-session-*.txt"))
        return trs[-1] if trs else None

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
        (T-0063). Nothing is escalated: a failed dispatch needs no one. Returns how many were moved.
        No attempt was charged for such a dispatch (the launch charges it, T-0127), so the cycle costs no budget;
        what it needs is the launcher fixed: see dispatch_rounds_needs, run at the end."""
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
        if n:
            self.events = None                                  # the dispatch_timeout events just written count below
        self.dispatch_rounds_needs(tasks)
        return n

    def dispatch_rounds_needs(self, tasks: dict) -> None:
        """ONE needs line per task whose launch keeps failing (T-0127): `dispatch_rounds` >= dispatch_timeout_rounds
        (limits.yaml) for a `ready` or `dispatched` task. It names the task, the round count and the launcher's
        reason (`launcher_reason`, read from the tail of reports/steward_watch.log), and says the redispatch is
        automatic, so the one action is fixing the launcher. The key `dispatch_rounds:<id>` is stable for the whole
        streak: the line is raised on every tick while the streak stands (the needs file is rewritten each tick)
        but it is one card, never one per round; the log gets one line when it first appears. A scriber launch
        resets the count and the line goes. The task is not escalated and stays in the ranking."""
        limit = int(self.limits.get("dispatch_timeout_rounds", DISPATCH_TIMEOUT_ROUNDS))
        if limit <= 0:
            return
        events, log_text = self.read_events(), None
        for t in tasks.values():
            key = f"dispatch_rounds:{t.id}"
            k = dispatch_rounds(events, t.id) if t.status in {"ready", "dispatched"} else 0
            if k < limit:
                self.unflag(key)
                continue
            if log_text is None:
                log_text = self.log_tail()
            reason = launcher_reason(log_text, t.id) or "no launcher line names it in the log tail"
            self.need(key, f"{t.id} was dispatched {k} times in a row and never launched (dispatch_timeout_rounds {limit}); "
                           f"the launcher says: {reason}. It keeps being redispatched automatically and no attempt is "
                           "charged — fix the launcher, not the task (reports/steward_watch.log)", t.id)
            if self.flag(key):
                self.log(f"{t.id} dispatch timed out {k} rounds in a row without a launch: {reason}")

    def log_tail(self) -> str:
        """The last LAUNCH_LOG_TAIL_BYTES of LOG_FILE ('' when it does not exist)."""
        p = self.root / LOG_FILE
        try:
            with p.open("rb") as f:
                f.seek(max(0, p.stat().st_size - LAUNCH_LOG_TAIL_BYTES))
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    # ---- a stuck task leaves the slot (T-0092) --------------------------------------------------------
    def escalation_checks(self, now: datetime, tasks: dict) -> int:
        """The one rule of agents/_common.md applied to every slot-holder: a task that cannot progress is
        escalated, never held. Three conditions, all pure functions above: `submitted` with no Decision written
        after review_stale_hours (T-0075's relaunch, once landed, runs before this and its cap replaces the
        clock); `in_progress`/`checkpoint` past its own `budget.max_wall_hours` (T-0121: budgets are hard; the
        submission and dead branches of the tick run first, so a finished session is submitted and a dead one
        returned, never escalated); `in_progress`/`checkpoint` with the newest blocking HELP line unanswered after help_stale_hours
        (once T-0069 lands that scriber has exited with `WAITING:` and the task is `waiting`, not escalated;
        `waiting` is T-0069's transition and is not made here). Returns how many tasks were escalated. One
        escalation per episode: the persisted flag `slot_freed:<id>:<since>` names the episode (the submission
        time, the in_progress start, the HELP line's first sighting), and only SLOT_STATUSES are looked at, so a task already
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
                since = submitted_since(events, t.id)
                if rt := review_takeover(events, t.id):
                    if not rt["ended"]:
                        continue            # Omri is in the review (T-0274): no review_stale while it stands
                    since = max(since, _event_ts({"ts": rt["ended"]}) or since)   # the relaunched review's clock
                if (retry := self.review_retry_since(t.id)) and since and retry > since:
                    since = retry           # the clock of the re-run review (rerun_review), not of the submission
                row = submitted_stale(t.status, text, since, review_h, now)
                if not row:
                    continue
                tr = self.newest_review_transcript(t)
                if tr and credits_line(tr, tr.with_name(tr.name + ".err")):
                    continue                # the review never ran (T-0187): relaunched after the credit pause, not escalated
                if self.rerun_review(t, f"no Decision after {row['age_h']} h (review_stale_hours {review_h:g})"):
                    continue                # 2026-10-14: a review that died is run again, not handed to Omri
                transcripts = sorted(p.name for p in t.dir.glob("review-session-*.txt"))
                reason = (f"submitted for {row['age_h']} h with no Decision written in review.md "
                          f"(review_stale_hours {review_h:g}); the review did not finish")
                action = ("read the review transcript, then decide in the gate card (accept / return), or re-review: "
                          f"delete `review:{t.id}` from .scr/watch.state.txt after `scr task set {t.id} returned` + `ready`")
            elif t.status in {"in_progress", "checkpoint"} and takeover_of(events, t.id):
                continue                            # under takeover Omri is in the session: his HELP lines and wall hold nothing
            elif t.status in {"in_progress", "checkpoint"} and str(t.rec.get("title", "")).startswith("hotfix:"):
                continue                            # 2026-10-14: a hotfix record is open while Omri edits the checkout; no wall
            elif t.status in {"in_progress", "checkpoint"} and (row := wall_stale(self.started_at(t.id), hours := wall_hours(t.rec), now)):
                # T-0121: out of budget is stuck. The running loop raised this episode's `budget_wall` earlier this
                # tick, so the trail reads budget_wall -> escalated -> slot_freed. `escalated` leaves only for
                # omri_queue (scr/lifecycle.py), so the redispatch route needs --force (it is logged).
                sess = self.session_of(t)
                if len([k for k in self.flags if k.startswith(f"autofix:{t.id}:wall:")]) < self.AUTOFIX_MAX:
                    # 2026-10-14: the first two exhaustions extend the wall by the same budget and relaunch from the
                    # checkpoint (the wall counts waiting time too, T-0342); the third is Omri's
                    if sess:
                        wl = workload_for(self.root, self.store, t)
                        self.stop_session(t, sess["pid"], task_worktree(wl, t), t.rec.get("branch") or "task/" + t.id, created=sess.get("created"))
                    new_hours = hours * 2
                    self.scr(f"steward:{t.id}", "task", "field", t.id, "budget.max_wall_hours", str(new_hours), "--note", "auto-fix: wall extended", "--quiet")
                    if self.autofix(t, "wall", f"wall budget exhausted ({row['age_h']} h > {hours:g} h); extended to {new_hours:g} h; "
                                              f"relaunch from the branch's last checkpoint and finish, or write report.md with what is done"):
                        continue
                transcripts = [sess["transcript"]] if sess and sess.get("transcript") else sorted(p.name for p in t.dir.glob("session-*.txt"))[-1:]
                reason = (f"wall budget exhausted: {row['age_h']} h in progress since "
                          f"{row['since'].astimezone(timezone.utc):%Y-%m-%dT%H:%MZ} > max_wall_hours {hours:g}")
                action = (f"extend the wall with `scr task field {t.id} budget.max_wall_hours N`, then `scr task set {t.id} returned --force` "
                          "+ `ready` so it is redispatched, or decide in the gate card")
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
        submitted = {t.id for t in tasks.values() if t.status == "submitted"}
        for k in [k for k in self.flags if k.startswith("review_retry:") and k.split(":", 2)[1] not in submitted]:
            self.unflag(k)
        return n

    def _escalate(self, t, since: datetime, reason: str, action: str, transcripts: list[str]) -> bool:
        """submitted / in_progress / checkpoint -> escalated as steward:<id>, then ONE `slot_freed` event whose note
        is the needs-you card: the one action for Omri, the reason, where the work is, the transcript(s).
        An in_progress / checkpoint task whose launched session is alive is first checkpoint-committed HERE (not
        by the tick's earlier pass, so nothing written since is lost), then that pid is stopped with run.kill:
        otherwise auto-dispatch fills the freed slot and cap + 1 sessions run, and the old session's
        `task set submitted` is refused unexplained (T-0124, T-0092 review item 3). Only the pid session_of
        returns is ever stopped, and never once another process has reused it (T-0147); a `submitted` task has no scriber session (its review is T-0075's). The
        escalation never depends on the stop: a failed checkpoint or kill is said in the note, not fatal."""
        key = f"slot_freed:{t.id}:{since.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
        if key in self.flags:
            return False
        wl = workload_for(self.root, self.store, t)
        wt = task_worktree(wl, t)
        branch = t.rec.get("branch") or "task/" + t.id
        where = f"work: {wt} on {branch}" + ("" if wt.exists() else " (worktree missing)")
        if t.status in DEAD_CHECK_STATUSES and (sess := self.session_of(t)):
            where = f"{self.stop_session(t, sess['pid'], wt, branch, created=sess.get('created'))}; {where}"
        tr = ", ".join(f"tasks/{t.dir.name}/{x}" for x in transcripts) if transcripts else "no transcript recorded"
        note = (f"stuck: {reason}; escalated by the watcher — a task that cannot progress is escalated, never held "
                f"(agents/_common.md); {where}; transcript(s): {tr}")
        if self.scr(f"steward:{t.id}", "task", "set", t.id, "escalated", "--note", note[:900], "--quiet") is None:
            self.need(f"escalate_failed:{t.id}", f"{t.id} is stuck ({reason}) but could not be escalated — see reports/steward_watch.log", t.id)
            return False
        self.flags.add(key)
        self.unflag(f"review:{t.id}")          # it left `submitted`: a later re-submission gets a fresh review
        for k in [k for k in self.flags if k.startswith(f"review_retry:{t.id}:")]:
            self.unflag(k)
        card = (f"ACTION for Omri: {action}. Why: {reason}. Its slot is free (escalated never counts against the cap); "
                f"{where}. Transcript: {tr}")
        self.scr(f"steward:{t.id}", "event", "system", "--name", "slot_freed", "--task", t.id, "--note", card[:900], "--quiet")
        self.log(f"{t.id} stuck -> escalated ({reason}); slot freed")
        self.escalated.append({"task": t.id, "since": since.isoformat(), "reason": reason})
        for r in self.rows:
            if r["task"] == t.id:
                r["flags"] = (r["flags"] + " ESCALATED").strip()
        return True

    def stop_session(self, t, pid: int, wt: Path, branch: str, created: int | None = None) -> str:
        """Checkpoint-commit the worktree, then stop the launched session `pid` (T-0124). Returns the clause for
        the escalation note; never raises. `created` is the creation time the launch recorded: a live process
        with that pid but another creation time is not the session, and is never killed (T-0147)."""
        if not pid_alive(pid):
            return f"session pid {pid} was already gone"
        if self.pid_reused({"pid": pid, "created": created}):
            self.log(f"{t.id} session pid {pid} was reused by another process: not stopped")
            return f"session pid {pid} was already gone (pid reused by another process)"
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
        if verdict == "escalated" and additions_only_gate1(text):
            verdict = "accepted"          # 2026-10-14 (rule 2): additions-only Gate 1 with all checks passing is an acceptance
            extra = ["--force-accept"]
        else:
            extra = []
        digest = review_digest(text)
        note = f"{DECISION_NOTE} [review.md {digest}]"
        prior = next((e for e in self.read_events() if e.get("kind") == "state" and e.get("task") == t.id
                      and f"[review.md {digest}]" in (e.get("note") or "")), None)
        if prior:
            if self.rerun_review(t, f"review.md is the review already applied ({prior.get('to')} at {prior.get('ts')})"):
                return False            # 2026-10-14: archived; the tick relaunches the review
            self.need(f"decision_stale:{t.id}", f"{t.id} is submitted again but review.md is the review already applied "
                      f"({prior.get('to')} at {prior.get('ts')}) — a fresh review is needed", t.id)
            return False
        if self.scr(f"steward:{t.id}", "task", "set", t.id, verdict, "--note", note, *extra, "--quiet") is None:
            self.need(f"decision_unapplied:{t.id}", f"{t.id} decision {verdict} from review.md could not be applied — see reports/steward_watch.log", t.id)
            return False
        self.log(f"{t.id} decision applied: {verdict}")
        decided.append({"task": t.id, "decision": verdict})
        return True

    LAND_JOB_PREFIX = "land-"

    def land_jobs(self, tid: str) -> list[dict]:
        return [j for j in jobs.list_jobs(self.root) if j.get("task") == tid and str(j.get("name") or "").startswith(self.LAND_JOB_PREFIX)]

    def land_via_job(self, t) -> str | None:
        """The landing of an accepted task as a detached job: `scr --actor steward:<id> land <id> --merge` (ops.land_any:
        the scriptorium landing with its test run, or the QI push/PR/merge), submitted once per attempt and applied when
        it ends (apply_land_job). Returns "merged" when a finished job landed the task this tick, "submitted" when a job
        was started, "failed" when a finished job was applied as a failure, None otherwise (job still running, nothing
        to do, or the task is held back by a known-failed tip)."""
        active = [j for j in self.land_jobs(t.id) if j["status"] in jobs.ACTIVE]
        if active:
            return None
        pending = [j for j in self.land_jobs(t.id) if j["status"] in jobs.FINISHED and not j.get("applied")]
        if pending:
            return self.apply_land_job(t, pending[-1])
        scriptorium = is_scriptorium_task(self.root, self.store, t)
        wl = workload_for(self.root, self.store, t)
        wt = task_worktree(wl, t)
        tip, scope_sig, failed = None, "", set()
        if scriptorium:
            if not wt.exists():                 # no worktree: ops.land_any decides in-process, no tests (T-0158)
                return "merged" if self.land_scriptorium_task(t) else None
            sc = t.rec.get("scope") or {}
            scope_sig = hashlib.sha1(json.dumps([sc.get("files") or [], sc.get("mission") or ""], sort_keys=True).encode("utf-8")).hexdigest()[:8]
            failed = {k for k in self.flags if k.startswith(f"land_failed:{t.id}:")}
            g, mg = Git(wt), Git(self.root)
            tip = f"{g.head()}+{scope_sig}" if not g.dirty() else None
            if tip and f"land_failed:{t.id}:{tip}" in failed and not mg.is_ancestor(f"task/{t.id}", wl.main):
                if not [k for k in self.flags if k.startswith(f"autofix:{t.id}:land:")]:
                    if self.autofix(t, "land", f"landing of task/{t.id} failed at {tip[:8]} (see the land_failed event); merge the trunk in, make the tests pass, commit, resubmit"):
                        for k in failed:
                            self.unflag(k)
                        self.unflag(f"land:{t.id}")
                        return "failed"
                self.need(f"land_failed:{t.id}", f"{t.id} accepted but its landing failed at {tip[:8]} (event land_failed) — fix task/{t.id} (a new commit or a wider scope retries) or merge it into {wl.main} by hand", t.id)
                self.unflag(f"land:{t.id}")
                return None
        elif not wt.exists():
            self.need(f"auto_land_failed:{t.id}", f"{t.id} accepted but its worktree {wt} is missing — nothing to land", t.id)
            return None
        if not self.flag(f"land:{t.id}"):
            return None
        from .ops import land_test_timeout
        minutes = (land_test_timeout(self.store) + 900) // 60 if scriptorium else 90
        cmd = service_argv(self.root) + ["--root", str(self.root), "--actor", f"steward:{t.id}", "land", t.id, "--merge"]
        try:
            job = jobs.submit(self.root, cmd, task=t.id, name=f"{self.LAND_JOB_PREFIX}{t.id}", timeout=f"{minutes}m", actor="steward-watch")
        except Exception as e:  # noqa: BLE001
            self.log(f"{t.id} landing job not submitted: {e}")
            self.unflag(f"land:{t.id}")
            return None
        job["land_tip"], job["land_scope_sig"], job["land_scriptorium"] = tip, scope_sig, scriptorium
        jobs.save(self.root, job)
        self.log(f"{t.id} landing submitted as job {job['id']} ({'scriptorium: commit, merge trunk, tests, merge' if scriptorium else 'push, PR, merge'}; {minutes} min)")
        return "submitted"

    def apply_land_job(self, t, job: dict) -> str:
        """A finished landing job: on rc 0 `scr land` already recorded the merge (status merged, events, cascade) and
        the worktree is retired here; otherwise the job log's FAILED / REFUSED / merge-failed line is the reason and
        the failure takes the same route as before (land_failed for the scriptorium, the QI conflict / scope / push
        branches), i.e. auto-fix first, Omri third."""
        job["applied"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        jobs.save(self.root, job)
        try:
            text = Path(job["log"]).read_text(encoding="utf-8", errors="replace") if job.get("log") else ""
        except OSError:
            text = ""
        out = [l.rstrip() for l in text.splitlines() if l.strip()][-40:]
        for line in out[-12:]:
            self.log(f"  land[{job['id']}]: {line[:200]}")
        scriptorium = bool(job.get("land_scriptorium"))
        scope_sig = job.get("land_scope_sig") or ""
        failed = {k for k in self.flags if k.startswith(f"land_failed:{t.id}:")}
        if job["status"] == "done" and job.get("rc") == 0:
            try:
                st = self.store.get(t.id).status
            except Exception:  # noqa: BLE001
                st = None
            if st != "merged":
                self.log(f"{t.id} landing job {job['id']} exited 0 but the task is {st}; treated as a failure")
            else:
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
                self.log(f"{t.id} landed by job {job['id']} -> merged")
                self.events = None
                return "merged"
        why = next((l for l in out if "FAILED" in l or "REFUSED" in l or "merge failed" in l or "push failed" in l), "")
        if job["status"] == "timeout":
            reason, why = "timeout", why or f"landing job {job['id']} hit its {job.get('timeout_min')} min deadline"
        elif job["status"] == "killed":
            reason, why = "killed", why or f"landing job {job['id']} was killed ({job.get('note') or 'no note'})"
        else:
            m = re.search(r"FAILED \((\w+)\)", why)
            reason = m.group(1) if m else ("conflict" if "merge failed" in why or "conflict" in why.lower() else "scope" if "REFUSED" in why else "tests")
        res = {"ok": False, "reason": reason, "out": out or [why or f"landing job {job['id']} {job['status']}"], "tip": job.get("land_tip"), "sha": None}
        if scriptorium:
            self.land_failed(t, res, failed, scope_sig)
            return "failed"
        wl = workload_for(self.root, self.store, t)
        self.unflag(f"land:{t.id}")
        if reason == "conflict":
            fix = (f"PR merge conflict with {wl.main}: in the worktree run `git fetch origin {wl.main} && git merge origin/{wl.main}`, "
                   f"resolve every conflict (a TARGETS.md / ledger conflict: keep both sides' rows, a status cell takes the more "
                   f"advanced value), `lake build` the touched modules, rerun check_done on the frozen decls, commit the merge, "
                   f"then write report.md (add a 'Merge' line naming the resolved files) and resubmit. No new statements.")
        elif reason == "scope":
            fix = (f"landing refused for out-of-scope paths ({why[:200]}): either widen scope.files in the record "
                   f"(`scr task field {t.id} scope.files <list>`) when the task owns them, or revert the drift commit; then resubmit.")
        else:
            fix = f"landing failed ({why[:300] or reason}); fix the branch and resubmit."
        if not self.autofix(t, "land", fix):
            self.need(f"auto_land_failed:{t.id}", f"{t.id} auto-land failed twice ({(why or reason)[:160]}) — see reports/steward_watch.log", t.id)
        return "failed"

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
            if not [k for k in self.flags if k.startswith(f"autofix:{t.id}:land:")]:
                # 2026-10-14: a landing that failed before auto-fix existed gets its send-back now
                if self.autofix(t, "land", f"landing of task/{t.id} failed at {tip[:8]} (see the land_failed event); merge the trunk in, make the tests pass, commit, resubmit"):
                    for k in failed:
                        self.unflag(k)
                    self.unflag(f"land:{t.id}")
                    return False
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
        for k in [k for k in self.flags if k.startswith((f"retire_refused:{tid}:", f"retire_retry:{tid}:"))]:
            self.unflag(k)
        self.flags.add(f"retire_refused:{tid}:{code}")
        self.flags.add(f"retire_retry:{tid}:{datetime.now():%Y%m%dT%H%M%S}")   # the attempt just made (T-0209)

    def retire_refused_needs(self, tasks: dict) -> None:
        """Each flagged task that is still `merged` with its worktree present gets its retire retried at most hourly
        (T-0209); a success clears flag and line, a refusal keeps both. Anything else drops the flag."""
        for k in sorted(k for k in self.flags if k.startswith("retire_refused:")):
            _, tid, code = k.split(":", 2)
            t = tasks.get(tid)
            if t is None or t.status != "merged" or not task_worktree(workload_for(self.root, self.store, t), t).exists():
                self.unflag(k)
                for r in [r for r in self.flags if r.startswith(f"retire_retry:{tid}:")]:
                    self.unflag(r)
                continue
            code = self._retry_retire(tid, code)
            if code is None:
                continue
            self.need(f"retire_refused:{tid}", f"{tid} merged; worktree retire refused ({code}) — see log", tid)

    def _retry_retire(self, tid: str, code: str) -> str | None:
        """Re-run a refused retire once the last attempt (flag `retire_retry:<id>:<stamp>`, kept across restarts) is an
        hour old. Returns the standing refusal code, or None once the worktree is retired. The retire's lines are
        logged only when the outcome differs from the flagged code: a repeat refusal leaves no hourly trace."""
        last = [r for r in self.flags if r.startswith(f"retire_retry:{tid}:")]
        try:
            stamp = datetime.strptime(max(last).split(":", 2)[2], "%Y%m%dT%H%M%S") if last else None
        except ValueError:
            stamp = None
        if stamp is not None and datetime.now() - stamp < timedelta(hours=1):
            return code
        try:
            r = retire(self.root, tid, execute=True)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "code": "error", "out": [str(e)]}
        new = None if r["ok"] else str(r["code"])
        if new != code:
            self.log(f"{tid} retire retry: " + ("worktree retired" if new is None else f"refused ({code} -> {new})"))
            for line in r["out"]:
                self.log(f"  retire: {line}")
        if new is None:
            for k in [k for k in self.flags if k.startswith((f"retire_refused:{tid}:", f"retire_retry:{tid}:"))]:
                self.unflag(k)
            return None
        self.retire_refused(tid, new)
        return new

    def land_failed(self, t, res: dict, failed: set, scope_sig: str) -> bool:
        """A refused landing (land_scriptorium or the no-worktree land_any): ONE land_failed event per branch tip
        (+ scope), then — 2026-10-14 — the task goes back to its scriber with the failure (autofix, at most
        AUTOFIX_MAX times); only after that does the task stay accepted with a NEEDS-YOU line every tick. A lost
        worktree or an internal error is Omri's at once. Returns False."""
        why = next((l for l in res["out"] if "FAILED" in l), res["reason"]).strip()
        nxt = next((l.split("— next: ", 1)[1] for l in res["out"] if "— next: " in l), None)   # land_any's closing line
        key = f"land_failed:{t.id}:{res.get('tip') or 'unknown'}+{scope_sig}"
        for k in failed - {key}:                                        # only the current tip (+ scope) is remembered
            self.unflag(k)
        if self.flag(key):
            self.scr(f"steward:{t.id}", "event", "system", "--name", "land_failed", "--task", t.id, "--note", why[:900], "--quiet")
        self.unflag(f"land:{t.id}")
        if res.get("reason") not in {"lost_worktree", "error", "trunk", "busy"}:    # those are the checkout's, not the branch's
            tail = "\n".join(res["out"][-12:])[-1500:]
            fix = (f"landing failed ({res.get('reason')}): {why[:300]}. Merge the trunk into the branch, make the full test suite pass "
                   f"(`.venv\\Scripts\\python.exe -m pytest tests -q`, as a scr job if long), fix only what the failure names, commit, "
                   f"then resubmit with report.md stating the test result.\nLanding output tail:\n{tail}")
            if self.autofix(t, "land", fix):
                self.unflag(key)
                return False
        self.need(f"land_failed:{t.id}", f"{t.id} landing failed ({res['reason']}) — {why[:200]}" + (f"; next: {nxt}" if nxt else "") + "; see reports/steward_watch.log", t.id)
        return False

    AUTOFIX_MAX = 2

    def autofix(self, t, kind: str, note: str, returned: bool = False) -> bool:
        """2026-10-14 (Omri: "the system should be resolving it by itself"). A task the system can send back to its own
        scriber with a concrete fix note is sent back — accepted/escalated/submitted -> returned -> ready — instead of
        becoming a Needs-you card. At most AUTOFIX_MAX times per task and kind (flags `autofix:<id>:<kind>:<n>`); the
        third failure is Omri's. The note lands in log.md as a [steward] line (the scriber's brief reads it) and in a
        `auto_fix` event. Returns True when the task was requeued."""
        done = [k for k in self.flags if k.startswith(f"autofix:{t.id}:{kind}:")]
        if len(done) >= self.AUTOFIX_MAX:
            return False
        n = len(done) + 1
        line = f"auto-fix {n}/{self.AUTOFIX_MAX} ({kind}): {note}"
        try:
            with (t.dir / "log.md").open("a", encoding="utf-8") as f:
                f.write(f"\n- {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} [steward] {line}\n")
        except OSError as e:
            self.log(f"{t.id} autofix: log.md not written: {e}")
        if t.status != "returned" and not returned:
            if self.scr(f"steward:{t.id}", "task", "set", t.id, "returned", "--force", "--note", line[:300], "--quiet") is None:
                return False
        if self.scr(f"steward:{t.id}", "task", "set", t.id, "ready", "--note", f"auto-fix {n}: redispatched", "--quiet") is None:
            return False
        self.flags.add(f"autofix:{t.id}:{kind}:{n}")
        self.unflag(f"review:{t.id}")
        self.scr(f"steward:{t.id}", "event", "system", "--name", "auto_fix", "--task", t.id, "--note", line[:900], "--quiet")
        self.log(f"{t.id} {line[:160]} -> ready")
        self.events = None
        return True

    REVIEW_RETRIES = 2

    def rerun_review(self, t, why: str) -> bool:
        """A review that ended without a Decision is run again (up to REVIEW_RETRIES times per submission episode) instead
        of escalating the task to Omri: review.md is archived as review-stale-<n>.md, `review:<id>` is cleared so the
        tick relaunches the review, and the stale clock restarts from now (flag `review_retry:<id>:<n>:<stamp>`)."""
        done = [k for k in self.flags if k.startswith(f"review_retry:{t.id}:")]
        if len(done) >= self.REVIEW_RETRIES:
            return False
        n = len(done) + 1
        rev = t.dir / "review.md"
        if rev.exists():
            try:
                rev.replace(t.dir / f"review-stale-{n}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.md")
            except OSError as e:
                self.log(f"{t.id} review re-run: review.md not archived: {e}")
                return False
        self.flags.add(f"review_retry:{t.id}:{n}:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        self.unflag(f"review:{t.id}")
        self.scr(f"steward:{t.id}", "event", "system", "--name", "review_retry", "--task", t.id, "--note", f"review re-run {n}/{self.REVIEW_RETRIES}: {why}"[:600], "--quiet")
        self.log(f"{t.id} review re-run {n}/{self.REVIEW_RETRIES}: {why[:140]}")
        return True

    def review_retry_since(self, tid: str) -> datetime | None:
        """The newest review re-run stamp of `tid`, or None."""
        stamps = [k.rsplit(":", 1)[1] for k in self.flags if k.startswith(f"review_retry:{tid}:")]
        out = None
        for s in stamps:
            try:
                d = datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            out = d if out is None or d > out else out
        return out

    def waiting_job(self, t) -> str | None:
        """The job a task's scriber handed off to: a `WAITING: job J-xxxx` line among the last lines of log.md naming a
        job of this task that is still queued or running (scr/jobs.py). None otherwise."""
        log_md = t.dir / "log.md"
        if not log_md.exists():
            return None
        jid = jobs.waiting_job_in(log_md.read_text(encoding="utf-8", errors="replace"))
        if not jid:
            return None
        job = jobs.load(self.root, jid)
        if not job or job.get("task") != t.id or job.get("status") not in jobs.ACTIVE:
            return None
        return jid

    def jobs_tick(self, now: datetime) -> None:
        """The watcher's pass over detached jobs (scr/jobs.py): poll (dead wrappers -> failed, deadlines -> timeout,
        queued -> running), record job_done / job_failed once per job, and resume a `waiting` task's session with
        the result of its finished job."""
        try:
            for j in jobs.poll(self.root, now):
                self.log(f"job {j['id']} {j.get('transition')}: {jobs.describe(j)}")
        except Exception as e:  # noqa: BLE001 — a jobs failure must not stop the tick
            self.log(f"jobs poll failed: {e}")
            return
        for job in jobs.list_jobs(self.root):
            if job.get("status") not in jobs.FINISHED or job.get("notified"):
                continue
            name = "job_done" if job["status"] == "done" else "job_failed"
            note = f"{job['id']} {job.get('name')}: {job['status']}" + (f" rc {job['rc']}" if job.get("rc") is not None else "") \
                   + (f" after {jobs.seconds_of(job)} s" if jobs.seconds_of(job) is not None else "") + (f"; {job['note']}" if job.get("note") else "")
            args = ["event", "system", "--name", name, "--note", note[:300], "--quiet"]
            if job.get("task"):
                args[4:4] = ["--task", job["task"]]
            self.scr(f"steward:{job['task']}" if job.get("task") else "steward-watch", *args)
            job["notified"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            jobs.save(self.root, job)
            self.log(f"job {note}")
            if job["status"] != "done" and not str(job.get("name") or "").startswith(self.LAND_JOB_PREFIX) and self.job_needs_omri(job):
                self.need(f"job:{job['id']}", f"job {job['id']} ({job.get('name')}) {job['status']}" + (f": {job['note']}" if job.get("note") else "")
                          + f" — log: scr job tail {job['id']}", job.get("task"))
        # resume: a waiting task whose jobs are all finished gets its session back, with the results as the prompt
        for t in [t for t in self.store.load_all().values() if t.status == "waiting"]:
            mine = [j for j in jobs.list_jobs(self.root) if j.get("task") == t.id]
            if not mine:
                self.need(f"waiting_nojob:{t.id}", f"{t.id} is waiting but has no job on record — set it returned or in_progress by hand", t.id)
                continue
            if any(j["status"] in jobs.ACTIVE for j in mine):
                continue
            pending = [j for j in mine if not j.get("resumed")]
            if not pending:
                continue
            self.resume_task(t, pending, now)

    def job_needs_omri(self, job: dict) -> bool:
        """2026-10-14: a failed job is Omri's only when nobody else acts on it — not one killed on purpose (`killed by`),
        not one superseded by a later job of the same task and name, and not one of a task that is running or
        waiting (its scriber gets the result on resume)."""
        if "killed by" in (job.get("note") or ""):
            return False
        tid = job.get("task")
        if not tid:
            return True
        later = [j for j in jobs.list_jobs(self.root) if j.get("task") == tid and j.get("name") == job.get("name") and j["id"] > job["id"]]
        if later:
            return False
        try:
            st = self.store.get(tid).status
        except Exception:  # noqa: BLE001
            return True
        return st not in DEAD_CHECK_STATUSES | {"dispatched", "waiting", "submitted"}

    def resume_task(self, t, finished: list[dict], now: datetime) -> None:
        """`claude -p --resume <session>` for a waiting task whose jobs ended: the job results are the prompt. The
        session id is the job's `session` or the task's last recorded scriber launch; without one (or without claude)
        the task goes back to `returned` so the dispatcher relaunches it fresh with the results in log.md."""
        wl = workload_for(self.root, self.store, t)
        wt = task_worktree(wl, t)
        sid = next((j.get("session") for j in finished if j.get("session")), None)
        if not sid:
            for e in self.read_events():
                if e.get("task") == t.id and is_scriber_launch(e) and (e.get("data") or {}).get("session_id"):
                    sid = e["data"]["session_id"]
        lines = []
        for j in finished:
            secs = jobs.seconds_of(j)
            lines.append(f"- job {j['id']} ({j.get('name')}): {j['status']}" + (f", exit code {j['rc']}" if j.get("rc") is not None else "")
                         + (f", {secs} s" if secs is not None else "") + f". Log: {j['log']}" + (f" [{j['note']}]" if j.get("note") else ""))
            tail = jobs.tail(self.root, j["id"], 60)
            if tail:
                lines.append("  last lines of the log:\n  " + "\n  ".join(tail.splitlines()))
        summary = "\n".join(lines)
        with (t.dir / "log.md").open("a", encoding="utf-8") as f:
            f.write(f"\n- {now:%Y-%m-%dT%H:%M:%SZ} [watcher] job(s) finished; resuming the session:\n" + "\n".join("  " + l for l in lines[:len(finished)]) + "\n")
        for j in finished:
            j["resumed"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            jobs.save(self.root, j)
        if not sid or not wt.exists():
            self.log(f"{t.id} cannot resume (session {sid}, worktree exists {wt.exists()}): -> returned, results are in log.md")
            self.scr(f"steward:{t.id}", "task", "set", t.id, "returned", "--note", "job finished; no session to resume (relaunch reads log.md)", "--quiet")
            return
        brief = (f"You are resuming Scriptorium task {t.id} after the job(s) you submitted finished. Results:\n{summary}\n"
                 f"Read the full log(s) if the tail is not enough, then continue the task exactly where you stopped: your brief, "
                 f"{t.dir / 'spec.md'}, {t.dir / 'log.md'} and {t.dir / 'report.md'} are unchanged. Submit further long "
                 f"commands the same way ({self.root / '.venv' / 'Scripts' / 'scr.exe'} job submit --task {t.id} ... then `WAITING: job <id>` as the last "
                 f"line of log.md and end your turn).\n")
        bf = t.dir / f"resume-brief-{now:%Y%m%d-%H%M%S}.txt"
        bf.write_text(brief, encoding="utf-8")
        tr = t.dir / f"session-{now:%Y%m%d-%H%M%S}.txt"
        if self.scr(f"steward:{t.id}", "task", "set", t.id, "in_progress", "--note", "resumed after job(s) finished", "--quiet") is None:
            return
        try:
            ceiling = bgwait.requested(t.rec)
        except ValueError:
            ceiling = None
        pid = start_claude(wt, bf, tr, permission_mode=self.permission_mode, root=self.root,
                           env_extra=bgwait.env_for(ceiling) or None, resume=sid)
        if not pid:
            self.log(f"{t.id} resume failed: claude not on PATH; -> returned")
            self.scr(f"steward:{t.id}", "task", "set", t.id, "returned", "--note", "resume failed: claude not on PATH", "--quiet")
            return
        created = process_created(pid)
        argv = session_argv(wt, root=self.root, permission_mode=self.permission_mode, resume=sid) or []
        with (t.dir / "log.md").open("a", encoding="utf-8") as f:
            f.write(f"- {now:%Y-%m-%dT%H:%M:%S} [launcher] pid {pid}, transcript {tr}\n"
                    f"- {now:%Y-%m-%dT%H:%M:%S} [launcher] session {sid} resumed (cwd {wt.resolve()}; take over: scr takeover {t.id})\n")
        record_launch(self.root, "scriber", t.id, pid, sid, wt, argv, note=f"headless pid {pid} (resumed after job)",
                      data={"transcript": tr.name, "created": created, "resumed_jobs": [j["id"] for j in finished]})
        self.log(f"{t.id} session {sid} resumed (pid {pid}) with {len(finished)} finished job(s)")

    def review_env(self) -> dict | None:
        """The background-wait ceiling for review sessions (REVIEW_BG_WAIT_MINUTES / limits.yaml review_bg_wait_minutes)."""
        try:
            minutes = int(self.limits.get("review_bg_wait_minutes", REVIEW_BG_WAIT_MINUTES))
        except (TypeError, ValueError):
            minutes = REVIEW_BG_WAIT_MINUTES
        return bgwait.env_for(minutes) or None

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
                 f"Run every check in the FOREGROUND and wait for it; never background check_done.py or a build and end your turn "
                 f"(this headless session is killed {int(self.limits.get('review_bg_wait_minutes', REVIEW_BG_WAIT_MINUTES))} min after "
                 f"your turn ends, and a review that ends without a Decision is lost). A check you expect to take over an hour: "
                 f"`{self.root / '.venv' / 'Scripts' / 'scr.exe'} job submit --task {t.id} --name <name> -- <command>` then "
                 f"`scr job wait <J-id>` keeps it under the watcher's eye while you wait here.\n"
                 f"Use `{self.root / '.venv' / 'Scripts' / 'scr.exe'} --actor steward:{t.id} ...` for every record change; never run git in the scriptorium folder (it journals itself).\n")
        bf = t.dir / "review-brief.txt"
        bf.write_text(brief, encoding="utf-8")
        tr = t.dir / f"review-session-{datetime.now():%Y%m%d-%H%M%S}.txt"
        if not wt.exists():
            self.log(f"{t.id} cannot start review: worktree missing"); self.unflag(f"review:{t.id}"); return
        sid = new_session_id()
        pid = start_claude(wt, bf, tr, permission_mode=self.permission_mode, root=self.root, session_id=sid, env_extra=self.review_env())
        if pid:
            self.log(f"{t.id} review session started (pid {pid}, session {sid}) -> {tr}")
            self.scr(f"steward:{t.id}", "event", "system", "--name", "review_started", "--task", t.id, "--note", f"pid {pid}", "--quiet")
            record_launch(self.root, "review", t.id, pid, sid, wt, session_argv(wt, root=self.root, permission_mode=self.permission_mode,
                          session_id=sid), data={"transcript": tr.name})
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
                 f"Run `hotfix test` in the FOREGROUND and wait for it (this session is killed "
                 f"{int(self.limits.get('review_bg_wait_minutes', REVIEW_BG_WAIT_MINUTES))} min after your turn ends; a review without a Decision is lost).\n"
                 f"Use `{self.root / '.venv' / 'Scripts' / 'scr.exe'} --actor steward:{t.id} ...` for every record change; the tree journals itself.\n")
        bf = t.dir / "review-brief.txt"
        bf.write_text(brief, encoding="utf-8")
        tr = t.dir / f"review-session-{datetime.now():%Y%m%d-%H%M%S}.txt"
        sid = new_session_id()
        pid = start_claude(self.root, bf, tr, permission_mode=self.permission_mode, root=self.root, session_id=sid, env_extra=self.review_env())
        if pid:
            self.log(f"{t.id} tooling review session started (pid {pid}, session {sid}) -> {tr}")
            self.scr(f"steward:{t.id}", "event", "system", "--name", "review_started", "--task", t.id, "--note", f"pid {pid} (tooling)", "--quiet")
            record_launch(self.root, "tooling-review", t.id, pid, sid, self.root, session_argv(
                self.root, root=self.root, permission_mode=self.permission_mode, session_id=sid), data={"transcript": tr.name})
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
