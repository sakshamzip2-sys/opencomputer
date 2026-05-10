"""Tokenjuice — tool-result compaction strategies + agent-loop wiring.

Pins the production behaviour: which strategy does what, the
do-not-compact guard for ``Read``-class tools, the hard-ceiling
defence against pathological output, and the "any crash returns
original" defensive contract.
"""
from __future__ import annotations

import logging

from opencomputer.agent.tokenjuice import (
    DEFAULT_DO_NOT_COMPACT,
    TokenjuiceConfig,
    ToolCompactionRule,
    compact_tool_result,
)


def _big(lines: int, prefix: str = "line") -> str:
    return "\n".join(f"{prefix} {i}" for i in range(lines))


# ─── disabled / skipped paths ─────────────────────────────────────────


def test_disabled_config_returns_input_unchanged():
    cfg = TokenjuiceConfig(enabled=False)
    big = _big(5_000)
    assert compact_tool_result(tool_name="Bash", content=big, config=cfg) == big


def test_do_not_compact_list_protects_read():
    cfg = TokenjuiceConfig(
        enabled=True,
        default_rule=ToolCompactionRule(strategy="truncate", max_lines=5),
    )
    big = _big(5_000)
    assert compact_tool_result(tool_name="Read", content=big, config=cfg) == big
    assert compact_tool_result(tool_name="ReadFile", content=big, config=cfg) == big
    assert compact_tool_result(tool_name="NotebookRead", content=big, config=cfg) == big


def test_strategy_none_rule_returns_unchanged():
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={"Bash": ToolCompactionRule(strategy="none")},
    )
    big = _big(5_000)
    assert compact_tool_result(tool_name="Bash", content=big, config=cfg) == big


def test_below_max_lines_threshold_returns_unchanged():
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={"Bash": ToolCompactionRule(strategy="truncate", max_lines=200)},
    )
    small = _big(50)
    assert compact_tool_result(tool_name="Bash", content=small, config=cfg) == small


# ─── truncate strategy ────────────────────────────────────────────────


def test_truncate_keeps_head_and_tail():
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={
            "Bash": ToolCompactionRule(
                strategy="truncate", head_lines=3, tail_lines=2, max_lines=10,
            )
        },
    )
    content = "\n".join(f"line {i}" for i in range(20))
    out = compact_tool_result(tool_name="Bash", content=content, config=cfg)
    assert "line 0" in out
    assert "line 2" in out
    assert "line 19" in out  # tail
    assert "line 18" in out
    # Middle is gone.
    assert "line 10" not in out
    # Sentinel is present and counts the omitted block.
    assert "omitted 15 lines (truncate)" in out


def test_truncate_zero_tail_keeps_only_head():
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={
            "Bash": ToolCompactionRule(
                strategy="truncate", head_lines=5, tail_lines=0, max_lines=5,
            )
        },
    )
    content = "\n".join(f"line {i}" for i in range(20))
    out = compact_tool_result(tool_name="Bash", content=content, config=cfg)
    assert "line 4" in out
    assert "line 19" not in out
    assert "omitted 15 lines" in out


# ─── summary strategy ─────────────────────────────────────────────────


def test_summary_preserves_error_lines():
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={
            "Bash": ToolCompactionRule(
                strategy="summary",
                head_lines=2,
                tail_lines=2,
                max_lines=5,
            ),
        },
    )
    content = "\n".join([
        "line 0",
        "line 1",
        "line 2",
        "ERROR: something blew up",  # signal
        "line 4",
        "line 5",
        "line 6",
        "line 7",
        "line 8",
        "WARNING: unrelated",       # signal
        "line 10",
        "line 11",
    ])
    out = compact_tool_result(tool_name="Bash", content=content, config=cfg)
    assert "ERROR: something blew up" in out
    assert "WARNING: unrelated" in out
    # Head + tail still present.
    assert "line 0" in out
    assert "line 11" in out


def test_summary_caps_signal_lines_at_proportional_count():
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={
            "Bash": ToolCompactionRule(
                strategy="summary",
                head_lines=2,
                tail_lines=2,
                max_lines=10,
                signal_patterns=(r"\bWARN\b",),
            ),
        },
    )
    # Many WARN lines; signal cap = max(head, tail) = 2.
    body = ["line A", "line B"] + ["WARN: x"] * 50 + ["last 1", "last 2"]
    out = compact_tool_result(
        tool_name="Bash", content="\n".join(body), config=cfg,
    )
    # Cap message present.
    assert "showing first 2 of 50 signal lines" in out
    # The kept lines are present; the dropped count is correct.
    assert out.count("WARN: x") <= 2 + 0  # WARN lines kept (cap)
    assert "dropped 48" in out


def test_summary_with_no_signal_lines_still_truncates():
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={
            "Bash": ToolCompactionRule(
                strategy="summary",
                head_lines=2,
                tail_lines=2,
                max_lines=5,
                signal_patterns=(r"\bnot-going-to-match-anything\b",),
            ),
        },
    )
    content = _big(50)
    out = compact_tool_result(tool_name="Bash", content=content, config=cfg)
    assert "line 0" in out
    assert "line 49" in out
    assert "omitted 46 lines" in out
    assert "signal lines from omitted region" not in out


# ─── hard ceiling ─────────────────────────────────────────────────────


def test_hard_ceiling_caps_pathological_output():
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={
            "Bash": ToolCompactionRule(
                strategy="truncate",
                head_lines=5,
                tail_lines=5,
                max_lines=10,
                max_chars=200,
            ),
        },
    )
    huge = "x" * 10_000
    out = compact_tool_result(tool_name="Bash", content=huge, config=cfg)
    assert len(out) <= 600  # head + sentinel + tail bounds the size
    assert "max_chars cap" in out


def test_hard_ceiling_applies_even_when_strategy_none():
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={"Bash": ToolCompactionRule(strategy="none", max_chars=100)},
    )
    huge = "x" * 5_000
    out = compact_tool_result(tool_name="Bash", content=huge, config=cfg)
    assert len(out) < 1_000
    assert "max_chars cap" in out


# ─── defensive: never break the loop ──────────────────────────────────


def test_compaction_crash_returns_original(caplog):
    """A buggy regex shouldn't crash the agent loop — return original instead."""
    bad_pattern = "(unclosed"  # invalid regex
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={
            "Bash": ToolCompactionRule(
                strategy="summary",
                head_lines=1,
                tail_lines=1,
                max_lines=2,
                signal_patterns=(bad_pattern,),
            ),
        },
    )
    content = _big(50)
    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.tokenjuice"):
        out = compact_tool_result(tool_name="Bash", content=content, config=cfg)
    assert out == content
    assert any("compaction crashed" in r.message for r in caplog.records)


def test_non_string_content_passed_through():
    cfg = TokenjuiceConfig(
        enabled=True,
        per_tool={"Bash": ToolCompactionRule(strategy="truncate", max_lines=2)},
    )
    obj = {"not": "a string"}
    assert compact_tool_result(tool_name="Bash", content=obj, config=cfg) == obj  # type: ignore[arg-type]


# ─── default config ───────────────────────────────────────────────────


def test_default_config_is_disabled():
    cfg = TokenjuiceConfig()
    assert cfg.enabled is False
    big = _big(5_000)
    assert compact_tool_result(tool_name="Bash", content=big, config=cfg) == big


def test_default_do_not_compact_includes_read_class():
    for name in ("Read", "ReadFile", "NotebookRead", "Skill"):
        assert name in DEFAULT_DO_NOT_COMPACT


def test_rule_for_returns_default_when_no_per_tool_rule():
    cfg = TokenjuiceConfig(
        enabled=True,
        default_rule=ToolCompactionRule(strategy="truncate", max_lines=5),
    )
    rule = cfg.rule_for("UnknownTool")
    assert rule is not None
    assert rule.strategy == "truncate"


def test_rule_for_per_tool_overrides_default():
    cfg = TokenjuiceConfig(
        enabled=True,
        default_rule=ToolCompactionRule(strategy="summary"),
        per_tool={"Bash": ToolCompactionRule(strategy="truncate")},
    )
    bash = cfg.rule_for("Bash")
    other = cfg.rule_for("WebFetch")
    assert bash is not None and bash.strategy == "truncate"
    assert other is not None and other.strategy == "summary"


def test_rule_for_returns_none_for_protected_tool():
    cfg = TokenjuiceConfig(enabled=True)
    assert cfg.rule_for("Read") is None


def test_rule_for_returns_none_when_disabled():
    cfg = TokenjuiceConfig(enabled=False)
    assert cfg.rule_for("Bash") is None
