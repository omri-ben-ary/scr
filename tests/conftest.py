from __future__ import annotations

import sys
from pathlib import Path

# A spawned worktree links the checkout's .venv, whose editable install points `import scr` at the checkout,
# and a bare pytest.exe (unlike `python -m pytest`) does not put the cwd on sys.path, so it would test the wrong tree.
# Putting this tree's root first makes both commands import the scr under test (T-0061's oracle, T-0106's fix).
ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[:1] != [ROOT]:
    sys.path.insert(0, ROOT)
