"""scr — the Scriptorium command line. Every mutating command ends with `refresh`:
graph/graph.json, queues/*.md and state/STATE.json are regenerated so the tree is
verifiable at all times."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from . import bgwait, lifecycle
from .canon import ZERO
from .events import EventLog, now_iso
from .journal import journal
from .brief import brief_for
from .graph import Graph
from .queue import write_queues
from .score import rank_ready, score_task
from .state import State
from .store import KINDS, QUEUES, RepoError, Store, find_root

def _ctx(args) -> tuple[Path, Store, EventLog]:
    root = find_root(Path(args.root) if getattr(args, "root", None) else None)
    return root, Store(root), EventLog(root / "events" / "events.jsonl")


def refresh(root: Path, quiet: bool = False, note: str | None = None, schema_version: int | None = None) -> dict:
    st = State(root)
    store = Store(root)
    g = Graph.build(store.load_all())
    write_queues(root, g, store.config("weights"), store.config("limits"))
    snap = st.snapshot(note, schema_version=schema_version)
    if not quiet:
        print(f"state_root {snap['state_root'][:16]}  tasks_root {snap['tasks_root'][:12]}  graph_root {snap['graph']['graph_root'][:12]}  events {snap['events_count']}")
    return snap


def _out(x, as_json: bool):
    if as_json:
        print(json.dumps(x, indent=1, sort_keys=True, default=str))
    else:
        print(yaml.safe_dump(x, sort_keys=False, allow_unicode=True, width=110))


# ---- commands ------------------------------------------------------------------
def cmd_init(args):
    root = Path(args.root or ".").resolve()
    for d in ("tasks", "config", "graph", "events", "state", "queues", "reports/daily", "reports/weekly"):
        (root / d).mkdir(parents=True, exist_ok=True)
    log = EventLog(root / "events" / "events.jsonl")
    if not log.path.exists() or log.head()[1] == 0:
        log.append("system", name="genesis", actor=args.actor, note="scriptorium initialized")
    refresh(root, note="init")


def cmd_task_new(args):
    root, store, log = _ctx(args)
    tasks = store.load_all()
    tid = args.id or store.next_id()
    # `--max-attempts` unset -> limits.yaml default_max_attempts (3 when the key is absent); T-0125
    if args.max_attempts is None:
        args.max_attempts = int(store.config("limits").get("default_max_attempts", 3))
    rec = {
        "id": tid, "title": args.title, "queue": args.queue, "kind": args.kind, "status": "proposed",
        "workload": args.workload, "created": now_iso(),
        "scope": {"files": args.files or [], "decls": args.decls or [], "may_touch_statements": bool(args.may_touch_statements)},
        "done_when": {"mechanical": [], "judgment": []},
        "budget": {"max_attempts": args.max_attempts, "max_tokens": args.max_tokens, "max_wall_hours": args.max_wall_hours},
        "priority": {"omri_weight": args.omri_weight, "verifiability": args.verifiability, "risk": args.risk},
        "blocked_by": args.blocked_by or [], "origin": args.origin, "related": [], "supersedes": [],
        "unlocks": {"on_success": [], "on_failure": []}, "attempts": 0, "tokens_used": 0,
    }
    if args.reason:
        rec["blocked_by_reasons"] = {b: args.reason for b in rec["blocked_by"]}
    for b in rec["blocked_by"]:
        if b not in tasks:
            raise RepoError(f"blocked_by references unknown task {b}")
    if rec["origin"] and rec["origin"] not in tasks and rec["origin"] not in ("omri", "intake", "observer"):
        raise RepoError(f"origin {rec['origin']} is not a task or a root name")
    t = store.new_task(rec)
    log.append("state", task=tid, **{"from": None}, to="proposed", actor=args.actor, note=args.note)
    for b in rec["blocked_by"]:
        log.append("edge", edge="blocks", **{"from": b}, to=tid, op="add", actor=args.actor, reason=args.reason)
    if rec["origin"] in tasks:
        log.append("edge", edge="unlocks", **{"from": rec["origin"]}, to=tid, op="add", actor=args.actor)
    print(f"created {tid} at {t.dir.relative_to(root)}")
    refresh(root, args.quiet)


def cmd_task_set(args):
    root, store, log = _ctx(args)
    t = store.get(args.id)
    old = t.status
    if not lifecycle.allowed(t.rec, args.status) and not args.force:
        raise RepoError(f"{args.id}: {old} -> {args.status} is not an allowed transition (use --force to override; it is logged)")
    if args.status == "accepted" and old != "omri_queue":
        # T-0081: no acceptance without a written review. The same reading as ops.land and the watcher's
        # auto-decide (scr/review.py). Only Omri may go past it, explicitly, and that leaves an event.
        # T-0128: on every route into accepted, whatever the previous status and even under --force
        # (--force overrides the lifecycle table above, never the review); omri_queue -> accepted is
        # the gate click, where Omri's decision is the review.
        from .review import acceptance_problem, additions_only_gate1
        problem = acceptance_problem(t.dir)
        if problem:
            if getattr(args, "force_accept", False) and args.actor == "omri":
                log.append("system", name="accept_override", task=args.id, actor=args.actor,
                           note=f"{problem}; accepted anyway by Omri (--force-accept)" + (f": {args.note}" if args.note else ""))
            elif getattr(args, "force_accept", False) and args.actor.startswith("steward") and \
                    (t.dir / "review.md").exists() and additions_only_gate1((t.dir / "review.md").read_text(encoding="utf-8", errors="replace")):
                # 2026-10-14 (card-routing rule 2): the Steward's own "Gate 1, additions only, all checks pass" escalation
                # is the acceptance; the watcher applies it. Any other steward --force-accept is refused below.
                log.append("system", name="accept_override", task=args.id, actor=args.actor,
                           note=f"{problem}; Gate 1 additions-only, all mechanical checks pass: accepted by the watcher (card-routing rule 2)")
            elif getattr(args, "force_accept", False):
                raise RepoError(f"{args.id}: {old} -> accepted refused — {problem}; --force-accept is Omri's alone (--actor omri)")
            else:
                raise RepoError(f"{args.id}: {old} -> accepted refused — {problem}; write the review in "
                                f"tasks/{t.dir.name}/review.md first (Omri may override with --actor omri --force-accept --note <why>; it is logged)")
    lifecycle.apply(t.rec, args.status)
    if args.tokens:
        t.rec["tokens_used"] = int(t.rec.get("tokens_used", 0)) + args.tokens
    if (args.note or "").lower().startswith("parked"):
        t.rec["parked"] = True              # T-0232 was auto-readied by the cascade after Omri parked it (2026-10-13)
    elif args.status == "ready":
        t.rec.pop("parked", None)
    t.save()
    log.append("state", task=args.id, **{"from": old}, to=args.status, actor=args.actor, tokens=args.tokens or None,
               note=(args.note or "") + (" [forced]" if args.force else "") or None)
    if old == "dispatched" and args.status == "in_progress":        # the launch charges the attempt (T-0127)
        log.append("field", task=args.id, field="attempts", value=t.rec["attempts"], actor=args.actor)
    if args.tokens:
        log.append("field", task=args.id, field="tokens_used", value=t.rec["tokens_used"], actor=args.actor)
    # cascade: a blocked task whose blockers are now all done becomes ready; done -> closed frees dependents
    lifecycle.cascade(store, log, args.id, args.status)
    print(f"{args.id}: {old} -> {args.status}")
    refresh(root, args.quiet)


def cmd_task_brief(args):
    root, store, log = _ctx(args)
    print(brief_for(root, store, store.get(args.id)))


def cmd_task_field(args):
    root, store, log = _ctx(args)
    t = store.get(args.id)
    val = yaml.safe_load(args.value)
    if args.field == bgwait.FIELD:
        try:
            val = bgwait.parse(val)
        except ValueError as e:
            raise RepoError(str(e)) from None
        if bgwait.needs_omri(val) and args.actor != "omri":
            raise RepoError(f"{args.id}: {bgwait.FIELD} = {val} min is over {bgwait.AUTH_LIMIT_MINUTES // 60} h; "
                            "only Omri can authorize that (scr --actor omri task field ...)")
    cur = t.rec
    parts = args.field.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = val
    t.rec["updated"] = now_iso()
    t.save()
    log.append("field", task=args.id, field=args.field, value=val, actor=args.actor, note=args.note)
    refresh(root, args.quiet)


def cmd_edge(args):
    root, store, log = _ctx(args)
    tasks = store.load_all()
    if args.frm not in tasks or args.to not in tasks:
        raise RepoError("both endpoints must be existing tasks")
    t = tasks[args.to]
    if args.edge == "blocks":
        bl = t.rec.setdefault("blocked_by", [])
        if args.op == "add" and args.frm not in bl:
            bl.append(args.frm)
            if args.reason:
                t.rec.setdefault("blocked_by_reasons", {})[args.frm] = args.reason
        elif args.op == "remove":
            bl[:] = [x for x in bl if x != args.frm]
            (t.rec.get("blocked_by_reasons") or {}).pop(args.frm, None)
    elif args.edge == "unlocks":
        t.rec["origin"] = args.frm if args.op == "add" else None
    elif args.edge == "informs":
        rel = [r for r in t.rec.get("related") or [] if r.get("task") != args.frm]
        if args.op == "add":
            rel.append({"task": args.frm, "weight": args.weight, "reason": args.reason or ""})
        t.rec["related"] = rel
    elif args.edge == "supersedes":
        sp = t.rec.setdefault("supersedes", [])
        if args.op == "add" and args.frm not in sp:
            sp.append(args.frm)
        elif args.op == "remove":
            sp[:] = [x for x in sp if x != args.frm]
    t.rec["updated"] = now_iso()
    g = Graph.build(tasks)  # in-memory, includes the pending change
    cyc = g.find_cycle(g.blocks if args.edge == "blocks" else g.supersedes) if args.edge in ("blocks", "supersedes") else None
    if cyc:
        log.append("system", name="invariant_violation", actor=args.actor, data={"edge": args.edge, "from": args.frm, "to": args.to, "cycle": cyc})
        refresh(root, True)
        raise RepoError(f"refused (logged): would create a cycle {' -> '.join(cyc)}")
    t.save()
    extra = {"weight": args.weight, "reason": args.reason} if args.edge == "informs" else {"reason": args.reason}
    log.append("edge", edge=args.edge, **{"from": args.frm}, to=args.to, op=args.op, actor=args.actor, **{k: v for k, v in extra.items() if v is not None})
    print(f"{args.op} {args.edge} {args.frm} -> {args.to}")
    refresh(root, args.quiet)


def cmd_graph(args):
    root, store, log = _ctx(args)
    g = Graph.build(store.load_all())
    if args.what == "show":
        _out(g.to_json(), args.json)
    elif args.what == "hashes":
        _out(g.hashes(), args.json)
    elif args.what == "analyze":
        errs, warns = g.check_invariants()
        lins = g.longest_into()
        ms = [t.id for t in g.tasks.values() if t.rec.get("kind") == "milestone" and t.is_open]
        report = {
            "invariant_errors": errs, "warnings": warns,
            "milestones": {m: {"longest_chain_attempts": round(lins.get(m, 0.0), 2), "open_ancestors": len([a for a in g.tasks if m in g.descendants(a) and g.tasks[a].is_open])} for m in ms},
            "hubs": g.hubs(int(args.min_in)),
            "chains": g.chains(),
            "orphans": [w.split(":")[0] for w in warns if "orphan" in w],
            "unlock_yield": g.unlock_yield(),
            "conflicts": sorted(g.conflicts),
            "depths": {t: g.depth(t) for t in g.tasks},
        }
        _out(report, args.json)
    elif args.what == "impact":
        decls = args.decls or []
        _out({"task": args.id, "changed_decls": decls, "stale_descendants": g.stale_on_statement_change(args.id, decls),
              "all_descendants": sorted(g.descendants(args.id))}, args.json)
    elif args.what == "context":
        # what a Steward packs for the scriber: informs in-neighbours above threshold, by weight
        rel = sorted(((v["weight"], a, v["reason"]) for (a, b), v in g.informs.items() if b == args.id and v["weight"] >= args.min_weight), reverse=True)
        _out([{"task": a, "weight": w, "reason": r, "dir": str(g.tasks[a].dir.relative_to(root)) if a in g.tasks else None} for w, a, r in rel], args.json)


def cmd_score(args):
    root, store, log = _ctx(args)
    g = Graph.build(store.load_all())
    if args.id:
        _out(score_task(g, args.id, store.config("weights")), args.json)
    else:
        rows = rank_ready(g, store.config("weights"), args.queue)
        _out([{"task": r["task"], "score": r["score"], **{k: round(v, 3) for k, v in r["terms"].items()}} for r in rows], args.json)


def statement_window_open(window, now: datetime | None = None) -> bool:
    """True when `now` (Asia/Jerusalem; local time when tzdata is missing) falls in limits.yaml
    statement_window, a "HH:MM-HH:MM" string; an absent or empty window is always open (T-0125)."""
    if not window:
        return True
    lo, hi = (s.strip() for s in str(window).split("-", 1))
    try:
        now = now or datetime.now(ZoneInfo("Asia/Jerusalem"))
    except ZoneInfoNotFoundError:
        now = datetime.now()
    t = now.strftime("%H:%M")
    return lo <= t <= hi if lo <= hi else t >= lo or t <= hi     # "22:00-06:00" wraps midnight


def _worktree_root(root: Path, store: Store, key: str | None) -> str:
    """The worktree root `scr dispatch` records, resolved the way the watcher and the launcher resolve it
    (ops.workload: an unregistered or missing key is the first entry; T-0153 review, item 1). The record keeps
    the relative string form: the entry's `worktrees` value, else the resolved root relative to the scriptorium."""
    from .ops import workload
    try:
        wl = workload(root, store, key)
    except RepoError:                                       # no workloads.yaml entries at all: the old default
        return "../worktrees"
    if wl.cfg.get("worktrees"):
        return str(wl.cfg["worktrees"])
    try:
        return Path(os.path.relpath(wl.wt_root, root.resolve())).as_posix()
    except ValueError:                                      # another drive: no relative form
        return wl.wt_root.as_posix()


def cmd_cloud(args):
    """Cloud lane (scr/cloud.py): `claim` hands ready, cloud-eligible tasks to the pool up to limits.cloud_pools by
    setting them `blocked` with the cloud-lane note (journaled, so the record is the claim); `status` lists claims."""
    from . import cloud
    from .events import now_iso
    root, store, log = _ctx(args)
    tasks = store.load_all()
    events = log.read()
    busy = cloud.claimed_ids(events, tasks)
    cap, lean_ok = cloud.cloud_limits(store)
    if args.op == "status":
        print(f"cloud_pools {cap} (lean {'on' if lean_ok else 'off'}); claimed {len(busy)}: {', '.join(busy) or '—'}")
        return
    if cap <= 0:
        if not args.quiet:
            print("cloud_pools is 0 in config/limits.yaml: nothing claimed")
        return
    g = Graph.build(tasks)
    chosen, skipped = cloud.pick(store, g, events, store.config("weights"))
    if args.dry_run:
        print("would claim:", ", ".join(chosen) or "—", f"(claimed now: {len(busy)}, cap {cap})")
        for tid, why in skipped[:8]:
            print(f"  skip {tid}: {why}")
        return
    for tid in chosen:
        t = store.get(tid)
        note = cloud.claim_note(args.actor, now_iso())
        old = t.status
        lifecycle.apply(t.rec, "blocked")
        t.rec["lane"] = "cloud"                       # the claim, readable by code; the note is for people
        t.rec["branch"] = f"task/{tid}"
        t.save()
        log.append("state", task=tid, **{"from": old}, to="blocked", actor=args.actor, note=note)
        if not args.quiet:
            print(f"{tid}: {old} -> blocked ({note})")
    if not chosen and not args.quiet:
        print(f"nothing to claim (claimed now: {len(busy)}, cap {cap})")

def cmd_dispatch(args):
    root, store, log = _ctx(args)
    limits = store.config("limits")
    g = Graph.build(store.load_all())
    # the slot-holders: the same set as scr.watch.SLOT_STATUSES; `escalated` and `returned` never count (T-0092)
    running = [t.id for t in g.tasks.values() if t.status in {"dispatched", "in_progress", "checkpoint", "submitted"}
               and not str(t.rec.get("title") or "").startswith("hotfix:")]   # never `waiting` (scr/jobs.py); a hotfix record is Omri editing the checkout, not a session (2026-10-15: T-0383 held a slot for 6 h)
    cap = int(limits.get("concurrency", 3)) - len(running)
    rows = rank_ready(g, store.config("weights"), "scribers")
    from . import cloud as _cloud
    cloud_held = set(_cloud.claimed_ids(log.read(), g.tasks))         # handed to the cloud lane: never a PC dispatch
    ranked = [r["task"] for r in rows if r["task"] not in cloud_held]
    # T-0272: a frozen workload's task is never picked; `scr --actor omri dispatch <id>` is the one exception path
    from .ops import task_frozen, workload_for
    if args.ids:
        for tid in args.ids:
            if tid not in g.tasks:
                raise RepoError(f"{tid}: no such task")
            if task_frozen(store, g.tasks[tid]) and args.actor != "omri":
                raise RepoError(f"{tid}: workload '{workload_for(root, store, g.tasks[tid]).name}' is frozen (config/workloads.yaml); "
                                f"only Omri dispatches its tasks: scr --actor omri dispatch {tid}")
            if tid not in ranked:
                raise RepoError(f"{tid}: not dispatchable (status {g.tasks[tid].status}; needs a ready scribers task)")
        ranked = [t for t in ranked if t in args.ids]
    held: list[str] = []
    if not args.ids:
        held = [t for t in ranked if task_frozen(store, g.tasks[t])]
        if held:
            print(f"frozen: {len(held)} task(s) held ({', '.join(held)})")
            ranked = [t for t in ranked if t not in held]
    # may_touch_statements tasks need --allow-statements and the limits.yaml statement_window to be open (T-0125)
    if not args.allow_statements or not statement_window_open(limits.get("statement_window")):
        if args.allow_statements:
            print(f"statement_window {limits.get('statement_window')} is closed now: may_touch_statements tasks held back")
        ranked = [t for t in ranked if not (g.tasks[t].rec.get("scope") or {}).get("may_touch_statements")]
    # never co-dispatch with something already running that conflicts
    ranked = [t for t in ranked if all(tuple(sorted((t, r))) not in g.conflicts for r in running)]
    chosen = g.independent_set(ranked, max(0, cap))
    if args.dry_run:
        print("would dispatch:", ", ".join(chosen) or "—", f"(running: {len(running)}, cap {limits.get('concurrency', 3)})")
        return
    for tid in chosen:
        t = g.tasks[tid]
        if t.status != "ready":
            lifecycle.apply(t.rec, "ready")
            log.append("state", task=tid, **{"from": t.status}, to="ready", actor="scr")
        old = t.rec["status"]
        lifecycle.apply(t.rec, "dispatched")
        t.rec["worktree"] = f"{_worktree_root(root, store, t.rec.get('workload'))}/{tid}"
        t.rec["branch"] = f"task/{tid}"
        t.save()
        # No attempt is charged here: the launcher's dispatched -> in_progress does it (T-0127), so a dispatch that
        # never launches and times out back to ready (T-0122) leaves `attempts` where it was.
        nxt = int(t.rec.get("attempts", 0)) + 1
        log.append("state", task=tid, **{"from": old}, to="dispatched", actor=args.actor, note=f"attempt {nxt} if it launches")
        print(f"dispatched {tid} -> {t.rec['branch']} (attempt {nxt} if it launches)")
    if not chosen:
        log.append("system", name="intake_starved" if not rows else "dispatch_blocked", actor=args.actor,
                   data={"ready": len(rows), "running": running, **({"frozen": held} if held else {})})
        print("nothing dispatched")
    refresh(root, args.quiet)


def cmd_event(args):
    root, store, log = _ctx(args)
    data = yaml.safe_load(args.data) if args.data else None
    ev = log.append(args.kind, **{k: v for k, v in {"name": args.name, "task": args.task, "actor": args.actor, "note": args.note, "data": data, "op": args.op}.items() if v is not None})
    print(ev["h"][:16])
    refresh(root, args.quiet)


def cmd_refresh(args):
    root, _, _ = _ctx(args)
    refresh(root, args.quiet, args.note)


def cmd_assets(args):
    """Print where the package's own files are (scr/assets.py): scripts and agents that need a hook or an
    agents/*.md path ask here instead of assuming a checkout."""
    from .assets import ASSETS, NAMES, asset_path
    print(asset_path(args.name) if args.name else ASSETS)
    if not args.name and not args.quiet:
        for n in NAMES:
            print(f"  {n}: {asset_path(n)}")


def cmd_verify(args):
    root, _, _ = _ctx(args)
    r = State(root).verify()
    if not args.quiet or r["errors"]:
        for e in r["errors"]:
            print("ERROR  ", e)
        for w in r["warnings"]:
            if not args.quiet:
                print("warn   ", w)
        for i in r.get("info", []):
            if not args.quiet:
                print("info   ", i)
        print(f"state_root {r['state_root'][:16]}  {len(r['errors'])} errors, {len(r['warnings'])} warnings")
    sys.exit(1 if r["errors"] else 0)


def cmd_replay(args):
    root, store, log = _ctx(args)
    _out(log.replay() if not args.task else list(log.iter_task(args.task)), args.json)


def cmd_history(args):
    root, _, _ = _ctx(args)
    try:
        out = subprocess.run(["git", "log", "--format=%H %cI", "--", "state/STATE.json"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout
    except subprocess.CalledProcessError:
        print("no git history")
        return
    for line in out.splitlines()[: args.n]:
        sha, ts = line.split()
        try:
            snap = json.loads(subprocess.run(["git", "show", f"{sha}:state/STATE.json"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout)
            print(f"{ts}  {sha[:10]}  state {snap['state_root'][:12]}  parent {snap['parent_state_root'][:12]}  tasks {len(snap['tasks'])}  events {snap['events_count']}" + (f"  {snap['note']}" if snap.get("note") else ""))
        except Exception as e:
            print(f"{ts}  {sha[:10]}  (unreadable snapshot: {e})")


def cmd_diff(args):
    root, _, _ = _ctx(args)
    st = State(root)

    def load(ref: str) -> dict:
        if ref in ("WORKTREE", "."):
            return st.read_snapshot() or {}
        return json.loads(subprocess.run(["git", "show", f"{ref}:state/STATE.json"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout)

    _out(st.diff(load(args.a), load(args.b)), args.json)


def _print(lines):
    for l in (lines if isinstance(lines, list) else [lines]):
        print(l)


def cmd_land(args):
    """One command for every landing (T-0119): ops.land_any sends a workload task through the push/PR land();
    a tooling task (workload repo = this checkout) is refused, because tooling lands as a PR in the scr repo."""
    root, store, _ = _ctx(args)
    from .ops import land_any
    r = land_any(root, args.id, no_pr=args.no_pr, draft=args.draft, dry_run=args.dry_run, merge=args.merge)
    _print(r["out"])
    if not r["ok"]:
        sys.exit(1)


def cmd_sweep(args):
    root, _, _ = _ctx(args)
    from .ops import sweep
    # --all (T-0202): the hygiene audit, one typed row per worktree of every workload; it fetches origin first
    # unless --dry-run, which leaves every repo's git state as it found it
    rows = sweep(root, merge=args.merge, dry_run=args.dry_run, wl_name=args.workload, all_workloads=args.all,
                 fetch=args.all and not args.dry_run)
    if args.json:
        _out(rows, True); return
    for r in rows:
        if r["kind"] != "dirty-mission":
            print(f"--- {r['workload']}/{r['worktree']} ({r['branch']}): {r['kind']}" + (f" — {r['note']}" if r["note"] else ""))
            continue
        print(f"=== {r['worktree']} ({r['branch']}): {r['files']} uncommitted file(s)" + (f" -> {r['sha']}" if r["sha"] else "") + (f" PR {r['url']}" if r["url"] else ""))
        for f in r["sample"]:
            print(f"  {f}")
        if r["note"]:
            print(f"  {r['note']}")
    if args.all:
        from .ops import HYGIENE_KINDS
        print("sweep --all: " + ", ".join(f"{k} {sum(1 for r in rows if r['kind'] == k)}" for k in HYGIENE_KINDS) + (" (dry run)" if args.dry_run else ""))
    else:
        print("sweep: nothing to do" if not rows else f"sweep: {len(rows)} worktree(s)" + (" (dry run)" if args.dry_run else ""))
    if any(not r["ok"] for r in rows):
        sys.exit(1)


def cmd_park(args):
    root, _, _ = _ctx(args)
    from .ops import park
    _print(park(root, args.id, args.note or "parked", actor=args.actor))


def cmd_retire(args):
    root, _, _ = _ctx(args)
    from .ops import retire
    r = retire(root, args.name, execute=args.execute)
    _print(r["out"])
    sys.exit(0 if r["ok"] else r["code"])


def cmd_worktrees(args):
    root, _, _ = _ctx(args)
    from .ops import worktrees_gc
    # T-0321: the retire rules applied to what is already on disk; --dry-run lists and changes nothing
    rows = worktrees_gc(root, dry_run=args.dry_run, wl_name=args.workload)
    if args.json:
        _out(rows, True); return
    for r in rows:
        print(f"{r['workload']:<18} {r['worktree']:<40} {r['status'] or '-':<11} {r['bytes'] / 2**20:>9.1f} MB  {r['action']}")
        for line in r["out"] if not r["ok"] else []:
            print(f"  {line}")
    gone = [r for r in rows if r["action"] in ("delete", "deleted", "drop .lake/build", "dropped .lake/build")]
    print(f"worktrees gc: {len(rows)} row(s), {sum(r['bytes'] for r in gone) / 2**30:.2f} GB "
          + ("would be freed (dry run)" if args.dry_run else "freed"))
    if any(not r["ok"] for r in rows):
        sys.exit(1)


def cmd_spawn(args):
    root, _, _ = _ctx(args)
    from .ops import spawn
    _print(spawn(root, args.tasks or None, lake=(False if args.no_lake else None)))


def cmd_launch(args):
    root, _, _ = _ctx(args)
    from .launch import launch
    _print(launch(root, args.tasks or None, mode=args.mode, permission_mode=args.permission_mode, model=args.model, relaunch=args.relaunch, dry_run=args.dry_run))


def cmd_intake(args):
    root, _, _ = _ctx(args)
    from .launch import intake
    _print(intake(root, dry_run=args.dry_run))


def cmd_checkpoint(args):
    root, _, _ = _ctx(args)
    from .ops import checkpoint_commit
    sha = checkpoint_commit(root, args.id, push=args.push)
    print(f"{args.id}: checkpoint {sha}" if sha else f"{args.id}: nothing to checkpoint")


def cmd_watch(args):
    root, _, _ = _ctx(args)
    from .watch import Watcher
    store = Store(root)
    limits = store.config("limits") or {}
    interval = args.interval or int(limits.get("checkpoint_every_minutes", 30))
    un = args.unattended
    # T-0302: --unattended implies --allow-statements when limits.yaml sets a statement_window; the window then
    # decides inside `scr dispatch`. No window: statement tasks still need the explicit flag (T-0125's default).
    allow_statements = args.allow_statements or (un and bool(limits.get("statement_window")))
    w = Watcher(root, stale_minutes=args.stale, auto_review=un or args.auto_review, auto_decide=un or args.auto_decide,
                auto_dispatch=un or args.auto_dispatch, allow_statements=allow_statements, auto_land=un or args.auto_land,
                auto_intake=un or args.auto_intake, permission_mode=args.permission_mode, push=args.push, quiet=args.quiet)
    w.loop(interval, once=args.once)


def cmd_chore(args):
    root, _, _ = _ctx(args)
    from . import chores
    if args.op == "list":
        acked = set((root / "reports" / "ui.acks.txt").read_text(encoding="utf-8").split()) if (root / "reports" / "ui.acks.txt").exists() else set()
        for c in chores.status(root, acked):
            print(f"{c['id']:24} {'(manual) ' if c['manual'] else ''}{c['title']}")
    elif args.op == "run":
        if not args.id:
            raise RepoError("chore run needs an id")
        r = chores.run(root, args.id, actor=args.actor)
        print(r["output"]); print("DONE" if r["done"] else "NOT DONE")
        sys.exit(0 if r["done"] else 1)
    elif args.op == "check":
        c = next((x for x in chores.load(root) if x["id"] == args.id), None)
        print("done" if c and chores.check(root, c.get("done")) else "not done")


def cmd_update(args):
    root, _, _ = _ctx(args)
    from .admin import update
    _print(update(root, Path(args.bundle), actor=args.actor))


def cmd_migrate(args):
    """Journal-mode setup (idempotent), then the pending state-schema migrations (scr/schema.py), each its own
    journaled commit. `--dry-run` lists the pending migrations and changes nothing."""
    root, _, _ = _ctx(args)
    from .schema import migrate_schema
    if args.dry_run:
        _print(migrate_schema(root, args.actor, dry_run=True))
        return
    from .admin import migrate
    _print(migrate(root))
    try:
        _print(migrate_schema(root, args.actor))
    except RuntimeError as e:
        raise RepoError(str(e))


def cmd_issue(args):
    """Known issues (scr/issues.py): add | list | close. `add --urgent` also files a tooling task at the top of
    the queue (ready, omri_weight 2), the fast track for a real outage."""
    root, store, _ = _ctx(args)
    from . import issues
    if args.op == "list":
        items = [i for i in issues.issues(root) if args.all or i["status"] == "open"]
        if args.json:
            _out(items, True)
            return
        print("\n".join(f"{i['id']} [{i['status']}]{' urgent' if i['urgent'] else ''} {i['text']}" + (f" -> {i['task']}" if i["task"] else "")
                        for i in items) or "no open issues")
        return
    if args.op == "close":
        if not args.id or not args.note:
            raise RepoError("scr issue close <id> --note <why>")
        try:
            i = issues.close(root, args.id, args.actor, args.note)
        except (KeyError, ValueError) as e:
            raise RepoError(str(e))
        print(f"closed {i['id']}")
        return
    text = " ".join([args.id or "", *(args.text or [])]).strip()   # `add` has no id: its first word lands in `id`
    if not text.strip():
        raise RepoError('scr issue add "<what is wrong>" [--urgent]')
    task = None
    if args.urgent:
        tid = store.next_id()
        wl = issues.tooling_workload(store.config("workloads"))
        argv = ["--root", str(root), "--actor", args.actor, "--quiet", "task", "new", f"Urgent known issue: {' '.join(text.split())}"[:200],
                "--id", tid, "--kind", "refactor", "--origin", "omri", "--omri-weight", "2.0", "--note", "scr issue add --urgent"]
        if wl:
            argv += ["--workload", wl]
        p = build_parser()
        for a in (argv, ["--root", str(root), "--actor", args.actor, "--quiet", "task", "set", tid, "triaged"],
                  ["--root", str(root), "--actor", args.actor, "--quiet", "task", "set", tid, "ready", "--note", "urgent known issue"]):
            ns = p.parse_args(a)
            ns.fn(ns)
        task = tid
    i = issues.add(root, text, args.actor, urgent=args.urgent, task=task)
    print(f"added {i['id']}" + (f" and filed {task} at the top of the queue" if task else "") + f" ({issues.VIEW.as_posix()})")


def cmd_ensure_main(args):
    """Fold a side branch into <main> (fast-forward, else merge) and with --push send it to origin. This is
    the `scr` way to move the checkout's branch: nobody types git in the scriptorium folder (T-0061)."""
    root, store, _ = _ctx(args)
    from .admin import ensure_main
    from .gitops import Git
    from .ops import self_workload
    g = Git(root)
    if g.dirty():                       # ensure_main refuses a dirty tree; in journal mode the tree is committed first
        journal(root, args.actor, "ensure-main: journal the tree before folding main")
    wl = self_workload(root, store)
    _print(ensure_main(root, g, main=args.main or (wl.main if wl else "main"), push=args.push))


def cmd_up(args):
    root, _, _ = _ctx(args)
    from .admin import up, stop, stop_failed
    if args.stop:
        lines = stop(root, "ui") + stop(root, "watch")
        _print(lines)
        sys.exit(1 if stop_failed(lines) else 0)      # T-0156: a surviving process must not read as a made stop
    lines = up(root, ui_only=args.ui_only, restart=args.restart, register_logon=args.register, port=args.port, open_browser=not args.no_browser)
    _print(lines)
    (root / "reports").mkdir(exist_ok=True)      # `up` also runs windowless from the Desktop shortcut: keep a trace
    with (root / "reports" / "up.log").open("a", encoding="utf-8") as f:
        f.write(f"--- {now_iso()}\n" + "\n".join(lines) + "\n")


def cmd_discuss(args):
    root, _, _ = _ctx(args)
    from .discuss import discuss
    _print(discuss(root, args.item, open_terminal=not args.print_only))


def cmd_takeover(args):
    root, _, _ = _ctx(args)
    from .discuss import takeover
    if not args.id and not args.session:
        raise RepoError("name a task or --session <id>")
    if args.terminal_pid is not None:        # the takeover terminal's launch script names its own process (T-0273)
        from .discuss import takeover_terminal
        if not args.session:
            raise RepoError("--terminal-pid needs --session <id>")
        ev = takeover_terminal(root, args.id, args.session, args.terminal_pid, by=args.actor)
        refresh(root, True)
        if not args.quiet:
            print(ev["h"][:16])
        return
    _print(takeover(root, args.id, session=args.session, stop=args.stop, phone=args.phone, open_terminal=not args.no_terminal))


def cmd_config(args):
    root, store, log = _ctx(args)
    p = root / "config" / f"{args.file}.yaml"
    if not p.exists():
        raise RepoError(f"no config/{args.file}.yaml")
    txt = p.read_text(encoding="utf-8")
    val = yaml.safe_load(args.value)
    rx = re.compile(rf"(?m)^{re.escape(args.key)}:[^#\n]*(#[^\n]*)?$")
    if rx.search(txt):
        txt = rx.sub(lambda m: f"{args.key}: {val}" + (f"   {m.group(1)}" if m.group(1) else ""), txt, count=1)
    else:
        txt = txt.rstrip("\n") + f"\n{args.key}: {val}\n"
    p.write_text(txt, encoding="utf-8")
    log.append("system", name="system_change", actor=args.actor, note=args.note or f"{args.file}.{args.key} = {val}", data={"file": args.file, "key": args.key, "value": val})
    print(f"{args.file}.{args.key} = {val}")
    refresh(root, args.quiet)


def cmd_hotfix(args):
    root, _, _ = _ctx(args)
    from . import hotfix
    who = args.actor.split(":", 1)[1] if args.actor.startswith("hotfix:") else args.actor
    if args.op == "open":
        symptom = " ".join(x for x in (args.id, args.text) if x)     # `hotfix open "<symptom>"`: the text lands in the id slot
        if not symptom:
            raise RepoError("hotfix open needs the symptom as text")
        r = hotfix.open_hotfix(root, symptom, who=who)
        print(f"{r['id']} {'already open' if r.get('already_open') else 'opened'}; brief: {r['brief']}")
        if args.discuss:
            from .discuss import discuss
            _print(discuss(root, f"hotfix:{r['id']}"))
    elif args.op == "close":
        r = hotfix.close_hotfix(root, args.id, args.summary or "closed", who=who, unlocks=args.unlock or [])
        print(r["output"])
        sys.exit(0 if r["ok"] else 1)
    elif args.op == "test":
        rc, out = hotfix.run_tests(root)
        print(out); sys.exit(rc)
    elif args.op == "status":
        st = hotfix.status(root)
        print(json.dumps(st, indent=1) if st else "no hotfix open")


def cmd_answer(args):
    root, _, _ = _ctx(args)
    from .answers import apply
    r = apply(root, args.id, args.text, args.kind, actor=args.actor, concierge=not args.no_concierge)
    _print(r["log"])


JOB_OPTS = ("task", "name", "timeout", "cwd", "session")


def job_args(args) -> tuple[argparse.Namespace, list[str]]:
    """Split `scr job ...`'s trailing words into the job's own options and the command after `--`.

    The `rest` positional is argparse.REMAINDER, which swallows EVERYTHING after `op` — including `--task T-0001`
    (2026-10-11: J-0007 and J-0010 ran a program called `--task`, rc 127). So the words before `--` are re-parsed
    here for the id and the options; the words after `--` are the command, verbatim."""
    rest = list(getattr(args, "rest", None) or [])
    opts, cmd = rest, []
    if "--" in rest:
        i = rest.index("--")
        opts, cmd = rest[:i], rest[i + 1:]
    sub = argparse.ArgumentParser(prog="scr job", add_help=False)
    sub.add_argument("id", nargs="?")
    for o in JOB_OPTS:
        sub.add_argument("--" + o)
    sub.add_argument("--all", action="store_true"); sub.add_argument("-n", type=int); sub.add_argument("--poll", type=float)
    known, unknown = sub.parse_known_args(opts)
    if unknown and cmd:
        raise RepoError(f"job: unrecognized words before `--`: {' '.join(unknown)}")
    if unknown and not cmd:                      # no `--`: whatever is left IS the command (a bare `scr job submit lake build`)
        cmd = unknown
    merged = argparse.Namespace(**vars(args))
    merged.id = args.id or known.id
    for o in JOB_OPTS:
        if getattr(known, o, None) is not None:
            setattr(merged, o, getattr(known, o))
    if known.all:
        merged.all = True
    if known.n is not None:
        merged.n = known.n
    if known.poll is not None:
        merged.poll = known.poll
    return merged, cmd


def cmd_job(args):
    """scr job submit|list|tail|kill|wait — long commands run detached under the watcher (scr/jobs.py)."""
    root, store, log = _ctx(args)
    from . import jobs
    op = args.op
    args, cmd = job_args(args)
    if op == "submit":
        if not cmd:
            raise RepoError("job submit: give the command after `--`, e.g. scr job submit --task T-0001 --name build -- lake build")
        if args.task and args.task not in store.load_all():
            raise RepoError(f"{args.task}: no such task")
        try:
            job = jobs.submit(root, cmd, task=args.task, name=args.name, cwd=args.cwd, timeout=args.timeout, session=args.session, actor=args.actor)
        except ValueError as e:
            raise RepoError(str(e)) from None
        ev = {"name": "job_submitted", "actor": args.actor, "note": f"{job['id']} {job['name']}: {job['status']} ({job['timeout_min']} min): {' '.join(cmd)[:200]}"}
        if job.get("task"):
            ev["task"] = job["task"]
        log.append("system", **ev)
        print(f"{job['id']} {job['status']} (timeout {job['timeout_min']} min; log {job['log']})")
        print(f"hand off: append `WAITING: job {job['id']}` as the last line of log.md and end your turn; the watcher resumes you when it ends")
        return
    if op == "list":
        rows = jobs.list_jobs(root, active_only=not args.all)
        print("\n".join(jobs.describe(j) for j in rows) if rows else "no jobs" + ("" if args.all else " running or queued (--all for finished ones)"))
        return
    if not args.id:
        raise RepoError(f"job {op}: a job id (J-xxxx) is required")
    if op == "tail":
        print(jobs.tail(root, args.id, args.n))
    elif op == "kill":
        job = jobs.kill_job(root, args.id, note=f"killed by {args.actor}")
        if not job:
            raise RepoError(f"{args.id}: no such job")
        log.append("system", name="job_killed", actor=args.actor, note=f"{args.id} {job.get('name')}", **({"task": job["task"]} if job.get("task") else {}))
        print(jobs.describe(job))
    elif op == "wait":
        job = jobs.wait(root, args.id, poll_seconds=args.poll)
        if not job:
            raise RepoError(f"{args.id}: no such job")
        print(jobs.describe(job))
        sys.exit(0 if job.get("status") == "done" else 1)


def cmd_inbox(args):
    root, _, _ = _ctx(args)
    from .answers import add_inbox
    add_inbox(root, args.text, actor=args.actor)
    print("added to Inbox")


def cmd_ack(args):
    root, _, _ = _ctx(args)
    p = root / "reports" / "ui.acks.txt"
    p.parent.mkdir(exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(args.key + "\n")
    EventLog(root / "events" / "events.jsonl").append("system", name="ack", actor=args.actor, note=args.key)
    print(f"acknowledged {args.key}")


def cmd_notify(args):
    """--test: one test message on every enabled channel of config/notify.yaml. Without it: the cards that stand
    and whether the watcher has pushed each (T-0090)."""
    root, _, _ = _ctx(args)
    from . import notify
    if args.test:
        lines = notify.send_test(root)
        print("\n".join(lines))
        if any("FAILED" in ln for ln in lines):
            sys.exit(1)
        return
    from .ui import acked_keys, needs_cards
    sf = root / ".scr" / "watch.state.txt"
    flags = set(sf.read_text(encoding="utf-8").split()) if sf.exists() else set()
    cards = needs_cards(root)
    new = {c["key"] for c in notify.pending(cards, flags, acked_keys(root))}
    print(f"channels on: {', '.join(notify.enabled(notify.load(root))) or 'none'}; {len(cards)} card(s) stand, {len(new)} not yet pushed")
    for c in cards:
        print(f"  {'new   ' if c['key'] in new else 'pushed'}  {c['key']}  {c.get('title', '')[:80]}")


def cmd_journal(args):
    root, _, _ = _ctx(args)
    sha = journal(root, args.actor, args.note, push=args.push)
    if not args.quiet:
        print(f"journaled {sha}" if sha else "nothing to journal")


def cmd_ui(args):
    from .ui import serve
    root, _, _ = _ctx(args)
    serve(root, args.port, not args.no_browser, child=args.child)


def cmd_checkin(args):
    root, store, log = _ctx(args)
    log.append("checkin", actor="omri", op=args.op, note=args.note)
    refresh(root, args.quiet)


# ---- parser --------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    def globals_(parser, sup):
        d = argparse.SUPPRESS if sup else None
        parser.add_argument("--root", default=d, help="repo root (default: search upward / $SCRIPTORIUM_ROOT)")
        parser.add_argument("--actor", default=argparse.SUPPRESS if sup else "omri", help="who is acting (omri | intake | prioritizer | steward:<id> | concierge | observer | scriber:<id>)")
        parser.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS if sup else False)
        parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS if sup else False, help="JSON output where applicable")
        parser.add_argument("--no-journal", action="store_true", default=argparse.SUPPRESS if sup else False, help="do not auto-commit the scriptorium tree after this command")

    p = argparse.ArgumentParser(prog="scr", description="Scriptorium: git-backed task queues, dependency graph, hashed state.")
    globals_(p, False)
    common = argparse.ArgumentParser(add_help=False)
    globals_(common, True)   # global options are also accepted after the subcommand
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", parents=[common], help="create the directory layout, genesis event, first snapshot"); s.set_defaults(fn=cmd_init)

    t = sub.add_parser("task", parents=[common], help="task records").add_subparsers(dest="tcmd", required=True)
    n = t.add_parser("new", parents=[common])
    n.add_argument("title"); n.add_argument("--id"); n.add_argument("--queue", choices=QUEUES, default="scribers")
    n.add_argument("--kind", choices=KINDS, default="prove"); n.add_argument("--workload", default="quantum-insertion")
    n.add_argument("--files", nargs="*"); n.add_argument("--decls", nargs="*"); n.add_argument("--may-touch-statements", action="store_true")
    n.add_argument("--blocked-by", nargs="*"); n.add_argument("--reason", help="justification for blocks edges without shared scope")
    n.add_argument("--origin"); n.add_argument("--omri-weight", type=float, default=0.0); n.add_argument("--verifiability", type=float, default=1.0)
    n.add_argument("--risk", type=float, default=0.2); n.add_argument("--max-attempts", type=int, default=None, help="default: limits.yaml default_max_attempts")
    n.add_argument("--max-tokens", type=int, default=2_000_000); n.add_argument("--max-wall-hours", type=float, default=6); n.add_argument("--note")
    n.set_defaults(fn=cmd_task_new)
    st = t.add_parser("set", parents=[common], help="status transition")
    st.add_argument("id"); st.add_argument("status"); st.add_argument("--note"); st.add_argument("--tokens", type=int); st.add_argument("--force", action="store_true")
    st.add_argument("--force-accept", action="store_true", help="(--actor omri only) accept a submitted task whose review.md does not decide `accepted`; logs an accept_override event with --note")
    st.set_defaults(fn=cmd_task_set)
    br = t.add_parser("brief", parents=[common], help="print the standard scriber launch prompt for a task")
    br.add_argument("id"); br.set_defaults(fn=cmd_task_brief)
    f = t.add_parser("field", parents=[common], help="set a record field (dotted path), logged")
    f.add_argument("id"); f.add_argument("field"); f.add_argument("value"); f.add_argument("--note"); f.set_defaults(fn=cmd_task_field)

    e = sub.add_parser("edge", parents=[common], help="add/remove a graph edge")
    e.add_argument("op", choices=["add", "remove"]); e.add_argument("edge", choices=["blocks", "unlocks", "informs", "supersedes"])
    e.add_argument("frm", metavar="from"); e.add_argument("to"); e.add_argument("--weight", type=float, default=0.5); e.add_argument("--reason")
    e.set_defaults(fn=cmd_edge)

    g = sub.add_parser("graph", parents=[common], help="graph views and analyses")
    g.add_argument("what", choices=["show", "hashes", "analyze", "impact", "context"]); g.add_argument("id", nargs="?")
    g.add_argument("--decls", nargs="*"); g.add_argument("--min-in", default=3); g.add_argument("--min-weight", type=float, default=0.4)
    g.set_defaults(fn=cmd_graph)

    sc = sub.add_parser("score", parents=[common], help="prioritization scores"); sc.add_argument("id", nargs="?"); sc.add_argument("--queue", default="scribers"); sc.set_defaults(fn=cmd_score)
    d = sub.add_parser("dispatch", parents=[common], help="pick a conflict-free set of ready tasks and mark them dispatched")
    d.add_argument("ids", nargs="*", help="dispatch only these tasks (a frozen workload's task: --actor omri only)")
    d.add_argument("--dry-run", action="store_true"); d.add_argument("--allow-statements", action="store_true"); d.set_defaults(fn=cmd_dispatch)

    cl = sub.add_parser("cloud", parents=[common], help="cloud lane: claim ready tasks for the cloud pool / show claims")
    cl.add_argument("op", choices=["claim", "status"]); cl.add_argument("--dry-run", action="store_true"); cl.set_defaults(fn=cmd_cloud)

    ev = sub.add_parser("event", parents=[common], help="append a system/checkin event")
    ev.add_argument("kind", choices=["system", "checkin"]); ev.add_argument("--name"); ev.add_argument("--task"); ev.add_argument("--note"); ev.add_argument("--data", help="YAML/JSON"); ev.add_argument("--op")
    ev.set_defaults(fn=cmd_event)
    ci = sub.add_parser("checkin", parents=[common], help="omri opens/closes a check-in (for the minutes metric)"); ci.add_argument("op", choices=["open", "close"]); ci.add_argument("--note"); ci.set_defaults(fn=cmd_checkin)

    r = sub.add_parser("refresh", parents=[common], help="regenerate graph.json, queues, STATE.json"); r.add_argument("--note"); r.set_defaults(fn=cmd_refresh)
    sub.add_parser("snapshot", parents=[common], help="alias of refresh").set_defaults(fn=cmd_refresh, note=None)
    sub.add_parser("verify", parents=[common], help="check snapshot, chain, replay, invariants; exit 1 on error").set_defaults(fn=cmd_verify)
    asp = sub.add_parser("assets", parents=[common], help="print the folder of the package's own files (templates, ui, agents, hooks)")
    asp.add_argument("name", nargs="?", choices=["templates", "ui", "agents", "hooks"]); asp.set_defaults(fn=cmd_assets)
    rp = sub.add_parser("replay", parents=[common], help="derived state from the event log"); rp.add_argument("--task"); rp.set_defaults(fn=cmd_replay)
    h = sub.add_parser("history", parents=[common], help="snapshot chain across git commits"); h.add_argument("-n", type=int, default=20); h.set_defaults(fn=cmd_history)
    df = sub.add_parser("diff-state", parents=[common], help="compare two snapshots by hash (git refs, or WORKTREE)"); df.add_argument("a"); df.add_argument("b", nargs="?", default="WORKTREE"); df.set_defaults(fn=cmd_diff)
    j = sub.add_parser("journal", parents=[common], help="commit the scriptorium tree if dirty (journal mode; every mutating command does this itself)")
    j.add_argument("--note", default="journal"); j.add_argument("--push", action="store_true"); j.set_defaults(fn=cmd_journal)
    land_help = ("land a task through the PR path (commit + push, open its PR; --merge: merge and mark merged); "
                 "a tooling task of this checkout is refused: tooling lands as a PR in the scr repo")
    ld = sub.add_parser("land", parents=[common], help=land_help, description=land_help)
    ld.add_argument("id"); ld.add_argument("--no-pr", action="store_true"); ld.add_argument("--draft", action="store_true"); ld.add_argument("--dry-run", action="store_true"); ld.add_argument("--merge", action="store_true")
    ld.set_defaults(fn=cmd_land)
    sw = sub.add_parser("sweep", parents=[common], help="commit uncommitted work in non-task worktrees (--merge: PR + merge into main)")
    sw.add_argument("--merge", action="store_true"); sw.add_argument("--dry-run", action="store_true")
    sw.add_argument("--all", action="store_true", help="hygiene audit: every workload, one typed row per worktree (dirty-mission/finished/phantom/orphan/locked/live); only dirty missions are committed")
    sw.add_argument("--workload", help="the workload to sweep (default: the first in config/workloads.yaml)"); sw.set_defaults(fn=cmd_sweep)
    pk = sub.add_parser("park", parents=[common], help="returned -> escalated -> omri_queue"); pk.add_argument("id"); pk.add_argument("--note"); pk.set_defaults(fn=cmd_park)
    rt = sub.add_parser("retire", parents=[common], help="dispose of a worktree by its task's status: merged/closed/rejected -> delete (git worktree remove + prune, untracked value copied out, links unlinked); returned/escalated/omri_queue -> quarantine into <repo>-RETIRED/ without .lake/build"); rt.add_argument("name"); rt.add_argument("--execute", action="store_true"); rt.set_defaults(fn=cmd_retire)
    wg = sub.add_parser("worktrees", parents=[common], help="worktree disk hygiene").add_subparsers(dest="wcmd", required=True)
    gc_help = ("every workload's worktrees with task status and size; deletes those whose task is merged/closed/rejected "
               "(the `scr retire` rules) and drops .lake/build from trees under <repo>-RETIRED/; --dry-run changes nothing")
    gc = wg.add_parser("gc", parents=[common], help=gc_help, description=gc_help)
    gc.add_argument("--dry-run", action="store_true"); gc.add_argument("--workload", help="one workload (default: all in config/workloads.yaml)"); gc.set_defaults(fn=cmd_worktrees)
    sp = sub.add_parser("spawn", parents=[common], help="worktree per dispatched task from origin/<main>"); sp.add_argument("--tasks", nargs="*"); sp.add_argument("--no-lake", action="store_true"); sp.set_defaults(fn=cmd_spawn)
    la = sub.add_parser("launch", parents=[common], help="start scriber sessions for dispatched tasks")
    la.add_argument("--tasks", nargs="*"); la.add_argument("--mode", choices=["headless", "tabs"], default="headless"); la.add_argument("--permission-mode", default=None, help="override the config default (limits.yaml claude_permission_mode; see scr/run.py)")
    la.add_argument("--model"); la.add_argument("--relaunch", action="store_true"); la.add_argument("--dry-run", action="store_true"); la.set_defaults(fn=cmd_launch)
    ik = sub.add_parser("intake", parents=[common], help="run the Intake agent once (headless)"); ik.add_argument("--dry-run", action="store_true"); ik.set_defaults(fn=cmd_intake)
    ck = sub.add_parser("checkpoint", parents=[common], help="commit a running task's worktree on its branch"); ck.add_argument("id"); ck.add_argument("--push", action="store_true"); ck.set_defaults(fn=cmd_checkpoint)
    w = sub.add_parser("watch", parents=[common], help="the Steward's mechanical loop (--once for a single tick)")
    w.add_argument("--once", action="store_true"); w.add_argument("--interval", type=int, default=0); w.add_argument("--stale", type=int, default=45)
    for flag in ("auto-review", "auto-decide", "auto-dispatch", "allow-statements", "auto-land", "auto-intake", "unattended", "push"):
        w.add_argument("--" + flag, action="store_true")
    w.add_argument("--permission-mode", default=None, help="override the config default for the sessions this watcher starts (limits.yaml claude_permission_mode)"); w.set_defaults(fn=cmd_watch)
    ch = sub.add_parser("chore", parents=[common], help="chores from ui/chores.yaml"); ch.add_argument("op", choices=["list", "run", "check"]); ch.add_argument("id", nargs="?"); ch.set_defaults(fn=cmd_chore)
    upd = sub.add_parser("update", parents=[common], help="apply an update bundle (zip or folder) and migrate"); upd.add_argument("bundle"); upd.set_defaults(fn=cmd_update)
    mg = sub.add_parser("migrate", parents=[common], help="journal-mode setup (idempotent), then the pending state-schema migrations")
    mg.add_argument("--dry-run", action="store_true", help="list the pending schema migrations, change nothing"); mg.set_defaults(fn=cmd_migrate)
    iss = sub.add_parser("issue", parents=[common], help='known issues: add "<text>" [--urgent] | list [--all] | close <id> --note <why>')
    iss.add_argument("op", choices=["add", "list", "close"]); iss.add_argument("id", nargs="?", help="close: the issue id")
    iss.add_argument("text", nargs="*", help=argparse.SUPPRESS); iss.add_argument("--urgent", action="store_true")
    iss.add_argument("--note"); iss.add_argument("--all", action="store_true"); iss.set_defaults(fn=cmd_issue)
    em = sub.add_parser("ensure-main", parents=[common], help="fold a side branch into main (fast-forward, else merge) and, with --push, push main to origin")
    em.add_argument("--main", help="branch name (default: the scriptorium workload's main_branch)"); em.add_argument("--push", action="store_true"); em.set_defaults(fn=cmd_ensure_main)
    upp = sub.add_parser("up", parents=[common], help="start (and register at logon) the UI and the watcher")
    upp.add_argument("--ui-only", action="store_true"); upp.add_argument("--restart", action="store_true"); upp.add_argument("--register", action="store_true", help="also start at logon (default: only the Desktop shortcut starts it)")
    upp.add_argument("--stop", action="store_true"); upp.add_argument("--port", type=int, default=8765); upp.add_argument("--no-browser", action="store_true"); upp.set_defaults(fn=cmd_up)
    cf = sub.add_parser("config", parents=[common], help="set a top-level key in config/<file>.yaml (logged as system_change)")
    cf.add_argument("op", choices=["set"]); cf.add_argument("file"); cf.add_argument("key"); cf.add_argument("value"); cf.add_argument("--note"); cf.set_defaults(fn=cmd_config)
    an = sub.add_parser("answer", parents=[common], help="record Omri's answer to a decision / HELP / gate item (and run the Concierge)")
    an.add_argument("id"); an.add_argument("--kind", choices=["decision", "help", "gate"], default="decision"); an.add_argument("--text", required=True); an.add_argument("--no-concierge", action="store_true"); an.set_defaults(fn=cmd_answer)
    ib = sub.add_parser("inbox", parents=[common], help="add one idea line to the Inbox in queues/omri.md"); ib.add_argument("text"); ib.set_defaults(fn=cmd_inbox)
    jb = sub.add_parser("job", parents=[common], help="long commands run detached under the watcher: submit (-- <cmd>) | list [--all] | tail <id> | kill <id> | wait <id>")
    jb.add_argument("op", choices=["submit", "list", "tail", "kill", "wait"]); jb.add_argument("id", nargs="?")
    jb.add_argument("--task"); jb.add_argument("--name"); jb.add_argument("--timeout", default=None, help="4h, 90m or minutes (default 240 min; over 12 h only --actor omri)")
    jb.add_argument("--cwd"); jb.add_argument("--session", help="the Claude session id to resume when the job ends (default: the task's last scriber launch)")
    jb.add_argument("--all", action="store_true"); jb.add_argument("-n", type=int, default=40); jb.add_argument("--poll", type=float, default=10.0)
    jb.add_argument("rest", nargs=argparse.REMAINDER, help="after `--`: the command and its arguments"); jb.set_defaults(fn=cmd_job)
    ak = sub.add_parser("ack", parents=[common], help="dismiss a system alarm (event hash) or a chore (chore:<id>) from the UI"); ak.add_argument("key"); ak.set_defaults(fn=cmd_ack)
    nt = sub.add_parser("notify", parents=[common], help="push channel for needs-you cards (config/notify.yaml): --test sends one test message per enabled channel")
    nt.add_argument("--test", action="store_true"); nt.set_defaults(fn=cmd_notify)
    hx = sub.add_parser("hotfix", parents=[common], help="fix the tooling in place, as a recorded task: open <symptom> | close [id] --summary | test | status")
    hx.add_argument("op", choices=["open", "close", "test", "status"]); hx.add_argument("id", nargs="?"); hx.add_argument("text", nargs="?")
    hx.add_argument("--summary"); hx.add_argument("--unlock", action="append", help='"<title> | <why>" (repeatable)'); hx.add_argument("--discuss", action="store_true", help="open the Toolsmith session too")
    hx.set_defaults(fn=cmd_hotfix)
    pl = sub.add_parser("plan", parents=[common], help="open a planning session (Intake + Prioritizer) to turn a conversation into tasks"); pl.add_argument("--print-only", action="store_true"); pl.set_defaults(fn=cmd_discuss, item="plan")
    dc = sub.add_parser("discuss", parents=[common], help="open a Claude Code session preloaded with a ticket's context"); dc.add_argument("item"); dc.add_argument("--print-only", action="store_true"); dc.set_defaults(fn=cmd_discuss)
    to = sub.add_parser("takeover", parents=[common], help="resume a headless session interactively (claude --resume in its folder); --stop stops it first if it still runs")
    to.add_argument("id", nargs="?"); to.add_argument("--session"); to.add_argument("--stop", action="store_true"); to.add_argument("--phone", action="store_true", help="with Remote Control")
    to.add_argument("--no-terminal", action="store_true", help="record + stop as usual, print the resume command instead of opening it")
    to.add_argument("--terminal-pid", type=int, help="record this pid as the takeover terminal's process (the launch script's own; T-0273)"); to.set_defaults(fn=cmd_takeover)
    ui = sub.add_parser("ui", parents=[common], help="serve the Scriptorium UI on localhost (Omri's entry point)")
    ui.add_argument("--port", type=int, default=8765); ui.add_argument("--no-browser", action="store_true"); ui.add_argument("--child", action="store_true", help=argparse.SUPPRESS); ui.set_defaults(fn=cmd_ui)
    return p


# Commands that change the tree and therefore end with a journal commit (journal mode).
MUTATING = {"init", "task", "edge", "dispatch", "event", "checkin", "land", "sweep", "park", "retire", "worktrees",
            "spawn", "launch", "chore", "update", "migrate", "ensure-main", "intake", "checkpoint", "discuss", "answer", "inbox", "ack", "config", "plan", "hotfix", "job", "issue"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Records and briefs carry ⊆, ×, → and Hebrew; a cp1255 console (the default when stdout is a pipe on
    # this machine) raised UnicodeEncodeError on `scr discuss --print-only` (2026-09-16). Never crash on output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except RepoError as e:
        print("error:", e, file=sys.stderr)
        sys.exit(2)
    finally:
        # journal AFTER the command, whether it succeeded or raised: a partial change is still a change
        if args.cmd in MUTATING and not (args.cmd == "worktrees" and getattr(args, "dry_run", False)):   # gc --dry-run changes nothing (T-0321)
            try:
                root = find_root(Path(args.root) if getattr(args, "root", None) else None)
                refresh(root, quiet=True)             # derived views always follow the records, journal or not
                if getattr(args, "no_journal", False):
                    raise RepoError("no journal")
                shown, skip = [], False
                for a in argv:                      # drop global noise; the actor is the message prefix
                    if skip: skip = False; continue
                    if a in ("--actor", "--root"): skip = True; continue
                    if a in ("--quiet", "--json", "--no-journal"): continue
                    shown.append(a)
                journal(root, getattr(args, "actor", "scr"), " ".join(shown), force_refresh=False)
            except RepoError:
                pass


if __name__ == "__main__":
    main()
