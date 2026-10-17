"""Reading and writing task records. tasks/<id>-<slug>/task.yaml is the record."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .canon import H, canon, sha256_bytes

STATES = [
    "proposed", "triaged", "blocked", "ready", "dispatched", "in_progress",
    "checkpoint", "waiting", "submitted", "returned", "escalated", "omri_queue",
    "accepted", "merged", "rejected", "closed",
]   # waiting = a job runs for the task outside its session (scr/jobs.py, 2026-10-10); holds no concurrency slot
OPEN_STATES = {s for s in STATES if s not in {"merged", "rejected", "closed"}}
DONE_STATES = {"merged", "closed"}
KINDS = ["prove", "audit", "refactor", "survey", "decide", "approve", "system", "milestone"]
QUEUES = ["scribers", "omri"]
DOC_FILES = ["spec.md", "log.md", "report.md", "review.md"]

# Human-friendly key order for task.yaml; unknown keys go last, alphabetically.
KEY_ORDER = [
    "id", "title", "queue", "kind", "status", "workload", "depth", "scope",
    "done_when", "budget", "priority", "blocked_by", "origin", "related",
    "supersedes", "unlocks", "attempts", "tokens_used", "created", "updated",
]


def file_hash(p: Path) -> str:
    """sha256 of a text file's bytes with CRLF normalised to LF (nothing else), so a checkout's
    line endings (core.autocrlf) never change docs_hash / config_hash."""
    return sha256_bytes(p.read_bytes().replace(b"\r\n", b"\n"))


class RepoError(Exception):
    pass


def find_root(start: Path | None = None) -> Path:
    p = (start or Path.cwd()).resolve()
    env = os.environ.get("SCRIPTORIUM_ROOT")
    if env:
        return Path(env).resolve()
    for cand in [p, *p.parents]:
        if (cand / "tasks").is_dir() and (cand / "config").is_dir():
            return cand
    raise RepoError("not inside a scriptorium repo (no tasks/ + config/ found); set SCRIPTORIUM_ROOT")


def _ordered(d: dict) -> dict:
    keys = [k for k in KEY_ORDER if k in d] + sorted(k for k in d if k not in KEY_ORDER)
    return {k: d[k] for k in keys}


def dump_yaml(d: dict) -> str:
    return yaml.safe_dump(_ordered(d), sort_keys=False, allow_unicode=True, width=100)


@dataclass
class Task:
    dir: Path
    rec: dict[str, Any]

    @property
    def id(self) -> str:
        return self.rec["id"]

    @property
    def status(self) -> str:
        return self.rec.get("status", "proposed")

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATES

    @property
    def is_done(self) -> bool:
        return self.status in DONE_STATES

    def task_hash(self) -> str:
        rec = {k: v for k, v in self.rec.items() if k != "hash"}
        return H("task", canon(rec))

    def docs_hash(self) -> str:
        parts = []
        for f in DOC_FILES:
            p = self.dir / f
            parts.append(file_hash(p) if p.exists() else sha256_bytes(b""))
        return H("docs", *parts)

    def leaf(self) -> str:
        return H("leaf", self.id, self.task_hash(), self.docs_hash())

    def save(self) -> None:
        """Atomic: the record is written to task.yaml.tmp and renamed over task.yaml, so a crash or a concurrent
        writer can never leave an empty / half-written record (T-0355's task.yaml was 0 bytes on 2026-10-15 20:55
        and every watcher tick crashed on it for 16 h)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "task.yaml.tmp"
        tmp.write_text(dump_yaml(self.rec), encoding="utf-8")
        tmp.replace(self.dir / "task.yaml")


ID_RE = re.compile(r"^T-\d{4}$")


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40].rstrip("-") or "task"


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.tasks_dir = root / "tasks"
        self.broken: list[str] = []          # records load_all skipped (empty / unparsable task.yaml)

    # ---- tasks -------------------------------------------------------------
    def load_all(self) -> dict[str, Task]:
        out: dict[str, Task] = {}
        self.broken = []
        if not self.tasks_dir.exists():
            return out
        for d in sorted(self.tasks_dir.iterdir()):
            f = d / "task.yaml"
            if not f.is_file():
                continue
            try:
                rec = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                rec = {"_error": str(e)}
            if "id" not in rec:
                # 2026-10-16: one broken record must not take the whole system down. It is skipped here; the
                # watcher raises a Needs-you line naming it (watch.py broken_records) and git holds the last good copy:
                # `git show HEAD:<path>` restores it.
                import sys
                print(f"warning: {f} has no id (empty or unparsable record) — skipped; restore it from git", file=sys.stderr)
                self.broken.append(str(f))
                continue
            if rec["id"] in out:
                raise RepoError(f"duplicate task id {rec['id']} in {d} and {out[rec['id']].dir}")
            out[rec["id"]] = Task(d, rec)
        return out

    def get(self, tid: str) -> Task:
        tasks = self.load_all()
        if tid not in tasks:
            raise RepoError(f"no such task {tid}")
        return tasks[tid]

    def next_id(self) -> str:
        ids = [int(t[2:]) for t in self.load_all() if ID_RE.match(t)]
        return f"T-{(max(ids) + 1 if ids else 1):04d}"

    def new_task(self, rec: dict[str, Any]) -> Task:
        tid = rec["id"]
        d = self.tasks_dir / f"{tid}-{slugify(rec.get('title', ''))}"
        t = Task(d, rec)
        t.save()
        tpl = self.root / "templates"
        for f in DOC_FILES:
            p = d / f
            if not p.exists():
                src = tpl / f
                p.write_text(src.read_text(encoding="utf-8") if src.exists() else "", encoding="utf-8")
        return t

    # ---- config ------------------------------------------------------------
    def config(self, name: str) -> dict:
        p = self.root / "config" / f"{name}.yaml"
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.exists() else {}

    def config_hash(self) -> str:
        cdir = self.root / "config"
        parts = []
        if cdir.exists():
            for f in sorted(cdir.iterdir()):
                if f.is_file():
                    parts.append(f.name + ":" + file_hash(f))
        return H("config", *parts)
