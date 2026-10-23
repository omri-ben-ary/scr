"""The code's own files: the folders that ship inside the package (templates, ui, agents, hooks), as opposed
to the state root's data (tasks, events, config, the live ui/chores.yaml). A non-editable install has no
checkout to find them in, so they are package data under scr/assets/ and every reader asks asset_path()."""
from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
NAMES = ("templates", "ui", "agents", "hooks")


def asset_path(name: str, *parts: str) -> Path:
    """scr/assets/<name>/<parts...> of the installed package. `name` is one of NAMES."""
    if name not in NAMES:
        raise KeyError(f"unknown asset folder {name!r} (one of {', '.join(NAMES)})")
    return ASSETS.joinpath(name, *parts)
