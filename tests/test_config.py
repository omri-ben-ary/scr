"""T-0125: every top-level key in config/limits.yaml has a reader. A key is read when its name occurs in
some scr/*.py (a substring match; false positives are cheap, a false negative is the bug), or when it is in
READERS, the allowlist of keys an agent reads through its brief or agent file, with the reader named. A key
in neither is a red test, not a review finding weeks later (dispatch_timeout_minutes sat unread from
2026-09-16 to 2026-09-23). The test does not require every read key to be present: claude_permission_mode
is read by scr/run.py and is not in the file."""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from scr.cli import statement_window_open
from test_journal_ops import REPO, world, scr  # noqa: F401  (the shared fixture)

# key -> who reads it and where. Only keys an agent reads through its brief belong here; a yaml comment is
# not a reader. Each entry must also be a key in limits.yaml, with a comment on its line naming the reader.
READERS = {
    "wip_ready_scribers": "the Intake agent, via scr/launch.py INTAKE_BRIEF and agents/intake.md",
    "max_depth": "the Intake agent, via scr/launch.py INTAKE_BRIEF and agents/intake.md",
    "max_successors_per_task": "the Intake agent, via agents/intake.md ('Respect max_successors_per_task and max_depth')",
    "omri_stale_hours": "the Concierge agent, via agents/concierge.md; T-0126 wires it into scr/watch.py, then drop this entry",
}


def limits_keys(root: Path) -> list[str]:
    return list((yaml.safe_load((root / "config" / "limits.yaml").read_text(encoding="utf-8")) or {}).keys())


def unread_limits_keys(root: Path, readers: dict = READERS) -> list[str]:
    """The limits.yaml keys under `root` that no scr/*.py under `root` mentions and the allowlist does not name."""
    text = "".join(p.read_text(encoding="utf-8") for p in sorted((root / "scr").glob("*.py")))
    return [k for k in limits_keys(root) if k not in text and k not in readers]


def test_every_limits_key_has_a_reader():
    unread = unread_limits_keys(REPO)
    assert not unread, (f"limits.yaml keys read by nothing in scr/*.py: {unread}. Wire each into scr/, delete it "
                        f"with a note in the yaml, or (only if an agent reads it through its brief) add it to "
                        f"READERS in tests/test_config.py naming the reader.")


def test_allowlist_entries_are_live_keys_with_a_named_reader():
    keys = limits_keys(REPO)
    lines = (REPO / "config" / "limits.yaml").read_text(encoding="utf-8").splitlines()
    for key, who in READERS.items():
        assert key in keys, f"READERS names {key}, which is no longer in limits.yaml: drop the entry"
        assert who and "agent" in who, f"READERS[{key}] must name the agent that reads it"
        line = next(l for l in lines if l.startswith(key + ":"))
        assert "#" in line and "reader" in line, f"limits.yaml line for {key} must carry a comment naming its reader"


def test_unread_limits_key_is_red(tmp_path: Path):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO / "scr", root / "scr", ignore=shutil.ignore_patterns("__pycache__"))
    src = (REPO / "config" / "limits.yaml").read_text(encoding="utf-8")
    (root / "config" / "limits.yaml").write_text(src + "nobody_reads_this_key: 7\n", encoding="utf-8")
    assert unread_limits_keys(root) == ["nobody_reads_this_key"]
    assert unread_limits_keys(REPO) == []       # the same check is green on the real tree


def test_the_five_formerly_unread_keys_are_wired_deleted_or_allowlisted():
    """The spec's list on 2026-09-23. Each is now read in scr/, gone from the file, or allowlisted."""
    keys = limits_keys(REPO)
    text = "".join(p.read_text(encoding="utf-8") for p in (REPO / "scr").glob("*.py"))
    assert "wip_open_omri" not in keys                                    # deleted with a note
    assert "default_max_attempts" in keys and "default_max_attempts" in (REPO / "scr" / "cli.py").read_text(encoding="utf-8")
    assert "statement_window" in keys and "statement_window" in (REPO / "scr" / "cli.py").read_text(encoding="utf-8")
    assert "max_successors_per_task" in READERS and "omri_stale_hours" in READERS
    assert "max_successors_per_task" not in text and "omri_stale_hours" not in text   # allowlisted because nothing in scr/ reads them yet


# ---- the two keys wired here ---------------------------------------------------------------------------
def test_task_new_uses_default_max_attempts(world):
    root, _, _ = world
    limits = yaml.safe_load((root / "config" / "limits.yaml").read_text(encoding="utf-8"))
    limits["default_max_attempts"] = 5
    (root / "config" / "limits.yaml").write_text(yaml.safe_dump(limits), encoding="utf-8")
    scr(root, "--quiet", "task", "new", "Default budget", "--id", "T-0002", "--origin", "omri", journal_on=False)
    scr(root, "--quiet", "task", "new", "Explicit budget", "--id", "T-0003", "--origin", "omri", "--max-attempts", "1", journal_on=False)
    rec = lambda tid: yaml.safe_load(next((root / "tasks").glob(tid + "-*")).joinpath("task.yaml").read_text(encoding="utf-8"))
    assert rec("T-0002")["budget"]["max_attempts"] == 5
    assert rec("T-0003")["budget"]["max_attempts"] == 1
    del limits["default_max_attempts"]
    (root / "config" / "limits.yaml").write_text(yaml.safe_dump(limits), encoding="utf-8")
    scr(root, "--quiet", "task", "new", "No key", "--id", "T-0004", "--origin", "omri", journal_on=False)
    assert rec("T-0004")["budget"]["max_attempts"] == 3                   # the key gone -> the old argparse default


@pytest.mark.parametrize("window, hhmm, open_", [
    ("00:00-23:59", "12:00", True), ("09:00-17:00", "08:59", False), ("09:00-17:00", "09:00", True),
    ("09:00-17:00", "17:00", True), ("09:00-17:00", "17:01", False),
    ("22:00-06:00", "23:30", True), ("22:00-06:00", "03:00", True), ("22:00-06:00", "12:00", False),
    (None, "12:00", True), ("", "12:00", True),
])
def test_statement_window_open(window, hhmm, open_):
    h, m = map(int, hhmm.split(":"))
    assert statement_window_open(window, datetime(2026, 9, 23, h, m)) is open_


def test_dispatch_drops_statement_tasks_outside_the_window(world):
    root, _, _ = world
    tdir = next((root / "tasks").glob("T-0001-*"))
    rec = yaml.safe_load((tdir / "task.yaml").read_text(encoding="utf-8"))
    rec["scope"]["may_touch_statements"] = True
    (tdir / "task.yaml").write_text(yaml.safe_dump(rec, sort_keys=False), encoding="utf-8")
    limits = yaml.safe_load((root / "config" / "limits.yaml").read_text(encoding="utf-8"))
    now = datetime.now().strftime("%H:%M")
    limits["statement_window"] = "00:01-00:01" if now == "00:00" else "00:00-00:00"   # a one-minute window that excludes now
    (root / "config" / "limits.yaml").write_text(yaml.safe_dump(limits), encoding="utf-8")
    r = scr(root, "dispatch", "--dry-run", "--allow-statements", journal_on=False)
    assert "would dispatch:" in r.stdout and "T-0001" not in r.stdout, r.stdout
    limits["statement_window"] = "00:00-23:59"
    (root / "config" / "limits.yaml").write_text(yaml.safe_dump(limits), encoding="utf-8")
    r = scr(root, "dispatch", "--dry-run", "--allow-statements", journal_on=False)
    assert "would dispatch: T-0001" in r.stdout, r.stdout
    r = scr(root, "dispatch", "--dry-run", journal_on=False)               # the flag rule is unchanged inside the window
    assert "T-0001" not in r.stdout, r.stdout
