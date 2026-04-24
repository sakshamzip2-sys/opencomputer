"""License boundary: no AGPL-v3 Open Interpreter imports in core or SDK.

If this fails, someone has crossed the AGPL boundary. OpenComputer is
MIT-licensed and published open-source; linking to AGPL code would
contaminate the whole stack. F7 will put OI behind a JSON-RPC subprocess,
never imports it.
"""
import re
from pathlib import Path

FORBIDDEN_PATTERNS = [
    re.compile(r"^\s*import\s+interpreter\b", re.M),
    re.compile(r"^\s*from\s+interpreter\b", re.M),
    re.compile(r"^\s*import\s+openinterpreter\b", re.M),
    re.compile(r"^\s*from\s+openinterpreter\b", re.M),
]


def _scan(roots: list[Path]) -> list[tuple[Path, str]]:
    violations: list[tuple[Path, str]] = []
    for root in roots:
        for py in root.rglob("*.py"):
            text = py.read_text()
            for pat in FORBIDDEN_PATTERNS:
                m = pat.search(text)
                if m:
                    violations.append((py, m.group(0)))
    return violations


def test_no_open_interpreter_imports_in_core_or_sdk():
    violations = _scan([Path("opencomputer"), Path("plugin_sdk")])
    msg = "\n".join(f"  {p}: {m}" for p, m in violations)
    assert violations == [], (
        f"AGPL boundary violated in {len(violations)} file(s):\n{msg}"
    )
