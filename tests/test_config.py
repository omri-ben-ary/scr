"""T-0125: every top-level key in config/limits.yaml has a reader. A key is read when its name occurs in
some scr/*.py (a substring match; false positives are cheap, a false negative is the bug), or when it is in
READERS, the allowlist of keys an agent reads through its brief or agent file, with the reader named. A key
in neither is a red test, not a review finding weeks later (dispatch_timeout_minutes sat unread from
2026-09-16 to 2026-09-23). The test does not require every read key to be present: claude_permission_mode
is read by scr/run.py and is not in the file.

T-0133 generalises the checker to the other two config files: every top-level key of config/weights.yaml and
every per-workload key of every workload in config/workloads.yaml (reported as <workload>.<key>) has a reader
in scr/*.py or sits in that file's allowlist. On 2026-09-23 both new allowlists are empty: the nine w_* keys
are read by scr/score.py, and the two unread workload keys (quantum-insertion.ci_gates, audit_surface) were
deleted with a dated note rather than allowlisted, because no brief in agents/ or scr/ names them."""
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

# T-0133: the same allowlists for the other two files. weights.yaml keys are top-level; workloads.yaml entries
# are qualified as "<workload>.<key>". Same rule: only a key an agent reads through its brief belongs here,
# named with the agent and the file; the yaml line must carry a `reader` comment. Both empty on 2026-09-23.
WEIGHTS_READERS: dict[str, str] = {}
WORKLOADS_READERS: dict[str, str] = {}

# config file -> its allowlist. The checker below uses these when no allowlist is passed.
CONFIG_READERS = {"limits.yaml": READERS, "weights.yaml": WEIGHTS_READERS, "workloads.yaml": WORKLOADS_READERS}


def config_keys(root: Path, file: str) -> list[str]:
    """The keys of config/<file> the reader check covers. Top-level keys for limits.yaml and weights.yaml;
    for workloads.yaml every key under every workload, qualified as "<workload>.<key>" (the `notes` list is
    one key like any other)."""
    data = yaml.safe_load((root / "config" / file).read_text(encoding="utf-8")) or {}
    if file == "workloads.yaml":
        return [f"{wl}.{k}" for wl, block in data.items() for k in (block or {})]
    return list(data.keys())


def limits_keys(root: Path) -> list[str]:
    return config_keys(root, "limits.yaml")


def _scr_text(root: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted((root / "scr").glob("*.py")))


def unread_config_keys(root: Path, file: str, readers: dict | None = None) -> list[str]:
    """The config/<file> keys under `root` that no scr/*.py under `root` mentions and the allowlist does not
    name. A workloads.yaml key is looked up in the code by its bare name and in the allowlist by its
    qualified name, so the failure message says which block."""
    readers = CONFIG_READERS.get(file, {}) if readers is None else readers
    text = _scr_text(root)
    return [q for q in config_keys(root, file) if q.rsplit(".", 1)[-1] not in text and q not in readers]


def unread_limits_keys(root: Path, readers: dict = READERS) -> list[str]:
    """The limits.yaml keys under `root` that no scr/*.py under `root` mentions and the allowlist does not name."""
    return unread_config_keys(root, "limits.yaml", readers)


def _assert_allowlist_is_honest(file: str, readers: dict[str, str]) -> None:
    """Every entry is a live key of config/<file>, names an agent, and its yaml line carries a `reader` comment."""
    keys = config_keys(REPO, file)
    for key, who in readers.items():
        assert key in keys, f"the {file} allowlist names {key}, which is no longer in {file}: drop the entry"
        assert who and "agent" in who, f"the {file} allowlist entry for {key} must name the agent that reads it"
        line = _yaml_line(REPO / "config" / file, key)
        assert "#" in line and "reader" in line, f"{file} line for {key} must carry a comment naming its reader"


def _yaml_line(path: Path, key: str) -> str:
    """The source line of `key` ("<block>.<key>" for a two-level file, else top-level), so a `reader` comment
    can be checked on it. Tracks the current top-level block by indentation, because both workloads have a
    `repo:` line."""
    block, name = key.rsplit(".", 1) if "." in key else (None, key)
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw and not raw[0].isspace() and not raw.startswith("#"):
            current = raw.split(":", 1)[0].strip()
            if block is None and current == name:
                return raw
        elif block is not None and current == block and raw.strip().startswith(name + ":"):
            return raw
    raise AssertionError(f"no line for {key} in {path.name}")


def test_every_limits_key_has_a_reader():
    unread = unread_limits_keys(REPO)
    assert not unread, (f"limits.yaml keys read by nothing in scr/*.py: {unread}. Wire each into scr/, delete it "
                        f"with a note in the yaml, or (only if an agent reads it through its brief) add it to "
                        f"READERS in tests/test_config.py naming the reader.")


def test_allowlist_entries_are_live_keys_with_a_named_reader():
    """READERS (limits.yaml) and, since T-0133, WEIGHTS_READERS and WORKLOADS_READERS: each entry is a live key,
    names an agent, and its yaml line carries a `reader` comment."""
    for file, readers in CONFIG_READERS.items():
        _assert_allowlist_is_honest(file, readers)
    # the helper itself must catch the three kinds of dishonest entry
    with pytest.raises(AssertionError, match="no longer in limits.yaml"):
        _assert_allowlist_is_honest("limits.yaml", {"nobody_reads_this_key": "the Intake agent"})
    with pytest.raises(AssertionError, match="must name the agent"):
        _assert_allowlist_is_honest("limits.yaml", {"max_depth": "a comment in the yaml"})
    with pytest.raises(AssertionError, match="must carry a comment"):
        _assert_allowlist_is_honest("workloads.yaml", {"quantum-insertion.repo": "the Steward agent, via agents/steward.md"})


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


# ---- T-0133: the other two config files --------------------------------------------------------------
def test_every_weights_key_has_a_reader():
    unread = unread_config_keys(REPO, "weights.yaml")
    assert not unread, (f"weights.yaml keys read by nothing in scr/*.py: {unread}. Wire each into scr/score.py, "
                        f"delete it with a note in the yaml, or (only if an agent reads it through its brief) add "
                        f"it to WEIGHTS_READERS in tests/test_config.py naming the reader.")


def test_every_workload_key_has_a_reader():
    unread = unread_config_keys(REPO, "workloads.yaml")
    assert not unread, (f"workloads.yaml keys read by nothing in scr/*.py: {unread}. Wire each into scr/, delete "
                        f"it with a dated note in its workload block, or (only if an agent reads it through its "
                        f"brief) add '<workload>.<key>' to WORKLOADS_READERS in tests/test_config.py naming the reader.")


def test_unread_workload_key_is_red(tmp_path: Path):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO / "scr", root / "scr", ignore=shutil.ignore_patterns("__pycache__"))
    data = yaml.safe_load((REPO / "config" / "workloads.yaml").read_text(encoding="utf-8"))
    first = next(iter(data))
    data[first]["nobody_reads_this_key"] = 7
    (root / "config" / "workloads.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert unread_config_keys(root, "workloads.yaml") == [f"{first}.nobody_reads_this_key"]
    assert unread_config_keys(REPO, "workloads.yaml") == []       # the same check is green on the real tree
    # the allowlist silences exactly the named entry, and only under its own workload
    assert unread_config_keys(root, "workloads.yaml", {f"{first}.nobody_reads_this_key": "the Intake agent"}) == []
    assert unread_config_keys(root, "workloads.yaml", {"other.nobody_reads_this_key": "the Intake agent"}) == [f"{first}.nobody_reads_this_key"]


def test_unread_weights_key_is_red(tmp_path: Path):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO / "scr", root / "scr", ignore=shutil.ignore_patterns("__pycache__"))
    src = (REPO / "config" / "weights.yaml").read_text(encoding="utf-8")
    (root / "config" / "weights.yaml").write_text(src + "nobody_reads_this_key: 7\n", encoding="utf-8")
    assert unread_config_keys(root, "weights.yaml") == ["nobody_reads_this_key"]
    assert unread_config_keys(REPO, "weights.yaml") == []


def test_config_keys_shapes():
    """Top-level for limits/weights; qualified per-workload keys for workloads, `notes` counting as one key."""
    assert config_keys(REPO, "limits.yaml") == limits_keys(REPO)
    assert all(k.startswith("w_") for k in config_keys(REPO, "weights.yaml"))
    wl = config_keys(REPO, "workloads.yaml")
    assert "quantum-insertion.repo" in wl and "scriptorium.repo" in wl and "scriptorium.notes" in wl
    assert wl.count("scriptorium.notes") == 1 and not any(k.endswith(".notes.0") for k in wl)


def test_ci_gates_and_audit_surface_are_wired_or_gone():
    """The two unread keys of 2026-09-23: each is now read in scr/ or agents/, or gone from workloads.yaml
    (deleted with a dated note). Neither may sit in WORKLOADS_READERS unless a brief names it."""
    keys = config_keys(REPO, "workloads.yaml")
    briefs = _scr_text(REPO) + "".join(p.read_text(encoding="utf-8") for p in (REPO / "agents").glob("*.md"))
    src = (REPO / "config" / "workloads.yaml").read_text(encoding="utf-8")
    for key in ("ci_gates", "audit_surface"):
        if f"quantum-insertion.{key}" in keys:
            assert key in briefs, f"{key} is still in workloads.yaml and nothing in scr/ or agents/ reads it"
            assert f"quantum-insertion.{key}" not in WORKLOADS_READERS or key in briefs
        else:
            note = next((l for l in src.splitlines() if l.lstrip().startswith("#") and key in l), "")
            assert "2026-09-23" in note, f"{key} is gone from workloads.yaml but its dated deletion note is not"


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


# ---- T-0186: land_test_timeout_seconds -------------------------------------------------------------------
def test_land_test_timeout_reads_limits_and_defaults_to_3600(tmp_path: Path):
    """ops.land_test_timeout: the limits.yaml value when it is a positive number, 3600 when the key (or the
    file) is absent; the real limits.yaml carries the key with a comment naming scr/ops.py."""
    from scr.ops import LAND_TEST_TIMEOUT_S, land_test_timeout
    from scr.store import Store
    assert LAND_TEST_TIMEOUT_S == 3600
    assert land_test_timeout(Store(tmp_path)) == 3600                     # no config/limits.yaml at all
    (tmp_path / "config").mkdir()
    lim = tmp_path / "config" / "limits.yaml"
    lim.write_text("concurrency: 2\n", encoding="utf-8")
    assert land_test_timeout(Store(tmp_path)) == 3600                     # the key absent
    lim.write_text("land_test_timeout_seconds: 5\n", encoding="utf-8")
    assert land_test_timeout(Store(tmp_path)) == 5
    for bad in ("0", "-3", "soon", "null"):
        lim.write_text(f"land_test_timeout_seconds: {bad}\n", encoding="utf-8")
        assert land_test_timeout(Store(tmp_path)) == 3600, bad
    line = [l for l in (REPO / "config" / "limits.yaml").read_text(encoding="utf-8").splitlines() if "land_test_timeout_seconds" in l]
    assert len(line) == 1 and line[0].startswith("land_test_timeout_seconds:") and "scr/ops.py" in line[0], line
