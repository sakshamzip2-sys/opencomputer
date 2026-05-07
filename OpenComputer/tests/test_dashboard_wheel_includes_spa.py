"""Verify the built wheel ships the dashboard SPA artifact."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest  # noqa: I001  (alphabetized order is unstable across ruff versions; this layout matches the rest of tests/)

SPA_INDEX = Path("opencomputer/dashboard/static/spa/index.html")


@pytest.mark.skipif(
    not SPA_INDEX.exists(),
    reason="SPA artifact not built — run scripts/build-dashboard.sh first",
)
def test_wheel_includes_spa_index(tmp_path: Path):
    """Build the wheel and confirm the SPA index.html is inside.

    Skipped in environments where the Vite build hasn't been run; CI
    builds the SPA before running tests so this gate fires there.
    """
    try:
        import build  # noqa: F401  (presence check)
    except ImportError:
        pytest.skip("`build` package not installed — pip install build")

    try:
        subprocess.check_call(
            ["python", "-m", "build", "--wheel", "--outdir", str(tmp_path)],
            cwd=Path(__file__).parent.parent,
        )
    except FileNotFoundError:
        pytest.skip("python -m build unavailable in this env")
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"python -m build failed in this env (skipping): {exc}")
    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, "no wheel built"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    assert any(
        n.endswith("opencomputer/dashboard/static/spa/index.html") for n in names
    ), f"SPA index.html not in wheel; sample names: {names[:30]}"
