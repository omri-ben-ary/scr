"""Scriptorium UI — Omri's entry point. `scr ui` serves ui/index.html on localhost and a small
JSON API over the records. Stdlib only (no new dependencies); the chat and the concierge use the
Claude Code CLI (`claude -p`) with the operator's existing login, never an API key.

Endpoints (all JSON):
  GET  /api/state                     needs-you items, running, recent, health
  GET  /api/task/<id>                 record + spec/log/report/review + events for the task
  GET  /api/events?task=<id>&n=200    event slice (history)
  POST /api/answer   {task, answer, kind}   apply an answer (decide/approve/help/gate1) and log it
  POST /api/inbox    {text}                 one idea line → Inbox (Intake turns it into a task)
  POST /api/chat     {thread, message, task?}  ask the agent; returns {reply, session}
  GET  /api/chat/<thread>             the thread transcript
  GET  /api/file?path=<repo-relative> read a file from the scriptorium or workload repo (read-only)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from .events import EventLog, now_iso
from .graph import Graph
from .store import Store, find_root

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
LOCK = threading.Lock()   # serialise scr mutations


def _scr(root: Path, actor: str, *args: str) -> tuple[int, str]:
    exe = root / ".venv" / "Scripts" / "scr.exe"
    cmd = [str(exe)] if exe.exists() else [sys.executable, "-m", "scr"]
    cmd += ["--root", str(root), "--actor", actor, *args]
    with LOCK:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    return r.returncode, (r.stdout + r.stderr).strip()


def _workload(root: Path, store: Store) -> tuple[str, dict]:
    wls = store.config("workloads") or {}
    name = next(iter(wls), "")
    return name, (wls.get(name) or {})


def _read(p: Path, limit: int = 400_000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


OPTIONS_RE = re.compile(r"\*\*Answer here[^*]*\*\*[^\n]*?((?:`[^`]+`\s*/?\s*)+)")


def _options(spec: str) -> list[str]:
    m = OPTIONS_RE.search(spec)
    if not m:
        return []
    return re.findall(r"`([^`]+)`", m.group(1))


def _help_lines(log: str) -> list[dict]:
    """HELP: lines with no [steward] line after them (before the next HELP:)."""
    lines = log.splitlines()
    out = []
    for i, ln in enumerate(lines):
        if re.match(r"^\s*(-\s*)?HELP:", ln):
            answered = False
            for j in range(i + 1, len(lines)):
                if "[steward]" in lines[j]:
                    answered = True
                    break
                if re.match(r"^\s*(-\s*)?HELP:", lines[j]):
                    break
            if not answered:
                # include continuation lines (indented) as context
                ctx = [ln.strip()]
                for j in range(i + 1, min(i + 8, len(lines))):
                    if lines[j].startswith("  ") and lines[j].strip():
                        ctx.append(lines[j].strip())
                    else:
                        break
                out.append({"line": i + 1, "text": " ".join(ctx)})
    return out


def _events(root: Path, task: str | None = None, n: int = 200) -> list[dict]:
    p = root / "events" / "events.jsonl"
    rows = []
    for ln in _read(p, 50_000_000).splitlines():
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if task and e.get("task") != task and not (e.get("kind") == "edge" and task in (e.get("from"), e.get("to"))):
            continue
        rows.append(e)
    return rows[-n:]


def state(root: Path) -> dict:
    store = Store(root)
    tasks = store.load_all()
    g = Graph.build(tasks)
    wl_name, wl = _workload(root, store)
    limits = store.config("limits") or {}
    needs: list[dict] = []
    for t in tasks.values():
        rec = t.rec
        d = t.dir
        if rec.get("queue") == "omri" and t.is_open:
            spec = _read(d / "spec.md")
            waiting = [x for x in g.descendants(t.id) if g.tasks[x].is_open]
            needs.append({"id": t.id, "kind": rec.get("kind"), "title": rec.get("title", ""), "status": t.status,
                          "type": "decision", "options": _options(spec), "waiting": waiting,
                          "created": rec.get("created"), "age_h": _age_h(rec.get("created"))})
        if t.status in {"in_progress", "checkpoint", "dispatched", "submitted"}:
            for h in _help_lines(_read(d / "log.md")):
                needs.append({"id": t.id, "kind": "help", "title": rec.get("title", ""), "status": t.status,
                              "type": "help", "text": h["text"], "line": h["line"], "options": []})
        if t.status == "escalated" or (t.status == "omri_queue" and rec.get("queue") == "scribers"):
            needs.append({"id": t.id, "kind": "gate", "title": rec.get("title", ""), "status": t.status,
                          "type": "gate", "options": ["accept", "return", "reject"],
                          "text": "escalated by the Steward — see review.md"})
    for e in _events(root, None, 400):
        age = _age_h(e.get("ts"))
        if age is not None and age > 24 * 7:
            continue   # alarms older than a week are history, not an inbox
        if e.get("kind") == "system" and e.get("name") in {"verify_failed", "budget_wall", "blocker_rejected", "scope_drift", "leak"}:
            needs.append({"id": e.get("task") or "-", "kind": "system", "title": e.get("name"), "type": "system",
                          "text": e.get("note") or json.dumps(e.get("data") or {}), "ts": e.get("ts"), "options": [],
                          "h": e.get("h")})
    # keep only system events not yet acknowledged
    ack = set(_read(root / "reports" / "ui.acks.txt").split())
    needs = [x for x in needs if x.get("type") != "system" or x.get("h") not in ack]
    needs += chores(root)
    running = [{"id": t.id, "status": t.status, "title": t.rec.get("title", ""), "kind": t.rec.get("kind"),
                "attempts": t.rec.get("attempts", 0), "tokens": t.rec.get("tokens_used", 0),
                "started": _started(root, t.id)}
               for t in tasks.values() if t.status in {"dispatched", "in_progress", "checkpoint", "submitted", "accepted"}]
    ready = [{"id": t.id, "title": t.rec.get("title", ""), "kind": t.rec.get("kind")} for t in tasks.values() if t.status == "ready"]
    recent = []
    for e in reversed(_events(root, None, 2000)):
        if e.get("kind") == "state" and e.get("to") in {"merged", "closed", "rejected"} and e.get("task") in tasks:
            recent.append({"id": e["task"], "to": e["to"], "ts": e.get("ts"), "title": tasks[e["task"]].rec.get("title", "")})
        if len(recent) >= 12:
            break
    watch_log = root / "reports" / "steward_watch.log"
    last_tick = None
    if watch_log.exists():
        last_tick = datetime.fromtimestamp(watch_log.stat().st_mtime, tz=timezone.utc).isoformat()
    _scr(root, "ui", "refresh", "--quiet")   # scribers change docs continuously; refresh like the pre-commit hook does
    rc, vout = _scr(root, "ui", "verify", "--quiet")
    snap = json.loads(_read(root / "state" / "STATE.json") or "{}")
    return {
        "generated": now_iso(), "workload": wl_name, "repo": str((root / wl.get("repo", "../" + wl_name)).resolve()),
        "trusted_branch": wl.get("trusted_branch"), "main_branch": wl.get("main_branch", "master"),
        "concurrency": int(limits.get("concurrency", 3)),
        "verify_ok": rc == 0, "verify_out": vout[-400:], "state_root": (snap.get("state_root") or "")[:12],
        "last_watch_tick": last_tick,
        "needs": sorted(needs, key=lambda x: {"chore": 0, "gate": 1, "help": 2, "decision": 3, "system": 4}[x["type"]]),
        "running": running, "ready": ready, "recent": recent,
        "counts": {s: sum(1 for t in tasks.values() if t.status == s) for s in
                   ("ready", "dispatched", "in_progress", "submitted", "accepted", "merged", "closed", "blocked", "omri_queue")},
    }


def _age_h(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return round((datetime.now(timezone.utc) - t).total_seconds() / 3600, 1)
    except ValueError:
        return None


def _started(root: Path, tid: str) -> str | None:
    ts = None
    for e in _events(root, tid, 500):
        if e.get("kind") == "state" and e.get("to") == "in_progress":
            ts = e.get("ts")
    return ts


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


def apply_answer(root: Path, tid: str, answer: str, kind: str) -> dict:
    """Record the answer where the agents look for it, then let the Concierge apply consequences."""
    store = Store(root)
    t = store.get(tid)
    d = t.dir
    ts = now_iso()
    log_msgs = []
    if kind == "help":
        with open(d / "log.md", "a", encoding="utf-8") as f:
            f.write(f"\n- {ts} [steward] (Omri, via UI) {answer}\n")
        rc, out = _scr(root, "omri", "event", "system", "--name", "help_answered", "--task", tid, "--note", answer[:500], "--quiet")
        log_msgs.append(out)
    elif kind == "decision":
        # write the answer under the item in queues/omri.md (the Concierge's input) and close the record
        q = root / "queues" / "omri.md"
        txt = _read(q)
        pat = re.compile(rf"(### {re.escape(tid)} ·.*?\nAnswer:)\n", re.S)
        if pat.search(txt):
            txt = pat.sub(lambda m: m.group(1) + f" {answer}\n", txt, count=1)
            q.write_text(txt, encoding="utf-8")
        with open(d / "log.md", "a", encoding="utf-8") as f:
            f.write(f"\n- {ts} [omri] answer: {answer}\n")
        rc, out = _scr(root, "omri", "event", "system", "--name", "answer", "--task", tid, "--note", answer[:500], "--quiet")
        log_msgs.append(out)
        rc, out = _scr(root, "concierge", "task", "set", tid, "closed", "--note", f"answered via UI: {answer[:200]}", "--quiet")
        log_msgs.append(out)
    elif kind == "gate":
        target = {"accept": "accepted", "return": "triaged", "reject": "rejected"}.get(answer.split()[0].lower(), None)
        note = answer
        if target:
            if t.status == "escalated":
                _scr(root, "omri", "task", "set", tid, "omri_queue", "--quiet")
            rc, out = _scr(root, "omri", "task", "set", tid, target, "--note", f"Gate decision via UI: {note[:200]}", "--quiet")
            log_msgs.append(out)
            if target == "triaged":   # "return": back to the scriber queue with the note as feedback
                _scr(root, "omri", "task", "set", tid, "ready", "--note", f"returned by Omri: {note[:200]}", "--quiet")
        else:
            with open(d / "log.md", "a", encoding="utf-8") as f:
                f.write(f"\n- {ts} [omri] gate note: {answer}\n")
            rc, out = _scr(root, "omri", "event", "system", "--name", "gate_note", "--task", tid, "--note", answer[:500], "--quiet")
            log_msgs.append(out)
    else:
        return {"ok": False, "error": f"unknown kind {kind}"}
    # Concierge: apply consequences (unblocking, new tasks, edges) as a headless agent run, non-blocking
    threading.Thread(target=_concierge_apply, args=(root, tid, answer, kind), daemon=True).start()
    return {"ok": True, "log": log_msgs}


def _claude_bin() -> str | None:
    return shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd")


def _concierge_apply(root: Path, tid: str, answer: str, kind: str) -> None:
    exe = _claude_bin()
    if not exe or kind == "help":
        return
    brief = (f"You are the Concierge (read {root / 'agents' / 'concierge.md'} and agents/_common.md and follow them). "
             f"Omri answered {tid} via the UI with: {answer!r}. The decision brief is tasks/{Store(root).get(tid).dir.name}/spec.md. "
             f"Apply the answer with scr calls (--actor concierge): unblock or create the tasks the answer implies "
             f"(scr task new ... --origin {tid}), add edges, log a system event 'concierge_applied' with --task {tid}. "
             f"The record itself is already closed. Never edit Lean. Finish with `scr verify` and a git commit prefixed [concierge].")
    out = root / "reports" / f"concierge-{tid}-{int(time.time())}.txt"
    with open(out, "w", encoding="utf-8") as f:
        subprocess.run([exe, "-p", brief, "--permission-mode", "acceptEdits", "--output-format", "text"],
                       cwd=root, stdout=f, stderr=subprocess.STDOUT, timeout=1800)


def add_inbox(root: Path, text: str) -> dict:
    q = root / "queues" / "omri.md"
    txt = _read(q)
    if "## Inbox" not in txt:
        txt += "\n## Inbox\n\n"
    txt = txt.rstrip("\n") + f"\n- {text.strip()}\n"
    q.write_text(txt, encoding="utf-8")
    _scr(root, "omri", "event", "system", "--name", "inbox", "--note", text[:500], "--quiet")
    return {"ok": True}


# ---- chores: one-off items from ui/chores.yaml, shown until their done-check passes ------------
def _ps(root: Path, script: str, timeout: int = 3600) -> tuple[int, str]:
    """Run a PowerShell snippet from the scriptorium root (pwsh on non-Windows, for tests)."""
    exe = "powershell" if os.name == "nt" else (shutil.which("pwsh") or "pwsh")
    try:
        r = subprocess.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True, cwd=root, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, "powershell not available"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


_CHORE_CACHE: dict = {"ts": 0.0, "items": []}


def chores(root: Path, check: bool = True, max_age: float = 240.0) -> list[dict]:
    if check and time.time() - _CHORE_CACHE["ts"] < max_age:
        return _CHORE_CACHE["items"]
    items = _chores(root, check)
    if check:
        _CHORE_CACHE.update(ts=time.time(), items=items)
    return items


def invalidate_chores():
    _CHORE_CACHE["ts"] = 0.0


def _chores(root: Path, check: bool = True) -> list[dict]:
    p = UI_DIR / "chores.yaml"
    items = yaml.safe_load(_read(p)) or []
    ack = set(_read(root / "reports" / "ui.acks.txt").split())
    out = []
    for c in items:
        if f"chore:{c['id']}" in ack:
            continue
        done = False
        if check and c.get("done") and not c.get("manual"):
            rc, _ = _ps(root, c["done"], timeout=300)
            done = rc == 0
        if done:
            continue
        out.append({"id": c["id"], "type": "chore", "kind": "manual" if c.get("manual") else "run",
                    "title": c["title"], "text": (c.get("why") or "").strip(), "link": c.get("link"),
                    "options": [], "has_run": bool(c.get("run"))})
    return out


def run_chore(root: Path, cid: str) -> dict:
    items = yaml.safe_load(_read(UI_DIR / "chores.yaml")) or []
    c = next((x for x in items if x["id"] == cid), None)
    if not c or not c.get("run"):
        return {"ok": False, "error": "no such runnable chore"}
    rc, out = _ps(root, c["run"])
    invalidate_chores()
    _scr(root, "omri", "event", "system", "--name", "chore", "--note", f"{cid}: exit {rc}", "--quiet")
    ok = rc == 0
    if c.get("done"):
        drc, _ = _ps(root, c["done"], timeout=300)
        ok = drc == 0
    return {"ok": ok, "exit": rc, "output": out[-6000:]}


# ---- chat: a Claude Code session per thread, read-only over both repos ---------------------
CHAT_DIR = "reports/chat"


def chat(root: Path, thread: str, message: str, task: str | None) -> dict:
    exe = _claude_bin()
    cdir = root / CHAT_DIR
    cdir.mkdir(parents=True, exist_ok=True)
    tpath = cdir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', thread)}.jsonl"
    meta_path = tpath.with_suffix(".meta.json")
    meta = json.loads(_read(meta_path) or "{}")
    store = Store(root)
    wl_name, wl = _workload(root, store)
    repo = (root / wl.get("repo", "../" + wl_name)).resolve()
    with open(tpath, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "ts": now_iso(), "text": message}) + "\n")
    if not exe:
        reply = "claude (Claude Code CLI) is not on PATH, so the chat agent cannot run. Everything else in the UI works."
        with open(tpath, "a", encoding="utf-8") as f:
            f.write(json.dumps({"role": "assistant", "ts": now_iso(), "text": reply}) + "\n")
        return {"reply": reply, "session": None}
    sysprompt = (
        f"You are the operator's assistant for the Scriptorium research operation. Scriptorium root: {root}. "
        f"Workload repo (Lean 4, read-only for you): {repo}. Truth lives in tasks/*/task.yaml and events/events.jsonl; "
        f"reports live in tasks/*/report.md; the Lean is under {repo}/QuantumInsertion; notes under {repo}/notes; "
        f"mission records under {repo}/missions. Answer questions about tasks, history, and the mathematics (FGGS "
        f"quant-ph/9901059 ordered search, the N_k law, the greedy algorithm) by READING the relevant files and quoting "
        f"them with paths and line numbers. You may run `scr` read-only commands (score, graph analyze/context/impact, "
        f"replay, history) and `git log`/`git show`. Never modify anything; if the operator wants an action, tell them "
        f"which button or scr command does it. Be concrete and brief; say when something is not in the records."
    )
    if task:
        try:
            t = store.get(task)
            sysprompt += (f"\n\nCurrent task in focus: {task} — {t.rec.get('title','')}. Its record folder: {t.dir}. "
                          f"Read its spec.md, report.md, review.md and log.md before answering about it.")
        except Exception:
            pass
    cmd = [exe, "-p", "--output-format", "json", "--permission-mode", "default",
           "--allowedTools", "Read", "Grep", "Glob", "Bash(scr *)", "Bash(git log*)", "Bash(git show*)", "Bash(git diff*)",
           "--add-dir", str(repo)]
    if meta.get("session"):
        cmd += ["--resume", meta["session"]]
    else:
        cmd += ["--append-system-prompt", sysprompt]
    try:
        r = subprocess.run(cmd, input=message, capture_output=True, text=True, cwd=root, timeout=600)
        raw = r.stdout.strip()
        try:
            j = json.loads(raw)
            reply = j.get("result") or raw
            sid = j.get("session_id") or meta.get("session")
        except json.JSONDecodeError:
            reply = raw or r.stderr.strip() or "(no reply)"
            sid = meta.get("session")
    except subprocess.TimeoutExpired:
        reply, sid = "(the agent took longer than 10 minutes; try a narrower question)", meta.get("session")
    if sid:
        meta_path.write_text(json.dumps({"session": sid, "task": task}), encoding="utf-8")
    with open(tpath, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "assistant", "ts": now_iso(), "text": reply}) + "\n")
    return {"reply": reply, "session": sid}


def chat_thread(root: Path, thread: str) -> list[dict]:
    tpath = root / CHAT_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', thread)}.jsonl"
    return [json.loads(l) for l in _read(tpath).splitlines() if l.strip()]


def read_file(root: Path, rel: str) -> dict:
    store = Store(root)
    wl_name, wl = _workload(root, store)
    repo = (root / wl.get("repo", "../" + wl_name)).resolve()
    for base in (root, repo):
        p = (base / rel).resolve()
        if str(p).startswith(str(base)) and p.is_file():
            return {"path": str(p), "text": _read(p, 2_000_000)}
    return {"error": "not found or outside the repos"}


# ---- HTTP ---------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    root: Path = Path(".")

    def log_message(self, *a):  # quiet
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
            elif u.path.startswith("/api/chat/"):
                self._json(chat_thread(self.root, u.path.rsplit("/", 1)[1]))
            elif u.path == "/api/file":
                self._json(read_file(self.root, (q.get("path") or [""])[0]))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # surface, don't crash the server
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/api/answer":
                self._json(apply_answer(self.root, data["task"], data["answer"], data.get("kind", "decision")))
            elif u.path == "/api/inbox":
                self._json(add_inbox(self.root, data["text"]))
            elif u.path == "/api/chat":
                self._json(chat(self.root, data.get("thread") or "general", data["message"], data.get("task")))
            elif u.path == "/api/chore":
                self._json(run_chore(self.root, data["id"]))
            elif u.path == "/api/ack":
                with open(self.root / "reports" / "ui.acks.txt", "a", encoding="utf-8") as f:
                    f.write(data["h"] + "\n")
                invalidate_chores()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def serve(root: Path, port: int = 8765, open_browser: bool = True):
    Handler.root = root
    (root / "reports").mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Scriptorium UI at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve(find_root(), int(os.environ.get("SCR_UI_PORT", "8765")))
