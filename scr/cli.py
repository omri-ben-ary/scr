"""scr — the Scriptorium command line. Every mutating command ends with `refresh`:
graph/graph.json, queues/*.md and state/STATE.json are regenerated so the tree is
verifiable at all times."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from . import lifecycle
from .canon import ZERO
from .events import EventLog, now_iso
from .graph import Graph
from .queue import write_queues
from .score import rank_ready, score_task
from .state import State
from .store import KINDS, QUEUES, RepoError, Store, find_root

HOOK = """#!/bin/sh
# scriptorium pre-commit: refresh derived views + snapshot, then verify.
# Uses the interpreter that ran `scr install-hooks`, so a venv need not be active.
set -e
PY="{python}"
"$PY" -m scr refresh --quiet
git add state/STATE.json graph/graph.json queues/scribers.md queues/omri.md 2>/dev/null || true
"$PY" -m scr verify --quiet
"""


def _ctx(args) -> tuple[Path, Store, EventLog]:
    root = find_root(Path(args.root) if getattr(args, "root", None) else None)
    return root, Store(root), EventLog(root / "events" / "events.jsonl")


def refresh(root: Path, quiet: bool = False, note: str | None = None) -> dict:
    st = State(root)
    store = Store(root)
    g = Graph.build(store.load_all())
    write_queues(root, g, store.config("weights"), store.config("limits"))
    snap = st.snapshot(note)
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
    for d in ("tasks", "config", "graph", "events", "state", "queues", "agents", "templates", "reports/daily", "reports/weekly"):
        (root / d).mkdir(parents=True, exist_ok=True)
    log = EventLog(root / "events" / "events.jsonl")
    if not log.path.exists() or log.head()[1] == 0:
        log.append("system", name="genesis", actor=args.actor, note="scriptorium initialized")
    refresh(root, note="init")


def cmd_task_new(args):
    root, store, log = _ctx(args)
    tasks = store.load_all()
    tid = args.id or store.next_id()
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
    lifecycle.apply(t.rec, args.status)
    if args.tokens:
        t.rec["tokens_used"] = int(t.rec.get("tokens_used", 0)) + args.tokens
    t.save()
    log.append("state", task=args.id, **{"from": old}, to=args.status, actor=args.actor, tokens=args.tokens or None,
               note=(args.note or "") + (" [forced]" if args.force else "") or None)
    if args.status == "dispatched":
        log.append("field", task=args.id, field="attempts", value=t.rec["attempts"], actor=args.actor)
    if args.tokens:
        log.append("field", task=args.id, field="tokens_used", value=t.rec["tokens_used"], actor=args.actor)
    # cascade: a blocked task whose blockers are now all done becomes ready; done -> closed frees dependents
    if args.status in {"merged", "closed", "rejected"}:
        g = Graph.build(store.load_all())
        for d in g.succ(g.blocks).get(args.id, []):
            dt = g.tasks[d]
            if dt.status in {"triaged", "blocked"} and g.ready_now(d) and dt.rec.get("queue") == "scribers":
                lifecycle.apply(dt.rec, "ready")
                dt.save()
                log.append("state", task=d, **{"from": "blocked" if dt.status == "blocked" else "triaged"}, to="ready", actor="scr", note=f"unblocked by {args.id}")
            if args.status == "rejected" and dt.is_open:
                log.append("system", name="blocker_rejected", actor="scr", data={"task": d, "blocker": args.id}, note="Gate 2: rewrite, reparent, or drop")
    print(f"{args.id}: {old} -> {args.status}")
    refresh(root, args.quiet)


BRIEF_TEMPLATE = """Use the scriber agent. This is Scriptorium task {id}: {title}
Your brief is {spec} — read it in full; its Scope and Definition of done are binding, and anything
outside Scope is read-only. Work only inside this worktree ({worktree}); never commit, never switch
branch. Keep an honest run log in {log} (append-only; put any question for the Steward on a line
starting `HELP:` and keep working on what does not depend on it). When done, write {report} using the
template already there — fill "Statement changes" truthfully (any entry routes to Gate 1) and the
`unlocks` block. Budget: {attempts} attempt(s), {tokens:,} tokens, {hours} h wall.{stmt}"""


def cmd_task_brief(args):
    root, store, log = _ctx(args)
    t = store.get(args.id)
    d = t.dir
    wl = (store.config("workloads") or {}).get(t.rec.get("workload") or "", {})
    wt = t.rec.get("worktree") or f"{wl.get('worktrees', '../worktrees')}/{t.id}"
    wt_abs = (root / wt).resolve()
    b = t.rec.get("budget") or {}
    stmt = ("\n\nThis task may need NEW or CHANGED statements (may_touch_statements: true). Freeze them in the "
            "mission's TARGETS.md and HALT there once for Omri's approval (Gate 1) before proving anything."
            if (t.rec.get("scope") or {}).get("may_touch_statements") else "")
    print(BRIEF_TEMPLATE.format(
        id=t.id, title=t.rec.get("title", ""), spec=(d / "spec.md").resolve(), worktree=wt_abs,
        log=(d / "log.md").resolve(), report=(d / "report.md").resolve(),
        attempts=b.get("max_attempts", 3), tokens=int(b.get("max_tokens", 0)), hours=b.get("max_wall_hours", 6), stmt=stmt))


def cmd_task_field(args):
    root, store, log = _ctx(args)
    t = store.get(args.id)
    val = yaml.safe_load(args.value)
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


def cmd_dispatch(args):
    root, store, log = _ctx(args)
    limits = store.config("limits")
    g = Graph.build(store.load_all())
    running = [t.id for t in g.tasks.values() if t.status in {"dispatched", "in_progress", "checkpoint", "submitted"}]
    cap = int(limits.get("concurrency", 3)) - len(running)
    rows = rank_ready(g, store.config("weights"), "scribers")
    ranked = [r["task"] for r in rows]
    if not args.allow_statements:
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
        wl = (store.config("workloads") or {}).get(t.rec.get("workload") or "", {})
        wt_root = wl.get("worktrees") or "../worktrees"
        t.rec["worktree"] = f"{wt_root}/{tid}"
        t.rec["branch"] = f"task/{tid}"
        t.save()
        log.append("state", task=tid, **{"from": old}, to="dispatched", actor=args.actor, note=f"attempt {t.rec['attempts']}")
        log.append("field", task=tid, field="attempts", value=t.rec["attempts"], actor=args.actor)
        print(f"dispatched {tid} -> {t.rec['branch']} (attempt {t.rec['attempts']})")
    if not chosen:
        log.append("system", name="intake_starved" if not rows else "dispatch_blocked", actor=args.actor,
                   data={"ready": len(rows), "running": running})
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


def cmd_verify(args):
    root, _, _ = _ctx(args)
    r = State(root).verify()
    if not args.quiet or r["errors"]:
        for e in r["errors"]:
            print("ERROR  ", e)
        for w in r["warnings"]:
            if not args.quiet:
                print("warn   ", w)
        print(f"state_root {r['state_root'][:16]}  {len(r['errors'])} errors, {len(r['warnings'])} warnings")
    sys.exit(1 if r["errors"] else 0)


def cmd_replay(args):
    root, store, log = _ctx(args)
    _out(log.replay() if not args.task else list(log.iter_task(args.task)), args.json)


def cmd_history(args):
    root, _, _ = _ctx(args)
    try:
        out = subprocess.run(["git", "log", "--format=%H %cI", "--", "state/STATE.json"], cwd=root, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        print("no git history")
        return
    for line in out.splitlines()[: args.n]:
        sha, ts = line.split()
        try:
            snap = json.loads(subprocess.run(["git", "show", f"{sha}:state/STATE.json"], cwd=root, capture_output=True, text=True, check=True).stdout)
            print(f"{ts}  {sha[:10]}  state {snap['state_root'][:12]}  parent {snap['parent_state_root'][:12]}  tasks {len(snap['tasks'])}  events {snap['events_count']}" + (f"  {snap['note']}" if snap.get("note") else ""))
        except Exception as e:
            print(f"{ts}  {sha[:10]}  (unreadable snapshot: {e})")


def cmd_diff(args):
    root, _, _ = _ctx(args)
    st = State(root)

    def load(ref: str) -> dict:
        if ref in ("WORKTREE", "."):
            return st.read_snapshot() or {}
        return json.loads(subprocess.run(["git", "show", f"{ref}:state/STATE.json"], cwd=root, capture_output=True, text=True, check=True).stdout)

    _out(st.diff(load(args.a), load(args.b)), args.json)


def cmd_install_hooks(args):
    root, _, _ = _ctx(args)
    hooks = root / ".git" / "hooks"
    if not hooks.exists():
        raise RepoError("not a git repo (run git init first)")
    p = hooks / "pre-commit"
    py = Path(sys.executable).as_posix()   # forward slashes work in Git Bash on Windows too
    p.write_text(HOOK.replace("{python}", py), encoding="utf-8")
    p.chmod(0o755)
    print(f"installed {p}")


def cmd_ui(args):
    from .ui import serve
    root, _, _ = _ctx(args)
    serve(root, args.port, not args.no_browser)


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
    n.add_argument("--risk", type=float, default=0.2); n.add_argument("--max-attempts", type=int, default=3)
    n.add_argument("--max-tokens", type=int, default=2_000_000); n.add_argument("--max-wall-hours", type=float, default=6); n.add_argument("--note")
    n.set_defaults(fn=cmd_task_new)
    st = t.add_parser("set", parents=[common], help="status transition")
    st.add_argument("id"); st.add_argument("status"); st.add_argument("--note"); st.add_argument("--tokens", type=int); st.add_argument("--force", action="store_true")
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
    d.add_argument("--dry-run", action="store_true"); d.add_argument("--allow-statements", action="store_true"); d.set_defaults(fn=cmd_dispatch)

    ev = sub.add_parser("event", parents=[common], help="append a system/checkin event")
    ev.add_argument("kind", choices=["system", "checkin"]); ev.add_argument("--name"); ev.add_argument("--task"); ev.add_argument("--note"); ev.add_argument("--data", help="YAML/JSON"); ev.add_argument("--op")
    ev.set_defaults(fn=cmd_event)
    ci = sub.add_parser("checkin", parents=[common], help="omri opens/closes a check-in (for the minutes metric)"); ci.add_argument("op", choices=["open", "close"]); ci.add_argument("--note"); ci.set_defaults(fn=cmd_checkin)

    r = sub.add_parser("refresh", parents=[common], help="regenerate graph.json, queues, STATE.json"); r.add_argument("--note"); r.set_defaults(fn=cmd_refresh)
    sub.add_parser("snapshot", parents=[common], help="alias of refresh").set_defaults(fn=cmd_refresh, note=None)
    sub.add_parser("verify", parents=[common], help="check snapshot, chain, replay, invariants; exit 1 on error").set_defaults(fn=cmd_verify)
    rp = sub.add_parser("replay", parents=[common], help="derived state from the event log"); rp.add_argument("--task"); rp.set_defaults(fn=cmd_replay)
    h = sub.add_parser("history", parents=[common], help="snapshot chain across git commits"); h.add_argument("-n", type=int, default=20); h.set_defaults(fn=cmd_history)
    df = sub.add_parser("diff-state", parents=[common], help="compare two snapshots by hash (git refs, or WORKTREE)"); df.add_argument("a"); df.add_argument("b", nargs="?", default="WORKTREE"); df.set_defaults(fn=cmd_diff)
    sub.add_parser("install-hooks", parents=[common], help="pre-commit: refresh + verify").set_defaults(fn=cmd_install_hooks)
    ui = sub.add_parser("ui", parents=[common], help="serve the Scriptorium UI on localhost (Omri's entry point)")
    ui.add_argument("--port", type=int, default=8765); ui.add_argument("--no-browser", action="store_true"); ui.set_defaults(fn=cmd_ui)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except RepoError as e:
        print("error:", e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
