"""Scriptorium UI: a local web page that is Omri's only surface. `scr ui` (supervisor) runs
`scr ui --child` (the server) and restarts it whenever the code changes on disk, so an update
is live within seconds and "did it load" is never a question.

Endpoints:
  GET  /                      the page (ui/index.html)
  GET  /api/state             needs-you cards, running/ready/recent, health, jobs
  GET  /api/task/<id>         record + docs + events
  GET  /api/events?task=&n=
  GET  /api/jobs              jobs started from this UI (running / done / failed), newest first
  GET  /api/file?path=        read a file under either repo
  POST /api/run               {kind: chore|answer|discuss|inbox|ack, ...} -> {job} (asynchronous)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import chores as chores_mod
from .events import EventLog, now_iso
from .graph import Graph
from .journal import last_failure
from .run import _env, scr_cmd, scr_exe
from .state import State
from .store import Store, find_root

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
CODE_DIR = Path(__file__).resolve().parent
RELOAD_EXIT = 3


def _read(p: Path, limit: int = 400_000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _workload(root: Path, store: Store) -> tuple[str, dict]:
    wls = store.config("workloads") or {}
    name = next(iter(wls), "")
    return name, (wls.get(name) or {})


OPTIONS_RE = re.compile(r"\*\*Answer here[^*]*\*\*[^\n]*?((?:`[^`]+`\s*/?\s*)+)")


def _options(spec: str) -> list[str]:
    m = OPTIONS_RE.search(spec)
    return re.findall(r"`([^`]+)`", m.group(1)) if m else []


def _help_lines(log: str) -> list[dict]:
    lines = log.splitlines()
    out = []
    for i, ln in enumerate(lines):
        if re.match(r"^\s*(-\s*)?HELP:", ln):
            answered = False
            for j in range(i + 1, len(lines)):
                if "[steward]" in lines[j]:
                    answered = True; break
                if re.match(r"^\s*(-\s*)?HELP:", lines[j]):
                    break
            if not answered:
                ctx = [ln.strip()]
                for j in range(i + 1, min(i + 8, len(lines))):
                    if lines[j].startswith("  ") and lines[j].strip():
                        ctx.append(lines[j].strip())
                    else:
                        break
                out.append({"line": i + 1, "text": " ".join(ctx)})
    return out


def _events(root: Path, task: str | None = None, n: int = 200) -> list[dict]:
    rows = []
    for e in EventLog(root / "events" / "events.jsonl").read():
        if task and e.get("task") != task and not (e.get("kind") == "edge" and task in (e.get("from"), e.get("to"))):
            continue
        rows.append(e)
    return rows[-n:]


def _age_h(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return round((datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 3600, 1)
    except ValueError:
        return None


def _started(root: Path, tid: str) -> str | None:
    ts = None
    for e in _events(root, tid, 500):
        if e.get("kind") == "state" and e.get("to") == "in_progress":
            ts = e.get("ts")
    return ts


_CHORE_CACHE: dict = {"ts": 0.0, "items": []}

# System events that become a "needs you" card, one per (task, name). `stale` is a live condition: its card
# shows only while the watcher's `stale:<task>` flag is set (idle >= stale_minutes, session alive) and
# clears by itself when the scriber writes again; `session_dead` stays until dismissed (T-0063).
ALARM_EVENTS = {"verify_failed", "budget_wall", "blocker_rejected", "scope_drift", "leak", "checkpoint_skipped", "session_dead", "stale", "slot_freed"}
ALARM_TITLES = {"session_dead": "scriber session died — task returned", "stale": "STALE: session alive but silent",
                "slot_freed": "escalated: a stuck task left its slot — one action for you"}
RUNNING = {"dispatched", "in_progress", "checkpoint"}


def _watch_flags(root: Path) -> set[str]:
    return set(_read(root / ".scr" / "watch.state.txt").split())


def chore_cards(root: Path, acked: set[str], max_age: float = 120.0) -> list[dict]:
    if time.time() - _CHORE_CACHE["ts"] < max_age:
        return _CHORE_CACHE["items"]
    items = [{"key": f"chore:{c['id']}", "id": c["id"], "type": "chore", "kind": "manual" if c["manual"] else "run",
              "title": c["title"], "text": c["why"], "link": c["link"], "options": [], "runnable": c["runnable"]}
             for c in chores_mod.status(root, acked)]
    _CHORE_CACHE.update(ts=time.time(), items=items)
    return items


def invalidate_chores() -> None:
    _CHORE_CACHE["ts"] = 0.0


def state(root: Path) -> dict:
    store = Store(root)
    tasks = store.load_all()
    g = Graph.build(tasks)
    wl_name, wl = _workload(root, store)
    limits = store.config("limits") or {}
    acked = set(_read(root / "reports" / "ui.acks.txt").split())
    needs: list[dict] = []
    for t in tasks.values():
        rec, d = t.rec, t.dir
        if rec.get("queue") == "omri" and t.is_open:
            waiting = [x for x in g.descendants(t.id) if g.tasks[x].is_open]
            needs.append({"key": f"decision:{t.id}", "id": t.id, "kind": rec.get("kind"), "title": rec.get("title", ""), "status": t.status,
                          "type": "decision", "options": _options(_read(d / "spec.md")), "waiting": waiting,
                          "created": rec.get("created"), "age_h": _age_h(rec.get("created"))})
        if t.status in {"in_progress", "checkpoint", "dispatched", "submitted"}:
            for h in _help_lines(_read(d / "log.md")):
                needs.append({"key": f"help:{t.id}:{h['line']}", "id": t.id, "kind": "help", "title": rec.get("title", ""), "status": t.status,
                              "type": "help", "text": h["text"], "line": h["line"], "options": []})
        if t.status == "escalated" or (t.status == "omri_queue" and rec.get("queue") == "scribers"):
            needs.append({"key": f"gate:{t.id}", "id": t.id, "kind": "gate", "title": rec.get("title", ""), "status": t.status,
                          "type": "gate", "options": ["accept", "return", "reject"], "text": "escalated by the Steward — see review.md"})
    # System alarms: ONE card per (task, alarm kind), not one per event — a watcher that logs per file can
    # otherwise produce hundreds of cards (583 on 2026-09-16). A card's key `events:<task>:<name>:<latest ts>`
    # acks every event of that task and kind up to that timestamp in one click; old per-event acks
    # (`event:<hash>` lines) still count.
    group_acked: dict[tuple[str, str], str] = {}
    for k in acked:
        if k.startswith("events:"):
            parts = k.split(":", 3)
            if len(parts) == 4:
                group_acked[(parts[1], parts[2])] = max(group_acked.get((parts[1], parts[2]), ""), parts[3])
    groups: dict[tuple[str, str], dict] = {}
    flags = _watch_flags(root)
    for e in _events(root, None, 2000):
        age = _age_h(e.get("ts"))
        if age is not None and age > 24 * 7:
            continue
        if e.get("kind") != "system" or e.get("name") not in ALARM_EVENTS:
            continue
        task, name, ts = e.get("task") or "-", e.get("name"), e.get("ts") or ""
        if e.get("h") in acked or ts <= group_acked.get((task, name), ""):
            continue
        if name == "stale" and not (f"stale:{task}" in flags and task in tasks and tasks[task].status in RUNNING):
            continue
        grp = groups.setdefault((task, name), {"id": task, "kind": "system", "name": name, "title": ALARM_TITLES.get(name, name), "type": "system",
                                               "options": [], "ts": ts, "count": 0, "notes": []})
        grp["count"] += 1
        grp["ts"] = max(grp["ts"], ts)
        grp["notes"].append(e.get("note") or json.dumps(e.get("data") or {}))
    for (task, name), grp in groups.items():
        notes = grp.pop("notes")
        shown = notes[-8:]
        grp["text"] = "\n".join(shown) + (f"\n… and {len(notes) - len(shown)} earlier" if len(notes) > len(shown) else "")
        if grp["count"] > 1:
            grp["title"] = f"{grp['title']} ×{grp['count']}"
        grp["key"] = grp["h"] = f"events:{task}:{name}:{grp['ts']}"
        needs.append(grp)
    needs += chore_cards(root, acked)
    from . import hotfix as _hx
    hx = _hx.status(root)
    if hx:
        needs.insert(0, {"key": f"hotfix:{hx['id']}", "id": hx["id"], "kind": "hotfix", "type": "hotfix", "title": hx["title"], "status": hx["status"],
                         "text": "A tooling fix is in progress in the live checkout. Files so far: " + (", ".join(hx["files"]) or "(none yet)") +
                                 ". When the fix is done, close it: the tests run, a report is written and the Steward reviews it.",
                         "options": []})
    running = [{"id": t.id, "status": t.status, "title": t.rec.get("title", ""), "kind": t.rec.get("kind"),
                "attempts": t.rec.get("attempts", 0), "tokens": t.rec.get("tokens_used", 0), "started": _started(root, t.id)}
               for t in tasks.values() if t.status in {"dispatched", "in_progress", "checkpoint", "submitted", "accepted"}]
    ready = [{"id": t.id, "title": t.rec.get("title", ""), "kind": t.rec.get("kind")} for t in tasks.values() if t.status == "ready"]
    recent = []
    for e in reversed(_events(root, None, 2000)):
        if e.get("kind") == "state" and e.get("to") in {"merged", "closed", "rejected"} and e.get("task") in tasks:
            recent.append({"id": e["task"], "to": e["to"], "ts": e.get("ts"), "title": tasks[e["task"]].rec.get("title", "")})
        if len(recent) >= 12:
            break
    watch_log = root / "reports" / "steward_watch.log"
    last_tick = datetime.fromtimestamp(watch_log.stat().st_mtime, tz=timezone.utc).isoformat() if watch_log.exists() else None
    from .cli import refresh
    try:
        refresh(root, quiet=True)
    except Exception:  # noqa: BLE001 — a scriber mid-write must not break the page
        pass
    v = State(root).verify()
    snap = json.loads(_read(root / "state" / "STATE.json") or "{}")
    from .gitops import Git
    gj = Git(root)
    head = gj.head(short=True) if gj.is_repo() else None
    return {
        "generated": now_iso(), "workload": wl_name, "repo": str((root / wl.get("repo", "../" + wl_name)).resolve()),
        "trusted_branch": wl.get("trusted_branch"), "main_branch": wl.get("main_branch", "master"),
        "concurrency": int(limits.get("concurrency", 3)),
        "verify_ok": not v["errors"], "verify_out": "; ".join(v["errors"][:3])[:400], "state_root": (snap.get("state_root") or "")[:12],
        "journal": {"head": head, "dirty": len(gj.dirty()) if head else 0, "last_failure": last_failure(root)},
        "last_watch_tick": last_tick, "pid": os.getpid(), "paused": paused(root),
        "watcher_running": bool(__import__("scr.run", fromlist=["pids_matching"]).pids_matching(__import__("scr.admin", fromlist=["WATCH_PATTERN"]).WATCH_PATTERN)),
        "needs": sorted(needs, key=lambda x: {"hotfix": -1, "chore": 0, "gate": 1, "help": 2, "decision": 3, "system": 4}[x["type"]]),
        "running": running, "ready": ready, "recent": recent,
        "jobs": JOBS.list(),
        "counts": {s: sum(1 for t in tasks.values() if t.status == s) for s in
                   ("ready", "dispatched", "in_progress", "submitted", "accepted", "merged", "closed", "blocked", "omri_queue")},
    }


def task_detail(root: Path, tid: str) -> dict:
    store = Store(root)
    t = store.get(tid)
    d = t.dir
    docs = {n: _read(d / f"{n}.md") for n in ("spec", "log", "report", "review")}
    g = Graph.build(store.load_all())
    return {"id": t.id, "rec": t.rec, "docs": docs, "events": _events(root, tid, 300),
            "options": _options(docs["spec"]), "help": _help_lines(docs["log"]),
            "blocks": sorted(g.succ(g.blocks).get(tid, [])), "blocked_by": t.rec.get("blocked_by") or [],
            "worktree": str((root / (t.rec.get("worktree") or "")).resolve()) if t.rec.get("worktree") else None}


def read_file(root: Path, rel: str) -> dict:
    store = Store(root)
    wl_name, wl = _workload(root, store)
    repo = (root / wl.get("repo", "../" + wl_name)).resolve()
    for base in (root, repo):
        p = (base / rel).resolve()
        if str(p).startswith(str(base)) and p.is_file():
            return {"path": str(p), "text": _read(p, 2_000_000)}
    return {"error": "not found or outside the repos"}


# ---- jobs: everything a button does runs asynchronously and lands in the Done list --------------
class Jobs:
    """In-memory + .scr/jobs.jsonl (so a restart keeps today's Done list)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict[str, dict] = {}
        self.root: Path | None = None

    def load(self, root: Path) -> None:
        self.root = root
        p = root / ".scr" / "jobs.jsonl"
        for ln in _read(p).splitlines():
            try:
                j = json.loads(ln)
                if _age_h(j.get("finished") or j.get("started")) is not None and _age_h(j.get("finished") or j.get("started")) < 48:
                    self.items[j["id"]] = j
            except (json.JSONDecodeError, KeyError):
                pass
        for j in self.items.values():          # a job that was running when the server died did not finish
            if j["state"] == "running":
                j["state"] = "failed"; j["output"] = (j.get("output") or "") + "\n(UI restarted while this ran)"

    def _persist(self, j: dict) -> None:
        if not self.root:
            return
        p = self.root / ".scr" / "jobs.jsonl"
        p.parent.mkdir(exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(j, default=str) + "\n")

    def start(self, kind: str, key: str, title: str, fn) -> dict:
        jid = uuid.uuid4().hex[:10]
        j = {"id": jid, "kind": kind, "key": key, "title": title, "state": "running", "started": now_iso(), "finished": None, "output": "", "ok": None}
        with self.lock:
            self.items[jid] = j

        def run():
            try:
                res = fn()
                ok = bool(res.get("ok", True)) if isinstance(res, dict) else True
                out = res.get("output") if isinstance(res, dict) else "\n".join(map(str, res if isinstance(res, list) else [res]))
                if isinstance(res, dict) and res.get("log"):
                    out = (out or "") + "\n".join(res["log"])
                j.update(state="done" if ok else "failed", ok=ok, output=(out or "")[-8000:])
            except Exception as e:  # noqa: BLE001
                j.update(state="failed", ok=False, output=f"{type(e).__name__}: {e}")
            j["finished"] = now_iso()
            invalidate_chores()
            self._persist(j)
        threading.Thread(target=run, daemon=True).start()
        return j

    def list(self) -> list[dict]:
        with self.lock:
            return sorted(self.items.values(), key=lambda j: j["started"], reverse=True)[:60]


JOBS = Jobs()


PAUSE_FILE = ".scr/paused"


def paused(root: Path) -> bool:
    return (root / PAUSE_FILE).exists()


def _set_paused(root: Path, on: bool) -> dict:
    p = root / PAUSE_FILE
    p.parent.mkdir(exist_ok=True)
    if on:
        p.write_text(now_iso(), encoding="utf-8")
    elif p.exists():
        p.unlink()
    scr_cmd(root, "omri", "event", "system", "--name", "watcher_paused" if on else "watcher_resumed", "--note", "via UI", "--quiet")
    return {"ok": True, "output": "watcher paused: ticks only log 'paused' until you resume" if on else "watcher resumed"}


def _quit(root: Path) -> dict:
    from .admin import stop
    lines = stop(root, "watch")
    if any(l.startswith("FAILED") for l in lines):          # T-0150: a surviving watcher must not cost Omri the UI
        return {"ok": False, "output": "\n".join(lines + [
            "UI stays up: the watcher pid above is still alive. Retry Quit, run `scr admin stop watch`, "
            "or end the pid in Task Manager"])}
    threading.Timer(1.0, lambda: os._exit(0)).start()      # the child exits 0 -> the supervisor stops too
    return {"ok": True, "output": "\n".join(lines + ["UI shutting down; start it again from the Scriptorium shortcut"])}


def run_action(root: Path, data: dict) -> dict:
    kind = data.get("kind")
    if kind == "pause":
        return JOBS.start("pause", "system:pause", "pause the watcher", lambda: _set_paused(root, True))
    if kind == "resume":
        return JOBS.start("resume", "system:resume", "resume the watcher", lambda: _set_paused(root, False))
    if kind == "quit":
        return JOBS.start("quit", "system:quit", "shut Scriptorium down", lambda: _quit(root))
    if kind == "chore":
        cid = data["id"]
        return JOBS.start("chore", f"chore:{cid}", data.get("title") or cid, lambda: _chore(root, cid))
    if kind == "answer":
        tid, ans, k = data["task"], data["answer"], data.get("answer_kind", "decision")
        from .answers import apply
        return JOBS.start("answer", data.get("key") or f"{k}:{tid}", f"{tid}: {ans[:60]}", lambda: apply(root, tid, ans, k))
    if kind == "discuss":
        item = data["item"]
        from .discuss import discuss
        return JOBS.start("discuss", f"discuss:{item}", f"discuss {item}", lambda: {"ok": True, "output": "\n".join(discuss(root, item))})
    if kind == "inbox":
        from .answers import add_inbox
        return JOBS.start("inbox", "inbox", data["text"][:60], lambda: add_inbox(root, data["text"]))
    if kind == "hotfix_open":
        symptom = (data.get("symptom") or "").strip() or "opened from the UI"
        def _open():
            rc, out = scr_cmd(root, "hotfix:omri", "hotfix", "open", symptom)
            from .discuss import discuss
            tid = out.split()[0] if out.startswith("T-") else None
            return {"ok": rc == 0, "output": out + "\n" + "\n".join(discuss(root, f"hotfix:{tid}" if tid else "hotfix"))}
        return JOBS.start("hotfix", "hotfix:new", f"fix a bug: {symptom[:50]}", _open)
    if kind == "hotfix_close":
        tid, summary = data.get("id"), (data.get("summary") or "").strip() or "closed from the UI"
        def _close():
            rc, out = scr_cmd(root, "hotfix:omri", "hotfix", "close", *( [tid] if tid else []), "--summary", summary, timeout=3600)
            return {"ok": rc == 0, "output": out}
        return JOBS.start("hotfix", f"hotfix:{tid}", f"close hotfix {tid}", _close)
    if kind == "ack":
        key = data["key"]
        return JOBS.start("ack", key, f"dismiss {key}", lambda: _ack(root, key))
    return {"error": f"unknown action {kind}"}


def _chore(root: Path, cid: str) -> dict:
    rc, out = scr_cmd(root, "omri", "chore", "run", cid, timeout=7200)
    return {"ok": rc == 0, "output": out}


def _ack(root: Path, key: str) -> dict:
    rc, out = scr_cmd(root, "omri", "ack", key)
    return {"ok": rc == 0, "output": out}


# ---- HTTP -------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    root: Path = Path(".")

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                body = (UI_DIR / "index.html").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif u.path == "/api/state":
                self._json(state(self.root))
            elif u.path.startswith("/api/task/"):
                self._json(task_detail(self.root, u.path.rsplit("/", 1)[1]))
            elif u.path == "/api/events":
                self._json(_events(self.root, (q.get("task") or [None])[0], int((q.get("n") or ["200"])[0])))
            elif u.path == "/api/jobs":
                self._json(JOBS.list())
            elif u.path == "/api/file":
                self._json(read_file(self.root, (q.get("path") or [""])[0]))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001 — surface, don't crash the server
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/api/run":
                self._json(run_action(self.root, data))
            # backwards-compatible aliases for the old page
            elif u.path == "/api/chore":
                self._json(run_action(self.root, {"kind": "chore", "id": data["id"]}))
            elif u.path == "/api/answer":
                self._json(run_action(self.root, {"kind": "answer", "task": data["task"], "answer": data["answer"], "answer_kind": data.get("kind", "decision")}))
            elif u.path == "/api/inbox":
                self._json(run_action(self.root, {"kind": "inbox", "text": data["text"]}))
            elif u.path == "/api/ack":
                self._json(run_action(self.root, {"kind": "ack", "key": data.get("key") or data.get("h")}))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)


# ---- server + supervisor ---------------------------------------------------------------------
def _code_signature() -> float:
    files = list(CODE_DIR.glob("*.py")) + [UI_DIR / "index.html", UI_DIR / "chores.yaml"]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


def serve_child(root: Path, port: int) -> int:
    Handler.root = root
    (root / "reports").mkdir(exist_ok=True)
    JOBS.load(root)
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    sig = _code_signature()
    rc = {"code": 0}

    def watch_code():
        while True:
            time.sleep(2)
            if _code_signature() > sig + 0.5:
                rc["code"] = RELOAD_EXIT
                srv.shutdown()
                return
    threading.Thread(target=watch_code, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    return rc["code"]


def serve(root: Path, port: int = 8765, open_browser: bool = True, child: bool = False):
    if child:
        sys.exit(serve_child(root, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"Scriptorium UI at {url}  (Ctrl+C to stop; restarts itself when its code changes)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    while True:
        # same interpreter as the supervisor (pythonw stays windowless), never scr.exe from here
        rc = subprocess.call([sys.executable, "-m", "scr", "--root", str(root), "ui", "--port", str(port), "--no-browser", "--child"], cwd=root, env=_env())
        if rc != RELOAD_EXIT:
            break
        print("UI code changed on disk — restarting")
        time.sleep(0.5)


if __name__ == "__main__":
    serve(find_root(), int(os.environ.get("SCR_UI_PORT", "8765")))
