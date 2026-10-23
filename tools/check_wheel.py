"""Build the wheel, install it NON-editable into a fresh venv, and run the installed `scr` against a state root
that holds only data: the package's own files (scr/assets.py) must come from site-packages, never a checkout.

    python tools/check_wheel.py          # needs `uv` on PATH; CI runs it after the test suite
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def run(*cmd: str, cwd: Path | None = None, env: dict | None = None) -> str:
    r = subprocess.run(list(cmd), cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"FAILED: {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def main() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        run("uv", "build", "--wheel", "--out-dir", str(tmp / "dist"), str(REPO))
        wheel = next((tmp / "dist").glob("*.whl"))
        venv = tmp / "venv"
        run("uv", "venv", "--quiet", str(venv))
        py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run("uv", "pip", "install", "--quiet", "--python", str(py), str(wheel))
        scr_exe = venv / ("Scripts/scr.exe" if os.name == "nt" else "bin/scr")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "SCRIPTORIUM_ROOT")}
        env["SCR_NO_JOURNAL"] = "1"
        state = tmp / "state"
        (state / "config").mkdir(parents=True)
        # the installed package, imported from outside the checkout
        where = run(str(py), "-c", "import scr, scr.assets as a; print(scr.__file__); print(a.ASSETS)", cwd=tmp, env=env).split()
        assert "site-packages" in where[0] and str(REPO) not in where[0], where
        for name in ("templates/spec.md", "templates/review.md", "agents/steward.md", "ui/index.html", "ui/chores.yaml",
                     "hooks/loopy_phase_c.ps1"):
            assert (Path(where[1]) / name).is_file(), f"missing from the wheel: assets/{name}"
        assert (Path(where[0]).parent / "defaults" / "config" / "limits.yaml").is_file(), "missing from the wheel: defaults/config"
        run(str(scr_exe), "--root", str(state), "init", "--quiet", cwd=tmp, env=env)
        assert not (state / "templates").exists() and not (state / "agents").exists(), "init created code folders in the state root"
        run(str(scr_exe), "--root", str(state), "--quiet", "task", "new", "A", "--id", "T-0001", "--origin", "omri", cwd=tmp, env=env)
        spec = next((state / "tasks").glob("T-0001-*")) / "spec.md"
        assert spec.read_text(encoding="utf-8") == (Path(where[1]) / "templates" / "spec.md").read_text(encoding="utf-8")
        hooks = run(str(scr_exe), "assets", "hooks", cwd=tmp, env=env).strip()
        assert Path(hooks) == Path(where[1]) / "hooks", hooks
        run(str(scr_exe), "--root", str(state), "verify", "--quiet", cwd=tmp, env=env)
        print(f"ok: {wheel.name} installs non-editable; assets from {where[1]}")


if __name__ == "__main__":
    main()
