"""Verifies extensions/<plugin-name>/ don't import sibling extensions' internals.

This is the cross-plugin boundary analogue of
tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer.

Mirrors OpenClaw's `check-no-extension-src-imports.ts` build-time rule
(see sources/openclaw/src/plugin-sdk/AGENTS.md). Bundled plugins today
don't cross-import, but we want this CI-visible before a third-party
plugin ecosystem ships.

A plugin may legitimately import from its own internals — e.g.
`extensions/anthropic-provider/plugin.py` imports `extensions.anthropic_provider.provider`
(package-mode fallback). Python packages can't contain dashes, so the folder
`anthropic-provider` is imported as `anthropic_provider`. The scanner must
treat both the raw folder name and its underscore-normalized variant as
"self" for that plugin.
"""
from __future__ import annotations

import re
from pathlib import Path

EXTENSIONS_ROOT = Path(__file__).resolve().parent.parent / "extensions"

# Plugins that act as runtimes/SDKs other plugins legitimately extend.
# Importing from these is by design, not a boundary violation. Matches
# the SDK pattern (`plugin_sdk/`) but at the extension layer:
#
#   adapter-runner — exposes ``@adapter`` decorator + ``Strategy`` enum +
#                     ``register_adapter_pack`` so adapter-pack plugins
#                     and `browser-control/adapters/*` can author tools.
#
# When more runtime plugins ship, replace this hardcoded set with a
# `plugin.json` metadata flag (e.g. ``"kind": "runtime"``) and read it
# at scan time. Tracked in v0.5+ as DEFERRED follow-up.
RUNTIME_PLUGINS = {
    "adapter-runner",
}

# Bidirectional pairs that are by-design coupled. Each entry says
# "plugin A may import from plugin B AND vice versa." The TODO is to
# break A→B with dependency injection in v0.5; for now, surface the
# pair explicitly so future grep can find it.
ALLOWED_BIDIRECTIONAL = {
    # adapter-runner's ctx wires Browser actions into the adapter
    # `ctx` object for COOKIE/UI/INTERCEPT adapters. Long-term, the
    # browser actions should be injected from browser-control's side
    # via api.register hooks, not imported. Tracked in DEFERRED.md.
    frozenset({"adapter-runner", "browser-control"}),
}


def _canonical_names(plugin_dir_name: str) -> set[str]:
    """Return folder name + its underscore-normalized variant.

    Plugins imported as Python packages can't have dashes, so a plugin
    folder named `dev-tools` is imported as `extensions.dev_tools`.
    Both forms belong to the same plugin for boundary purposes.
    """
    return {plugin_dir_name, plugin_dir_name.replace("-", "_")}


def _scan_for_cross_imports(root: Path) -> list[str]:
    """Scan `root/<plugin>/**/*.py` for imports of sibling plugins.

    Returns a list of human-readable violation strings (empty on clean tree).
    """
    plugins = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    name_to_aliases: dict[str, set[str]] = {
        p.name: _canonical_names(p.name) for p in plugins
    }
    violations: list[str] = []

    for plugin in plugins:
        self_aliases = name_to_aliases[plugin.name]
        for py_file in plugin.rglob("*.py"):
            try:
                text = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for other_name, other_aliases in name_to_aliases.items():
                if other_name == plugin.name:
                    continue
                # Skip declared runtime plugins — by design extensible
                # from sibling plugins (analogue of plugin_sdk).
                if other_name in RUNTIME_PLUGINS:
                    continue
                # Skip pairs explicitly allowlisted as by-design coupled.
                if frozenset({plugin.name, other_name}) in ALLOWED_BIDIRECTIONAL:
                    continue
                # A sibling's alias set may share entries with our own only
                # if the folder names collide after normalization — in that
                # edge case we can't distinguish, so skip.
                foreign_aliases = other_aliases - self_aliases
                for alias in foreign_aliases:
                    patterns = [
                        rf"\bfrom\s+extensions\.{re.escape(alias)}\b",
                        rf"\bimport\s+extensions\.{re.escape(alias)}\b",
                    ]
                    for pat in patterns:
                        if re.search(pat, text):
                            rel = py_file.relative_to(root.parent)
                            violations.append(
                                f"{rel} imports from extensions.{alias} "
                                f"(sibling plugin '{other_name}')"
                            )
    return violations


def test_no_cross_plugin_imports() -> None:
    """Bundled extensions/<plugin>/ must not import sibling plugins' internals."""
    violations = _scan_for_cross_imports(EXTENSIONS_ROOT)
    assert not violations, (
        "CROSS-PLUGIN BOUNDARY VIOLATION — plugins importing sibling internals:\n"
        + "\n".join(violations)
    )


def test_scanner_detects_synthetic_violation(tmp_path: Path) -> None:
    """Positive control: construct a violating tree, assert scanner flags it.

    We don't want to rely solely on "the current tree is clean" to prove the
    scanner works — if it was silently broken (e.g. regex wrong, io swallowed),
    the main test would pass vacuously. This test plants a deliberate
    `from extensions.good_plugin` import inside `bad_plugin/` and checks it's
    caught.
    """
    fake_root = tmp_path / "extensions"
    fake_root.mkdir()
    good = fake_root / "good_plugin"
    good.mkdir()
    (good / "tool.py").write_text("x = 1\n", encoding="utf-8")
    bad = fake_root / "bad_plugin"
    bad.mkdir()
    (bad / "plugin.py").write_text(
        "from extensions.good_plugin.tool import x\n", encoding="utf-8"
    )

    violations = _scan_for_cross_imports(fake_root)

    assert any(
        "bad_plugin" in v and "extensions.good_plugin" in v for v in violations
    ), f"scanner failed to flag synthetic violation; got: {violations!r}"


def test_scanner_detects_hyphen_plugin_violation(tmp_path: Path) -> None:
    """Hyphen-named plugins (e.g. anthropic-provider) must still be protected.

    Real bundled plugins use dashes in folder names but underscores in imports.
    A hostile `from extensions.anthropic_provider` inside some other plugin
    must be caught even though the folder literally is `anthropic-provider`.
    """
    fake_root = tmp_path / "extensions"
    fake_root.mkdir()
    hyphen = fake_root / "my-provider"
    hyphen.mkdir()
    (hyphen / "provider.py").write_text("y = 2\n", encoding="utf-8")
    other = fake_root / "attacker"
    other.mkdir()
    (other / "plugin.py").write_text(
        "from extensions.my_provider.provider import y\n", encoding="utf-8"
    )

    violations = _scan_for_cross_imports(fake_root)

    assert any(
        "attacker" in v and "extensions.my_provider" in v for v in violations
    ), f"scanner failed to flag hyphen-plugin violation; got: {violations!r}"


def test_scanner_ignores_legitimate_self_imports(tmp_path: Path) -> None:
    """Self-imports (a plugin importing its own internals) must NOT fire.

    This matches real bundled plugins: `extensions/anthropic-provider/plugin.py`
    imports `extensions.anthropic_provider.provider` (underscore-normalized)
    — that's a self-import, not a cross-plugin violation.
    """
    fake_root = tmp_path / "extensions"
    fake_root.mkdir()
    p = fake_root / "anthropic-provider"
    p.mkdir()
    (p / "provider.py").write_text("v = 1\n", encoding="utf-8")
    (p / "plugin.py").write_text(
        "from extensions.anthropic_provider.provider import v\n", encoding="utf-8"
    )

    violations = _scan_for_cross_imports(fake_root)

    assert not violations, (
        f"scanner incorrectly flagged a self-import as cross-plugin: {violations!r}"
    )
