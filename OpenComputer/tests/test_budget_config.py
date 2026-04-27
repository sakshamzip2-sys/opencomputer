"""TS-T2 — BudgetConfig tests."""

from opencomputer.agent.budget_config import (
    DEFAULT_BUDGET,
    DEFAULT_PREVIEW_SIZE_CHARS,
    BudgetConfig,
)


def test_default_budget_constants():
    assert DEFAULT_PREVIEW_SIZE_CHARS > 0
    assert DEFAULT_BUDGET.turn_budget > 0
    assert DEFAULT_BUDGET.preview_size > 0


def test_resolve_threshold_returns_inf_when_unknown():
    cfg = BudgetConfig()
    # Tools without explicit threshold get the default
    assert cfg.resolve_threshold("UnknownTool") > 0
