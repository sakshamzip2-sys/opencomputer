"""AGPL boundary CI test — load-bearing guarantee.

Greps the entire codebase for `import interpreter` or `from interpreter`
lines outside the allowed path (subprocess/server.py). Any match fails the build.

This is the CI enforcement of the AGPL isolation strategy described in
extensions/oi-capability/NOTICE and docs/f7/design.md §2.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

# Root of the project (two levels up from this test file)
PROJECT_ROOT = Path(__file__).parent.parent

# The ONE file that IS allowed to import interpreter (runs in the OI venv subprocess)
ALLOWED_PATH = PROJECT_ROOT / "extensions" / "oi-capability" / "subprocess" / "server.py"

# Pattern that would indicate AGPL contamination
_FORBIDDEN_PATTERN = re.compile(
    r"^\s*(?:import interpreter|from interpreter\b)",
    re.MULTILINE,
)


def _scan_for_forbidden_imports(root: Path, exclude: Path | None = None) -> list[tuple[Path, int, str]]:
    """Scan all .py files under root for forbidden OI import lines.

    Returns a list of (file_path, line_number, line_text) for each violation.
    """
    violations: list[tuple[Path, int, str]] = []
    for py_file in root.rglob("*.py"):
        if exclude is not None and py_file == exclude:
            continue
        # Skip generated cache directories
        if any(part.startswith(".") or part == "__pycache__" for part in py_file.parts):
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if _FORBIDDEN_PATTERN.search(line):
                violations.append((py_file, lineno, line.strip()))
    return violations


class TestAGPLBoundary:
    """Suite: AGPL boundary enforcement."""

    def test_no_forbidden_imports_in_codebase(self):
        """No Python file (except server.py) may contain `import interpreter` or `from interpreter`."""
        violations = _scan_for_forbidden_imports(PROJECT_ROOT, exclude=ALLOWED_PATH)

        if violations:
            report_lines = [
                f"\n  {path.relative_to(PROJECT_ROOT)}:{lineno}: {line}"
                for path, lineno, line in violations
            ]
            pytest.fail(
                "AGPL BOUNDARY VIOLATION — found forbidden `import interpreter` / "
                "`from interpreter` outside the allowed subprocess boundary:\n"
                + "".join(report_lines)
                + "\n\nOnly extensions/oi-capability/subprocess/server.py may import OI. "
                "See extensions/oi-capability/NOTICE for the isolation strategy."
            )

    def test_allowed_file_exists_and_has_oi_import(self):
        """Confirm that server.py (the allowed boundary file) exists and does contain the OI import.

        This test ensures the allowed-list stays in sync with reality — if someone
        renames server.py, this test catches the stale allowed-path.
        """
        assert ALLOWED_PATH.exists(), (
            f"Allowed OI import boundary file not found: {ALLOWED_PATH}\n"
            "Did someone rename or move subprocess/server.py?"
        )
        content = ALLOWED_PATH.read_text(encoding="utf-8")
        has_oi_import = bool(re.search(r"from interpreter import|import interpreter", content))
        assert has_oi_import, (
            f"server.py ({ALLOWED_PATH}) does not contain any `from interpreter import` "
            "or `import interpreter` line. This is suspicious — "
            "the server should be importing Open Interpreter."
        )

    def test_planted_import_would_fail_scan(self):
        """Confirm the scanner would catch a forbidden import if planted in an arbitrary file.

        Regression guard: verifies the scanner itself works by running it against
        a temporary file that contains a forbidden import.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            evil_file = Path(tmpdir) / "evil_plugin.py"
            evil_file.write_text(
                "# This file simulates an AGPL contamination\n"
                "import interpreter\n"
                "\n"
                "def some_function():\n"
                "    from interpreter import OpenInterpreter\n"
            )
            violations = _scan_for_forbidden_imports(Path(tmpdir))
            assert len(violations) == 2, (
                f"Expected 2 violations (one `import interpreter` + one `from interpreter`), "
                f"got {len(violations)}: {violations}"
            )
            # Both lines are in the evil file
            assert all(v[0] == evil_file for v in violations)
            # Line numbers are correct
            line_nums = {v[1] for v in violations}
            assert 2 in line_nums  # `import interpreter` is on line 2
            assert 5 in line_nums  # `from interpreter import` is on line 5
