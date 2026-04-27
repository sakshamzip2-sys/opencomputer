"""Smoke audit — every tool description must clear a quality bar.

V3.A-T4: tool descriptions are nudge-text the model reads at every dispatch.
This audit enforces a 120-char floor on description length AND requires
destructive tools (those that mutate the filesystem, run shell commands,
or execute arbitrary code) to include warning/guidance language.

Two tool surfaces are audited:
  1. Built-in tools registered by ``opencomputer.cli._register_builtin_tools``.
  2. Coding-harness extension tools loaded directly from
     ``extensions/coding-harness/`` (the dir is hyphenated so it can't be
     imported as a package; we use the same ``sys.path`` shim that
     ``test_phase6c.py`` uses).

The OI bridge tools (``extensions/coding-harness/oi_bridge/tools/``) live
under the coding-harness umbrella — they are imported via the
``extensions.coding_harness`` alias that ``conftest.py`` installs.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

from opencomputer.cli import _register_builtin_tools
from opencomputer.tools.registry import registry as _builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "extensions" / "coding-harness"

# Names of every tool whose description must contain at least one warning /
# guidance keyword. These tools mutate the filesystem, run arbitrary commands,
# or otherwise have side effects the model should think twice about.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "Edit",
        "MultiEdit",
        "Write",
        "Bash",
        "PythonExec",
        "AppleScriptRun",
    }
)

# At least one of these substrings must appear (case-insensitively) in every
# destructive tool's description so the model is reminded of the foot-guns.
WARNING_KEYWORDS: tuple[str, ...] = (
    "read first",
    "review",
    "preserves",
    "denylist",
    "warn",
    "caution",
    "use",
    "prefer",
)

MIN_DESCRIPTION_CHARS = 120


# ─── Built-in tool collection ────────────────────────────────────


def _builtin_schemas():
    """All tool schemas from the core CLI registry (idempotent)."""
    _register_builtin_tools()
    return list(_builtin_registry.schemas())


# ─── Coding-harness tool collection ──────────────────────────────


@pytest.fixture(scope="module")
def harness_schemas() -> list:
    """Collect ToolSchema objects from every coding-harness extension tool.

    Loaded directly from ``extensions/coding-harness/`` via the ``sys.path``
    shim used elsewhere in the test suite. The descriptor-only path means
    we don't need a HarnessContext for tools that take ``ctx`` — the
    ``schema`` property is a class attribute on BaseTool subclasses, so we
    pass a minimal stub when the constructor demands one.
    """
    sys.path.insert(0, str(PLUGIN_ROOT))
    # Purge cached harness internals so we always load the fresh sources.
    for mod_name in list(sys.modules):
        if mod_name.split(".")[0] in {
            "context",
            "rewind",
            "state",
            "tools",
            "hooks",
            "plan_mode",
        }:
            sys.modules.pop(mod_name, None)
    try:
        # Tier-2 tools that take a HarnessContext: provide a duck-typed stub
        # whose attribute access is benign because we never call .schema's
        # implementation on context (schema is a static @property).
        class _NullCtx:
            rewind_store = None
            session_state = None

            def emit_progress(self, *a, **k) -> None:
                return None

        ctx = _NullCtx()

        from tools.background import (  # type: ignore[import-not-found]
            CheckOutputTool,
            KillProcessTool,
            StartProcessTool,
        )
        from tools.diff import CheckpointDiffTool  # type: ignore[import-not-found]
        from tools.edit import EditTool  # type: ignore[import-not-found]
        from tools.exit_plan_mode import (
            ExitPlanModeTool,  # type: ignore[import-not-found]
        )
        from tools.multi_edit import MultiEditTool  # type: ignore[import-not-found]
        from tools.rewind import RewindTool  # type: ignore[import-not-found]
        from tools.run_tests import RunTestsTool  # type: ignore[import-not-found]
        from tools.todo_write import TodoWriteTool  # type: ignore[import-not-found]

        instances = [
            EditTool(),
            MultiEditTool(),
            TodoWriteTool(),
            ExitPlanModeTool(),
            StartProcessTool(),
            CheckOutputTool(),
            KillProcessTool(),
            RewindTool(ctx=ctx),
            CheckpointDiffTool(ctx=ctx),
            RunTestsTool(ctx=ctx),
        ]

        # OI Tier-1 introspection tools — five of them. Use the
        # extensions.coding_harness alias installed by tests/conftest.py.
        try:
            tier1_mod = importlib.import_module(
                "extensions.coding_harness.oi_bridge.tools.tier_1_introspection"
            )
            wrapper_mod = importlib.import_module(
                "extensions.coding_harness.oi_bridge.subprocess.wrapper"
            )
            wrapper = wrapper_mod.OISubprocessWrapper()
            for tool_cls in tier1_mod.ALL_TOOLS:
                instances.append(tool_cls(wrapper=wrapper))
        except Exception:  # noqa: BLE001
            # If the alias isn't wired we still want the rest of the
            # extension audit to fire; the OI tools just won't be checked.
            pass

        return [t.schema for t in instances]
    finally:
        if str(PLUGIN_ROOT) in sys.path:
            sys.path.remove(str(PLUGIN_ROOT))


# ─── Audit assertions ────────────────────────────────────────────


def test_every_builtin_tool_description_is_at_least_120_chars():
    """Built-in tools must clear the 120-char floor — descriptions <120 are
    almost certainly unfit nudge-text (verb + one noun isn't a hint about
    when/when-not-to-use)."""
    failures: list[str] = []
    for schema in _builtin_schemas():
        L = len(schema.description)
        if L < MIN_DESCRIPTION_CHARS:
            failures.append(f"{schema.name}: {L} chars")
    assert not failures, (
        f"Tools with thin descriptions (must be >= {MIN_DESCRIPTION_CHARS}): "
        + ", ".join(failures)
    )


def test_every_harness_tool_description_is_at_least_120_chars(harness_schemas):
    """Coding-harness extension tools must clear the same 120-char floor."""
    failures: list[str] = []
    for schema in harness_schemas:
        L = len(schema.description)
        if L < MIN_DESCRIPTION_CHARS:
            failures.append(f"{schema.name}: {L} chars")
    assert not failures, (
        f"Harness tools with thin descriptions (must be >= {MIN_DESCRIPTION_CHARS}): "
        + ", ".join(failures)
    )


def test_destructive_tools_warn_in_description(harness_schemas):
    """Tools that mutate FS / send messages / run commands must include
    warning or guidance text so the model is reminded of pitfalls.

    Combines the built-in registry + coding-harness extension surface so
    Edit/MultiEdit (which only register through the plugin) are covered too.
    """
    all_schemas = list(_builtin_schemas()) + list(harness_schemas)
    failures: list[str] = []
    for schema in all_schemas:
        if schema.name not in DESTRUCTIVE_TOOLS:
            continue
        desc = schema.description.lower()
        if not any(kw in desc for kw in WARNING_KEYWORDS):
            failures.append(schema.name)
    assert not failures, (
        f"Destructive tools missing warning/guidance keywords "
        f"({list(WARNING_KEYWORDS)!r}): {failures}"
    )
