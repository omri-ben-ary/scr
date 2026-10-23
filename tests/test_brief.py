from __future__ import annotations

from pathlib import Path

from test_journal_ops import REPO, scr, world  # noqa: F401  (the shared fixture)
from _paths import asset  # noqa: E402


def test_task_brief_carries_the_utf8_script_rule(world):
    """T-0579: a scriber's brief tells it to write scripts that survive `python -I` on a cp1255 console."""
    root, _, _ = world
    out = scr(root, "task", "brief", "T-0001").stdout
    assert 'sys.stdout.reconfigure(encoding="utf-8")' in out and 'sys.stderr.reconfigure(encoding="utf-8")' in out
    assert 'encoding="utf-8"' in out and "python -I" in out


def test_steward_review_brief_and_runbook_carry_the_utf8_check():
    """The review side of the rule: steward.md, the watcher's review brief and the cloud runbook all say it."""
    steward = (asset("agents") / "steward.md").read_text(encoding="utf-8")
    assert "python -I <script>` WITHOUT `-X utf8`" in steward and "UnicodeEncodeError" in steward
    watch = (REPO / "scr" / "watch.py").read_text(encoding="utf-8")
    assert "WITHOUT -X utf8 and with PYTHONUTF8 / PYTHONIOENCODING unset" in watch
    runbook = (REPO / "docs" / "cloud-pool.md").read_text(encoding="utf-8")
    assert "## Encoding rule for scripts" in runbook and "Scriptorium cloud pool" in runbook
