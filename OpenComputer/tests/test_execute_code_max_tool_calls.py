"""G5 — code_execution.max_tool_calls override (Hermes Doc-2 parity)."""

from __future__ import annotations


def test_max_tool_calls_default_is_50():
    """Default cap stays at Hermes-spec 50."""
    from opencomputer.tools.ptc import _MAX_RPC_CALLS

    assert _MAX_RPC_CALLS == 50


def test_build_prologue_default_uses_50():
    """_build_prologue() with no override → in-script cap is 50."""
    from opencomputer.tools.ptc import _build_prologue

    prologue = _build_prologue(("Read",))
    assert "_ptc_max_calls = 50" in prologue


def test_build_prologue_honours_override():
    """_build_prologue(max_tool_calls=7) → in-script cap is 7."""
    from opencomputer.tools.ptc import _build_prologue

    prologue = _build_prologue(("Read",), max_tool_calls=7)
    assert "_ptc_max_calls = 7" in prologue
    # And the default value must NOT also appear:
    assert "_ptc_max_calls = 50" not in prologue


def test_code_execution_config_default_max_tool_calls():
    """CodeExecutionConfig defaults match Hermes spec (300s, 50 calls)."""
    from opencomputer.agent.config import CodeExecutionConfig

    cfg = CodeExecutionConfig()
    assert cfg.max_tool_calls == 50
    assert cfg.timeout_seconds == 300.0


def test_code_execution_config_override():
    from opencomputer.agent.config import CodeExecutionConfig

    cfg = CodeExecutionConfig(max_tool_calls=10)
    assert cfg.max_tool_calls == 10


def test_default_config_exposes_code_execution():
    """``Config.code_execution`` slot exists with sensible defaults."""
    from opencomputer.agent.config import CodeExecutionConfig, default_config

    cfg = default_config()
    assert isinstance(cfg.code_execution, CodeExecutionConfig)
    assert cfg.code_execution.max_tool_calls == 50
