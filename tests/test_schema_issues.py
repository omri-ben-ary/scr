"""The state schema version and migration runner (scr/schema.py), and known issues (scr/issues.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scr import schema
from scr.events import EventLog
from scr.schema import Migration, current_version, migrate_schema, pending
from scr.store import Store
from test_journal_ops import git, scr, world  # noqa: F401  (the shared fixture)


def _events(root: Path, name: str) -> list[dict]:
    return [e for e in EventLog(root / "events" / "events.jsonl").read() if e.get("kind") == "system" and e.get("name") == name]


def _snap(root: Path) -> dict:
    return json.loads((root / "state" / "STATE.json").read_text(encoding="utf-8"))


def _mark(name: str):
    def apply(root: Path) -> list[str]:
        (root / "config" / f"{name}.txt").write_text("migrated\n", encoding="utf-8")
        return [f"wrote config/{name}.txt"]
    return apply


# ---- schema version ---------------------------------------------------------------------------------------
def test_a_fresh_state_records_the_schema_version(world):
    root, _, _ = world
    assert _snap(root)["schema_version"] == schema.SCHEMA_VERSION == 1
    assert current_version(root) == 1


def test_a_snapshot_without_the_key_is_version_1_and_keeps_it(world):
    root, _, _ = world
    p = root / "state" / "STATE.json"
    snap = _snap(root); snap.pop("schema_version"); p.write_text(json.dumps(snap), encoding="utf-8")
    assert current_version(root) == 1
    scr(root, "--quiet", "task", "new", "Another", "--id", "T-0002", "--origin", "omri", journal_on=False)
    assert _snap(root)["schema_version"] == 1


def test_nothing_is_pending_at_version_1():
    assert schema.MIGRATIONS == [] and schema.SCHEMA_VERSION == 1


def test_pending_follows_the_chain_and_refuses_a_gap(world):
    root, _, _ = world
    migs = [Migration(1, 2, "one", _mark("one")), Migration(2, 3, "two", _mark("two"))]
    assert [(m.frm, m.to) for m in pending(root, migs)] == [(1, 2), (2, 3)]
    assert [(m.frm, m.to) for m in pending(root, migs, target=2)] == [(1, 2)]
    with pytest.raises(RuntimeError, match="no migration from schema version 2"):
        pending(root, [Migration(1, 2, "one", _mark("one")), Migration(3, 4, "x", _mark("x"))], target=4)


def test_migrate_applies_each_step_with_one_event_and_one_commit(world):
    root, _, _ = world
    migs = [Migration(1, 2, "one", _mark("one")), Migration(2, 3, "two", _mark("two"))]
    commits: list[str] = []
    dry = migrate_schema(root, "omri", migs, dry_run=True)
    assert dry == ["would migrate 1 -> 2: one", "would migrate 2 -> 3: two"] and current_version(root) == 1
    out = migrate_schema(root, "omri", migs, commit=lambda r, a, msg: commits.append(msg))
    assert commits == ["migrate schema 1 -> 2", "migrate schema 2 -> 3"]
    assert current_version(root) == 3 and (root / "config" / "two.txt").exists()
    ev = _events(root, "schema_migrated")
    assert [(e["data"]["from"], e["data"]["to"]) for e in ev] == [(1, 2), (2, 3)] and all(e["actor"] == "omri" for e in ev)
    assert "migrated 1 -> 2: one" in out and "  wrote config/two.txt" in out
    assert migrate_schema(root, "omri", migs) == ["schema version 3: nothing to migrate"]
    assert not EventLog(root / "events" / "events.jsonl").verify_chain()


def test_an_incompatible_migration_waits_for_running_sessions(world):
    root, _, _ = world
    migs = [Migration(1, 2, "safe", _mark("safe")), Migration(2, 3, "breaking", _mark("breaking"), compatible_with_running_sessions=False)]
    live = [{"task": "T-0001", "kind": "scriber", "pid": 4242}]
    commit = lambda r, a, m: None  # noqa: E731
    assert migrate_schema(root, "omri", migs, dry_run=True)[1].endswith("(needs no running sessions)")
    with pytest.raises(RuntimeError, match="running sessions .*T-0001"):
        migrate_schema(root, "omri", migs, sessions=lambda r: live, commit=commit)
    assert current_version(root) == 2 and not (root / "config" / "breaking.txt").exists()   # the safe step stood
    migrate_schema(root, "omri", migs, sessions=lambda r: [], commit=commit)
    assert current_version(root) == 3


def test_running_sessions_reads_live_launch_events(world):
    import os
    root, _, _ = world
    log = EventLog(root / "events" / "events.jsonl")
    log.append("system", name="launch", task="T-0001", actor="s", data={"kind": "scriber", "session_id": "a", "pid": os.getpid()})
    log.append("system", name="launch", task="T-0001", actor="s", data={"kind": "review", "session_id": "b", "pid": 2 ** 22 + 12345})
    assert [s["session_id"] for s in schema.running_sessions(root)] == ["a"]


def test_scr_migrate_dry_run_changes_nothing(world):
    root, _, _ = world
    r = scr(root, "migrate", "--dry-run", journal_on=False)
    assert "schema version 1: nothing to migrate" in r.stdout


# ---- known issues -----------------------------------------------------------------------------------------
def test_issue_add_list_close_round_trip(world):
    root, _, _ = world
    assert "added I-0001" in scr(root, "issue", "add", "landing job queued with nothing running", journal_on=False).stdout
    scr(root, "issue", "add", "UI", "card", "shows", "twice", journal_on=False)
    out = scr(root, "issue", "list", journal_on=False).stdout
    assert "I-0001 [open] landing job queued with nothing running" in out and "I-0002 [open] UI card shows twice" in out
    view = (root / "queues" / "known-issues.md").read_text(encoding="utf-8")
    assert "## Open (2)" in view and "**I-0001**" in view
    scr(root, "issue", "close", "I-0001", "--note", "fixed in v0.2.1", journal_on=False)
    assert "I-0001" not in scr(root, "issue", "list", journal_on=False).stdout
    assert "I-0001 [closed]" in scr(root, "issue", "list", "--all", journal_on=False).stdout
    view = (root / "queues" / "known-issues.md").read_text(encoding="utf-8")
    assert "## Open (1)" in view and "I-0001 landing job queued with nothing running — fixed in v0.2.1" in view
    assert [e["data"]["id"] for e in _events(root, "issue_add")] == ["I-0001", "I-0002"]
    assert _events(root, "issue_close")[0]["data"] == {"id": "I-0001", "note": "fixed in v0.2.1"}
    assert not EventLog(root / "events" / "events.jsonl").verify_chain()


def test_issue_close_refuses_unknown_closed_or_unexplained(world):
    root, _, _ = world
    scr(root, "issue", "add", "a bug", journal_on=False)
    assert scr(root, "issue", "close", "I-0009", "--note", "x", journal_on=False, check=False).returncode == 2
    assert scr(root, "issue", "close", "I-0001", journal_on=False, check=False).returncode == 2       # no --note
    scr(root, "issue", "close", "I-0001", "--note", "done", journal_on=False)
    assert scr(root, "issue", "close", "I-0001", "--note", "again", journal_on=False, check=False).returncode == 2
    assert scr(root, "issue", "add", journal_on=False, check=False).returncode == 2                   # no text


def test_urgent_issue_files_a_ready_tooling_task_at_the_top(world):
    root, _, _ = world
    out = scr(root, "issue", "add", "--urgent", "watcher dead on every tick", journal_on=False).stdout
    assert "filed T-0002 at the top of the queue" in out
    t = Store(root).get("T-0002")
    wl = yaml.safe_load((root / "config" / "workloads.yaml").read_text(encoding="utf-8"))
    assert t.status == "ready" and t.rec["priority"]["omri_weight"] == 2.0
    assert t.rec["workload"] == ("scr" if "scr" in wl else "scriptorium")
    assert _events(root, "issue_add")[0]["data"]["task"] == "T-0002"
    assert "→ T-0002" in (root / "queues" / "known-issues.md").read_text(encoding="utf-8")


def test_issue_commands_journal_themselves(world):
    root, _, _ = world
    before = git(root, "rev-parse", "HEAD")
    scr(root, "issue", "add", "journaled bug")
    assert git(root, "rev-parse", "HEAD") != before
    assert git(root, "status", "--porcelain", "queues/known-issues.md", "events") == ""
