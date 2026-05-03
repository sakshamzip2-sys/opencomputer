"""Refresh the frozen-inventory fixture for the extension-boundary test.

Walks ``extensions/*/**.py`` and records every ``from opencomputer.X
import Y`` / ``import opencomputer.X``. Output is a JSON file mapping
relative-path -> sorted list of imported modules.

Run when an extension is removed OR an existing extension's imports
legitimately need to change. NEW extensions should not introduce new
``opencomputer.*`` imports - the boundary test will fail in that case.

Sub-project G (openclaw-parity) Task 11. Mirrors openclaw
``test/fixtures/plugin-extension-import-boundary-inventory.json``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS_DIR = REPO_ROOT / "extensions"
INVENTORY_PATH = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "plugin_extension_import_boundary_inventory.json"
)


def collect_imports(py_path: Path) -> list[str]:
    """Return sorted list of ``opencomputer.*`` imports in ``py_path``."""
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "opencomputer" or mod.startswith("opencomputer."):
                found.add(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "opencomputer" or alias.name.startswith("opencomputer."):
                    found.add(alias.name)
    return sorted(found)


def main() -> int:
    inventory: dict[str, list[str]] = {}
    for py in sorted(EXTENSIONS_DIR.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).as_posix()
        imports = collect_imports(py)
        if imports:
            inventory[rel] = imports
    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_PATH.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(inventory)} entries to {INVENTORY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
