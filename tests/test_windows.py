"""Windows-only code paths that had no test of their own (the `tests (windows)` CI job runs every
`windows`-marked test; on Linux the ones here that need Windows skip, the rest run everywhere)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import scr.run as run_mod
from scr.ops import _is_link, _link_venv

pytestmark = pytest.mark.windows


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


def test_claude_argv_wraps_a_cmd_shim_in_cmd_c(monkeypatch):
    """Popen cannot start a .cmd/.bat shim directly: claude_argv runs it through `cmd /c`; an .exe runs as is."""
    monkeypatch.setattr(run_mod, "claude_bin", lambda: r"C:\tools\claude.cmd")
    assert run_mod.claude_argv("-p", "x") == ["cmd", "/c", r"C:\tools\claude.cmd", "-p", "x"]
    monkeypatch.setattr(run_mod, "claude_bin", lambda: r"C:\tools\claude.exe")
    assert run_mod.claude_argv("-p") == [r"C:\tools\claude.exe", "-p"]


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are the Windows path")
def test_link_venv_makes_a_junction_git_ignores(tmp_path):
    """`venv: link` on Windows: <worktree>/.venv is a directory junction to the checkout's .venv (no copy), it
    resolves to the same files, `_is_link` sees it as a link (so retire unlinks it, never recurses), and a
    `.venv/` ignore rule keeps it out of git status (git treats a junction as a directory)."""
    repo, wt = tmp_path / "checkout", tmp_path / "wt"
    (repo / ".venv" / "Scripts").mkdir(parents=True)
    (repo / ".venv" / "Scripts" / "marker.txt").write_text("checkout venv\n", encoding="utf-8")
    wt.mkdir()
    _git(wt, "init", "-q")
    (wt / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "seed")
    out: list[str] = []
    _link_venv(repo, wt, out)
    assert out == ["  junction .venv -> checkout .venv"], out
    assert _is_link(wt / ".venv")
    assert (wt / ".venv" / "Scripts" / "marker.txt").read_text(encoding="utf-8") == "checkout venv\n"
    assert _git(wt, "status", "--porcelain") == ""
