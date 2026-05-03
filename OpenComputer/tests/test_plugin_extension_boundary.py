"""Boundary test - extensions may only import from plugin_sdk.

Sub-project G (openclaw-parity) Task 11. Mirrors openclaw's
``test/extension-package-tsc-boundary.test.ts`` semantics in Python:
extensions may only import from ``plugin_sdk.*``, their own files,
stdlib, and third-party deps - NOT from ``opencomputer.*``.

This test ships with a frozen inventory of the 26 existing violators
(see ``tests/fixtures/plugin_extension_import_boundary_inventory.json``).
The test FAILS when:

  (a) An extension introduces a NEW ``from opencomputer.*`` import not
      in the inventory, OR
  (b) An existing extension adds a NEW ``opencomputer.*`` import to
      its previously-known set, OR
  (c) The inventory has a stale entry (file removed / renamed).

To resolve a failure:

  - If you legitimately need a new core import: bring it through
    ``plugin_sdk`` instead, then update the inventory only as a last
    resort with ``python scripts/refresh_extension_boundary_inventory.py``.
  - If you removed a file or stopped using a core import: regenerate
    the inventory the same way.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.refresh_extension_boundary_inventory import (
    EXTENSIONS_DIR,
    INVENTORY_PATH,
    REPO_ROOT,
    collect_imports,
)


def _live_inventory() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for py in sorted(EXTENSIONS_DIR.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).as_posix()
        imports = collect_imports(py)
        if imports:
            out[rel] = imports
    return out


def _frozen_inventory() -> dict[str, list[str]]:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


class TestExtensionBoundary:
    def test_inventory_file_exists(self) -> None:
        assert INVENTORY_PATH.exists(), (
            f"frozen inventory not found at {INVENTORY_PATH} - "
            "run `python scripts/refresh_extension_boundary_inventory.py`"
        )

    def test_no_new_violation_files(self) -> None:
        live = _live_inventory()
        frozen = _frozen_inventory()
        new_files = sorted(set(live) - set(frozen))
        assert not new_files, (
            "NEW extension files import from opencomputer.* "
            "(should import from plugin_sdk only):\n  "
            + "\n  ".join(f"{f}: {live[f]}" for f in new_files)
            + "\n\nFix by routing the import through plugin_sdk, OR if truly "
            "unavoidable, run `python scripts/refresh_extension_boundary_inventory.py` "
            "to update the inventory (and explain why in the PR description)."
        )

    def test_no_new_imports_in_existing_files(self) -> None:
        live = _live_inventory()
        frozen = _frozen_inventory()
        new_imports: list[str] = []
        for f, imports in live.items():
            if f not in frozen:
                continue
            extras = sorted(set(imports) - set(frozen[f]))
            if extras:
                new_imports.append(f"{f}: {extras}")
        assert not new_imports, (
            "Existing extension files added NEW opencomputer.* imports:\n  "
            + "\n  ".join(new_imports)
            + "\n\nFix by routing through plugin_sdk, OR run "
            "`python scripts/refresh_extension_boundary_inventory.py`."
        )

    def test_no_stale_inventory_entries(self) -> None:
        live = _live_inventory()
        frozen = _frozen_inventory()
        stale = sorted(set(frozen) - set(live))
        assert not stale, (
            "Inventory has entries for files that no longer exist or no "
            "longer import opencomputer.*:\n  "
            + "\n  ".join(stale)
            + "\n\nRun `python scripts/refresh_extension_boundary_inventory.py` "
            "to clean up."
        )
