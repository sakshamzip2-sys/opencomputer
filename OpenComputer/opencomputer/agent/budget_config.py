"""Configurable budget constants for tool result persistence.

Ported near-verbatim from
``sources/hermes-agent-2026.4.23/tools/budget_config.py`` for OpenComputer
(TS-T2). Per-tool resolution: pinned > config overrides > registry > default.

The registry lookup is best-effort — if ``opencomputer.tools.registry`` does
not expose a ``get_max_result_size`` API yet, the default is used. This keeps
the port self-contained without requiring registry changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Tools whose thresholds must never be overridden.
# ``read_file``=inf prevents infinite persist->read->persist loops. The
# corresponding tool in OC is named ``Read``; we list both spellings so the
# guard works regardless of which name is dispatched.
PINNED_THRESHOLDS: dict[str, float] = {
    "read_file": float("inf"),
    "Read": float("inf"),
}

# Defaults matching Hermes's hardcoded values exactly.
# Kept here as the single source of truth; tool_result_storage.py imports them.
DEFAULT_RESULT_SIZE_CHARS: int = 100_000
DEFAULT_TURN_BUDGET_CHARS: int = 200_000
DEFAULT_PREVIEW_SIZE_CHARS: int = 1_500


@dataclass(frozen=True)
class BudgetConfig:
    """Immutable budget constants for the 3-layer tool result persistence system.

    Layer 2 (per-result): resolve_threshold(tool_name) -> threshold in chars.
    Layer 3 (per-turn):   turn_budget -> aggregate char budget across all tool
                          results in a single assistant turn.
    Preview:              preview_size -> inline snippet size after persistence.
    """

    default_result_size: int = DEFAULT_RESULT_SIZE_CHARS
    turn_budget: int = DEFAULT_TURN_BUDGET_CHARS
    preview_size: int = DEFAULT_PREVIEW_SIZE_CHARS
    tool_overrides: dict[str, int] = field(default_factory=dict)

    def resolve_threshold(self, tool_name: str) -> int | float:
        """Resolve the persistence threshold for a tool.

        Priority: pinned -> tool_overrides -> registry per-tool -> default.
        """
        if tool_name in PINNED_THRESHOLDS:
            return PINNED_THRESHOLDS[tool_name]
        if tool_name in self.tool_overrides:
            return self.tool_overrides[tool_name]
        # Best-effort registry lookup. The OC tool registry does not expose a
        # ``get_max_result_size`` hook today; if added later (e.g. via tool
        # metadata), this branch picks it up automatically. No registry
        # import on the hot path otherwise — keeps the port standalone.
        try:
            from opencomputer.tools.registry import registry  # type: ignore[import-not-found]

            getter = getattr(registry, "get_max_result_size", None)
            if callable(getter):
                return getter(tool_name, default=self.default_result_size)
        except Exception:  # noqa: BLE001 — never break threshold resolution
            pass
        return self.default_result_size


# Default config -- matches current hardcoded behavior exactly.
DEFAULT_BUDGET = BudgetConfig()


__all__ = [
    "BudgetConfig",
    "DEFAULT_BUDGET",
    "DEFAULT_PREVIEW_SIZE_CHARS",
    "DEFAULT_RESULT_SIZE_CHARS",
    "DEFAULT_TURN_BUDGET_CHARS",
    "PINNED_THRESHOLDS",
]
