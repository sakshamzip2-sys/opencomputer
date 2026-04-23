"""Phase 12b.2 — Sub-project B, Tasks B4 + B5.

B4: docs/plugin-authors.md + docs/sdk-reference.md exist, are non-trivial,
    and sdk-reference.md covers every plugin_sdk/__init__.py __all__ export.
B5: extensions/weather-example/ is a bundled plugin — discoverable by the
    real loader, valid manifest, loads cleanly.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_sdk_all_names() -> list[str]:
    """Extract the __all__ list from plugin_sdk/__init__.py via AST."""
    sdk_init = _REPO_ROOT / "plugin_sdk" / "__init__.py"
    tree = ast.parse(sdk_init.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    assert isinstance(node.value, ast.List)
                    names = []
                    for element in node.value.elts:
                        assert isinstance(element, ast.Constant)
                        names.append(element.value)
                    return names
    raise AssertionError("no __all__ found in plugin_sdk/__init__.py")


# ─── B4: docs exist + are non-trivial ─────────────────────────────────


def test_plugin_authors_doc_exists_and_non_trivial() -> None:
    p = _REPO_ROOT / "docs" / "plugin-authors.md"
    assert p.exists(), f"expected doc at {p}"
    body = p.read_text(encoding="utf-8")
    assert len(body) > 2000, (
        f"plugin-authors.md is only {len(body)} bytes — expected ~300 lines of real content"
    )
    # Key concepts that the guide must cover.
    assert "plugin.json" in body
    assert "register(api)" in body
    assert "weather-example" in body  # references the bundled example


def test_sdk_reference_doc_exists_and_covers_exports() -> None:
    ref = _REPO_ROOT / "docs" / "sdk-reference.md"
    assert ref.exists(), f"expected doc at {ref}"
    body = ref.read_text(encoding="utf-8")
    assert len(body) > 2000, (
        f"sdk-reference.md is only {len(body)} bytes — too thin to cover 34 exports"
    )
    names = _parse_sdk_all_names()
    assert names, "parsed __all__ list was empty — AST walk bug"
    missing = [n for n in names if n not in body]
    assert not missing, (
        f"these plugin_sdk exports are not mentioned in sdk-reference.md: {missing}"
    )


# ─── B5: weather-example bundled plugin loads ─────────────────────────


def test_weather_example_bundled_plugin_loads() -> None:
    """The weather-example extension must be discovered by the real loader."""
    from opencomputer.plugins.discovery import discover

    ext = _REPO_ROOT / "extensions"
    candidates = discover([ext])
    ids = [c.manifest.id for c in candidates]
    assert "weather-example" in ids, (
        f"weather-example not in discovered ids: {ids}"
    )
    # And the manifest should be a provider kind with the expected metadata.
    wx = next(c for c in candidates if c.manifest.id == "weather-example")
    assert wx.manifest.kind == "provider"
    assert wx.manifest.entry == "plugin"
