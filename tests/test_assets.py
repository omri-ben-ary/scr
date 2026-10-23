"""The package's own files (scr/assets.py): templates, ui, agents and hooks ship in the wheel and every reader
finds them there, so a non-editable install works with a state root that holds only data. The wheel itself is
built and installed by tools/check_wheel.py (a CI step)."""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

from scr.assets import ASSETS, NAMES, asset_path
from test_journal_ops import REPO, scr, world  # noqa: F401  (the shared fixture)


def test_every_asset_folder_ships_in_the_package():
    assert ASSETS == REPO / "scr" / "assets"
    for name in NAMES:
        assert asset_path(name).is_dir(), name
    assert asset_path("agents", "steward.md").is_file() and asset_path("templates", "review.md").is_file()
    assert asset_path("ui", "index.html").is_file() and asset_path("ui", "chores.yaml").is_file()
    with pytest.raises(KeyError):
        asset_path("config")                                          # operational data: the state root's, never the package's


def test_package_data_covers_every_asset_and_the_default_config():
    """pyproject's package-data globs must match every file a reader can ask for, or the wheel ships without it."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")       # no tomllib: Python 3.10 is supported
    block = text.split("[tool.setuptools.package-data]", 1)[1]
    pats = re.findall(r'"([^"]+)"', re.search(r"(?m)^scr = \[(.*)\]", block).group(1))
    pkg = REPO / "scr"
    files = [p.relative_to(pkg).as_posix() for d in (ASSETS, pkg / "defaults")
             for p in d.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert files
    missing = [f for f in files if not any(fnmatch.fnmatch(f, pat.replace("**/", "*")) for pat in pats)]
    assert missing == [], missing


def test_init_creates_no_code_folders_and_templates_come_from_the_package(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    (root / "config").mkdir()
    scr(root, "init", "--quiet", journal_on=False)
    assert not (root / "templates").exists() and not (root / "agents").exists()
    scr(root, "--quiet", "task", "new", "A", "--id", "T-0001", "--origin", "omri", journal_on=False)
    spec = next((root / "tasks").glob("T-0001-*")) / "spec.md"
    assert spec.read_text(encoding="utf-8") == asset_path("templates", "spec.md").read_text(encoding="utf-8")


def test_review_brief_names_the_packaged_steward_instructions(world, monkeypatch):
    import scr.watch as watch_mod
    from scr.store import Store
    root, _, _ = world
    monkeypatch.setattr(watch_mod, "start_claude", lambda *a, **k: None)
    for st in ("dispatched", "in_progress", "submitted"):
        scr(root, "--quiet", "--actor", "s", "task", "set", "T-0001", st)
    watch_mod.Watcher(root, quiet=True).start_review(Store(root).get("T-0001"))
    brief = (Store(root).get("T-0001").dir / "review-brief.txt").read_text(encoding="utf-8")
    assert str(asset_path("agents", "steward.md")) in brief and str(root / "agents") not in brief


def test_scr_assets_prints_the_folders(tmp_path):
    out = scr(tmp_path, "assets", "hooks").stdout.strip()
    assert Path(out) == asset_path("hooks") and (Path(out) / "loopy_phase_c.ps1").is_file()
