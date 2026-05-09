"""Plugin loader: shared subpackage names don't collide across plugins.

Pins the fix added 2026-05-09 to ``opencomputer/plugins/loader.py``:

* When two extensions both have a top-level ``slash_commands/`` /
  ``state/`` / ``tools/`` etc., loading the second one MUST resolve
  its OWN siblings, not the first plugin's cached version.
* The loader now (a) moves ``plugin_root`` to ``sys.path[0]``
  unconditionally before each exec_module, and (b) clears the
  expanded ``_PLUGIN_LOCAL_NAMES`` set (with prefix wildcard) so
  cached subpackages don't shadow re-resolution.

Caught by `test_phase12b4_exit_plan_mode::test_coding_harness_registers_exit_plan_mode`
which previously failed under pytest because conftest registers
voice-mode (slash_commands/ has no accept_edits.py) before the
test loads coding-harness's plugin (which expects to find its OWN
slash_commands/accept_edits.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

from opencomputer.plugins.loader import (
    _PLUGIN_LOCAL_NAMES,
    _clear_plugin_local_cache,
)


class TestPluginLocalNamesCoverage:
    """Pin the expanded clear-list against silent regression."""

    def test_subpackage_names_in_clear_list(self) -> None:
        # Names that appear in MULTIPLE extension dirs (verified by
        # `find extensions -maxdepth 3 -type d -name X`).
        for name in (
            "slash_commands",
            "state",
            "tools",
            "modes",
            "permissions",
            "rewind",
            "introspection",
        ):
            assert name in _PLUGIN_LOCAL_NAMES, (
                f"Expected {name!r} in _PLUGIN_LOCAL_NAMES so the loader "
                f"clears any cached version before each plugin loads. "
                f"Without it, two plugins with the same subpackage name "
                f"silently share whichever loaded first → bare imports "
                f"resolve to the wrong directory."
            )


class TestClearCacheClearsNestedSubmodules:
    """`_clear_plugin_local_cache` must also pop nested submodules
    (e.g. `slash_commands.accept_edits`), not just the top-level
    package — otherwise re-importing the package finds a stale
    submodule cache."""

    def test_pops_nested_submodule(self) -> None:
        # Plant a fake nested entry in sys.modules
        sys.modules["slash_commands"] = object()  # type: ignore[assignment]
        sys.modules["slash_commands.accept_edits"] = object()  # type: ignore[assignment]
        sys.modules["slash_commands.deeper.nest"] = object()  # type: ignore[assignment]

        _clear_plugin_local_cache()

        assert "slash_commands" not in sys.modules
        assert "slash_commands.accept_edits" not in sys.modules
        assert "slash_commands.deeper.nest" not in sys.modules

    def test_does_not_pop_unrelated_modules(self) -> None:
        # Defensive — must not nuke `slash_commands_unrelated` or
        # `not_slash_commands` that just happen to share a substring.
        sys.modules["slash_commandsX"] = object()  # type: ignore[assignment]
        sys.modules["unrelated_slash_commands"] = object()  # type: ignore[assignment]
        try:
            _clear_plugin_local_cache()
            assert "slash_commandsX" in sys.modules
            assert "unrelated_slash_commands" in sys.modules
        finally:
            sys.modules.pop("slash_commandsX", None)
            sys.modules.pop("unrelated_slash_commands", None)


class TestPluginRootMovesToFront:
    """Verify the loader moves plugin_root to sys.path[0] even when
    it was already on sys.path. Source-level check rather than
    spinning up a real plugin load (the latter is exercised by
    test_phase12b4)."""

    def test_loader_uses_remove_then_insert_pattern(self) -> None:
        from pathlib import Path as _P

        loader_src = _P("opencomputer/plugins/loader.py").read_text()
        # The fix uses ``while plugin_root_str in sys.path: sys.path.remove(...)``
        # followed by ``sys.path.insert(0, ...)`` — both lines must be present.
        assert "while plugin_root_str in sys.path:" in loader_src
        assert "sys.path.remove(plugin_root_str)" in loader_src
        assert "sys.path.insert(0, plugin_root_str)" in loader_src
