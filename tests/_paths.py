"""Where the tests find the code repo's assets now that config/ and tasks/ live in the state repo."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "scr" / "defaults" / "config"     # the shipped defaults; the live config/ stays in the state folder
FIXTURES = REPO / "tests" / "fixtures"           # task records the tests replay (copied from the state repo)
ASSETS = REPO / "scr" / "assets"                 # templates, ui, agents, hooks: package data (scr/assets.py)


def asset(d: str) -> Path:
    """config from the shipped defaults; templates, ui, agents and hooks from the package's assets."""
    return CONFIG if d == "config" else ASSETS / d
