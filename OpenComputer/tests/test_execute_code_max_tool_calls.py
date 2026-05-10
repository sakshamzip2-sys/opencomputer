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


def test_code_execution_yaml_round_trip(tmp_path, monkeypatch):
    """Top-level YAML `code_execution:` block parses into Config.code_execution.

    Lock-in test to confirm the generic _apply_overrides path covers the
    new dataclass — without this, a future refactor of the parser could
    silently break user-facing config.yaml support.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "code_execution:\n"
        "  max_tool_calls: 123\n"
        "  timeout_seconds: 600.5\n"
        "  terminal:\n"
        "    env_passthrough:\n"
        "      - MY_API_KEY\n"
        "      - OTHER_VAR\n"
    )

    from opencomputer.agent.config_store import load_config

    cfg = load_config(cfg_yaml)
    assert cfg.code_execution.max_tool_calls == 123
    assert cfg.code_execution.timeout_seconds == 600.5
    assert cfg.code_execution.terminal.get("env_passthrough") == [
        "MY_API_KEY",
        "OTHER_VAR",
    ]


def test_code_execution_yaml_partial_override_keeps_defaults(tmp_path):
    """Setting only one field leaves the others at their defaults."""
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text("code_execution:\n  max_tool_calls: 5\n")
    from opencomputer.agent.config_store import load_config

    cfg = load_config(cfg_yaml)
    assert cfg.code_execution.max_tool_calls == 5
    assert cfg.code_execution.timeout_seconds == 300.0
    assert cfg.code_execution.terminal == {}


# ─── P3 validation guards ────────────────────────────────────────


def test_code_execution_config_rejects_zero_max_tool_calls():
    """``max_tool_calls=0`` fails fast at construction (would brick exec)."""
    import pytest

    from opencomputer.agent.config import CodeExecutionConfig

    with pytest.raises(ValueError, match="max_tool_calls"):
        CodeExecutionConfig(max_tool_calls=0)


def test_code_execution_config_rejects_negative_max_tool_calls():
    import pytest

    from opencomputer.agent.config import CodeExecutionConfig

    with pytest.raises(ValueError, match="max_tool_calls"):
        CodeExecutionConfig(max_tool_calls=-1)


def test_code_execution_config_rejects_zero_timeout():
    import pytest

    from opencomputer.agent.config import CodeExecutionConfig

    with pytest.raises(ValueError, match="timeout_seconds"):
        CodeExecutionConfig(timeout_seconds=0.0)


def test_code_execution_config_rejects_negative_timeout():
    import pytest

    from opencomputer.agent.config import CodeExecutionConfig

    with pytest.raises(ValueError, match="timeout_seconds"):
        CodeExecutionConfig(timeout_seconds=-30.0)


def test_code_execution_yaml_invalid_max_tool_calls_raises_at_load(tmp_path):
    """A bad config.yaml fails loudly at load — not silently at first use."""
    import pytest

    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text("code_execution:\n  max_tool_calls: 0\n")
    from opencomputer.agent.config_store import load_config

    with pytest.raises(ValueError, match="max_tool_calls"):
        load_config(cfg_yaml)


# ─── P2 ExecuteCode reads config defaults ───────────────────────


def test_execute_code_reads_timeout_default_from_config(tmp_path, monkeypatch):
    """ExecuteCode uses config.code_execution.timeout_seconds when args has none."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "code_execution:\n"
        "  timeout_seconds: 47.5\n"
        "  max_tool_calls: 25\n"
    )
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.tools.execute_code import ExecuteCode
    from plugin_sdk.core import ToolCall

    tool = ExecuteCode()
    call = ToolCall(id="t1", name="ExecuteCode", arguments={"code": "print('hi')"})

    captured: dict = {}

    async def fake_run_ptc(_code, **kwargs):
        captured.update(kwargs)
        from opencomputer.tools.ptc import PTCResult
        return PTCResult(stdout="hi\n", stderr="", exit_code=0, duration_seconds=0.01)

    with patch("opencomputer.tools.execute_code.run_ptc", AsyncMock(side_effect=fake_run_ptc)):
        result = asyncio.run(tool.execute(call))

    assert not result.is_error, result.content
    assert captured["timeout_s"] == 47.5
    assert captured["max_tool_calls"] == 25


def test_execute_code_args_timeout_overrides_config(tmp_path, monkeypatch):
    """A per-call timeout_seconds beats the config default."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text("code_execution:\n  timeout_seconds: 100.0\n")
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.tools.execute_code import ExecuteCode
    from plugin_sdk.core import ToolCall

    tool = ExecuteCode()
    call = ToolCall(
        id="t1",
        name="ExecuteCode",
        arguments={"code": "print('hi')", "timeout_seconds": 7.5},
    )

    captured: dict = {}

    async def fake_run_ptc(_code, **kwargs):
        captured.update(kwargs)
        from opencomputer.tools.ptc import PTCResult
        return PTCResult(stdout="hi\n", stderr="", exit_code=0, duration_seconds=0.01)

    with patch("opencomputer.tools.execute_code.run_ptc", AsyncMock(side_effect=fake_run_ptc)):
        asyncio.run(tool.execute(call))

    assert captured["timeout_s"] == 7.5


def test_execute_code_rejects_zero_timeout_from_args():
    """Per-call timeout_seconds=0 → clean error, not silent infinite hang."""
    import asyncio

    from opencomputer.tools.execute_code import ExecuteCode
    from plugin_sdk.core import ToolCall

    tool = ExecuteCode()
    call = ToolCall(
        id="t1",
        name="ExecuteCode",
        arguments={"code": "print('hi')", "timeout_seconds": 0},
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "timeout_seconds" in result.content


def test_execute_code_rejects_non_numeric_timeout():
    import asyncio

    from opencomputer.tools.execute_code import ExecuteCode
    from plugin_sdk.core import ToolCall

    tool = ExecuteCode()
    call = ToolCall(
        id="t1",
        name="ExecuteCode",
        arguments={"code": "print('hi')", "timeout_seconds": "fast"},
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "timeout_seconds" in result.content
