"""Round 2B P-16 — security hardening (MCP internal-tool gating + .env BOM/ACL).

Covers the five required test cases:

* (a) BOM stripped correctly
* (b) loose perms refused
* (c) override allows with warning
* (d) MCP listing hides internal tools
* (e) MCP listing shows public tools

Plus a few edge cases to keep the redaction-pattern set honest (the
new patterns are shared with P-4 so other call-sites depend on them
being precise).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── env_loader (sub-item b) ─────────────────────────────────────


def _make_env_file(path: Path, contents: str, mode: int = 0o600) -> Path:
    path.write_text(contents, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_a_bom_stripped_correctly(tmp_path: Path) -> None:
    """Sub-item (b)/(a): a UTF-8 BOM at the start of a .env file is
    transparently removed so the first key isn't shadowed by an
    invisible prefix."""
    from opencomputer.security.env_loader import load_env_file

    contents = "﻿FOO=bar\nBAZ=qux\n"
    env_file = _make_env_file(tmp_path / ".env", contents)
    parsed = load_env_file(env_file)
    assert parsed == {"FOO": "bar", "BAZ": "qux"}
    # No stray "﻿FOO" key (would happen if BOM weren't stripped).
    assert all(not k.startswith("﻿") for k in parsed)


def test_a_no_bom_still_parses(tmp_path: Path) -> None:
    """Files without a BOM go through the same path unchanged."""
    from opencomputer.security.env_loader import load_env_file

    env_file = _make_env_file(tmp_path / ".env", "ALPHA=1\nBETA=2\n")
    assert load_env_file(env_file) == {"ALPHA": "1", "BETA": "2"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics required")
def test_b_loose_perms_refused(tmp_path: Path) -> None:
    """Sub-item (b): a world-readable .env (mode 0644) is refused
    with a typed LoosePermissionError carrying the actual mode."""
    from opencomputer.security.env_loader import LoosePermissionError, load_env_file

    env_file = _make_env_file(tmp_path / ".env", "SECRET=hunter2\n", mode=0o644)
    with pytest.raises(LoosePermissionError) as excinfo:
        load_env_file(env_file)
    assert excinfo.value.path == env_file
    assert excinfo.value.mode == 0o644
    # Exception message has actionable guidance.
    assert "chmod 600" in str(excinfo.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics required")
def test_b_group_only_perms_refused(tmp_path: Path) -> None:
    """Group-readable but other-private (mode 0640) is still refused —
    the mask covers any non-zero bit in 0o077."""
    from opencomputer.security.env_loader import LoosePermissionError, load_env_file

    env_file = _make_env_file(tmp_path / ".env", "X=y\n", mode=0o640)
    with pytest.raises(LoosePermissionError):
        load_env_file(env_file)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics required")
def test_b_strict_perms_load(tmp_path: Path) -> None:
    """Mode 0600 is the happy path."""
    from opencomputer.security.env_loader import load_env_file

    env_file = _make_env_file(tmp_path / ".env", "TOKEN=abc\n", mode=0o600)
    assert load_env_file(env_file) == {"TOKEN": "abc"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics required")
def test_c_override_allows_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Sub-item (b): explicit allow_loose_perms=True loads the file
    AND emits a WARNING log entry. The warning message includes the
    path and a chmod-fix hint."""
    from opencomputer.security.env_loader import load_env_file

    env_file = _make_env_file(tmp_path / ".env", "API=xx\n", mode=0o644)
    with caplog.at_level(logging.WARNING, logger="opencomputer.security.env_loader"):
        result = load_env_file(env_file, allow_loose_perms=True)
    assert result == {"API": "xx"}
    # WARNING records emitted, not silently swallowed.
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("loose-env-perms" in r.message for r in warning_records)
    assert any(str(env_file) in r.message for r in warning_records)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics required")
def test_c_process_flag_overrides_default(tmp_path: Path) -> None:
    """The process-wide flag set by the CLI propagates: callers that
    pass ``allow_loose_perms=None`` (default) inherit the global
    posture."""
    from opencomputer.security.env_loader import (
        load_env_file,
        set_process_allow_loose_perms,
    )

    env_file = _make_env_file(tmp_path / ".env", "Z=1\n", mode=0o644)
    # Default (no flag) refuses.
    with pytest.raises(PermissionError):
        load_env_file(env_file)
    set_process_allow_loose_perms(True)
    try:
        assert load_env_file(env_file) == {"Z": "1"}
    finally:
        set_process_allow_loose_perms(False)


def test_b_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    """Probing a missing file returns ``{}`` — callers don't need
    try/except just to check whether a profile has a .env yet."""
    from opencomputer.security.env_loader import load_env_file

    assert load_env_file(tmp_path / "does-not-exist.env") == {}


def test_env_parser_handles_export_quotes_comments(tmp_path: Path) -> None:
    """Spot-check the minimal parser. Not aiming for full python-dotenv
    parity — only the syntax the project actually emits."""
    from opencomputer.security.env_loader import load_env_file

    contents = (
        "# leading comment\n"
        "\n"
        "export FOO=bar\n"
        'WITH_QUOTES="hello world"\n'
        "WITH_SQUOTES='hi'\n"
        "EMPTY=\n"
        "= junk\n"  # missing key — skipped
    )
    env_file = _make_env_file(tmp_path / ".env", contents)
    assert load_env_file(env_file) == {
        "FOO": "bar",
        "WITH_QUOTES": "hello world",
        "WITH_SQUOTES": "hi",
        "EMPTY": "",
    }


def test_cli_flag_strips_and_sets_process_state() -> None:
    """The CLI handler removes ``--allow-loose-env-perms`` from argv
    AND flips the module-level state. Without this, every Typer
    subcommand would have to declare the option."""
    from opencomputer.cli import _apply_loose_env_perms_flag
    from opencomputer.security.env_loader import (
        get_process_allow_loose_perms,
        set_process_allow_loose_perms,
    )

    original_argv = sys.argv[:]
    set_process_allow_loose_perms(False)
    try:
        sys.argv = ["opencomputer", "chat", "--allow-loose-env-perms"]
        _apply_loose_env_perms_flag()
        assert get_process_allow_loose_perms() is True
        assert "--allow-loose-env-perms" not in sys.argv
        assert sys.argv == ["opencomputer", "chat"]
    finally:
        sys.argv = original_argv
        set_process_allow_loose_perms(False)


def test_cli_flag_absent_leaves_state_alone() -> None:
    """If the flag isn't present, argv is unchanged and the override
    stays at its current value."""
    from opencomputer.cli import _apply_loose_env_perms_flag
    from opencomputer.security.env_loader import (
        get_process_allow_loose_perms,
        set_process_allow_loose_perms,
    )

    original_argv = sys.argv[:]
    set_process_allow_loose_perms(False)
    try:
        sys.argv = ["opencomputer", "chat"]
        _apply_loose_env_perms_flag()
        assert get_process_allow_loose_perms() is False
        assert sys.argv == ["opencomputer", "chat"]
    finally:
        sys.argv = original_argv


# ─── MCP internal-tool gating (sub-item a) ───────────────────────


def _fake_mcp_tool(
    name: str,
    *,
    meta: dict | None = None,
    annotations: dict | None = None,
) -> SimpleNamespace:
    """Build a minimal stand-in for an :class:`mcp.types.Tool` object
    with just the attributes the filter inspects."""
    annotations_obj: object = None
    if annotations is not None:
        # Mimic the pydantic ToolAnnotations API: model_dump() returns the
        # extras dict. We deliberately don't use ToolAnnotations directly so
        # the test stays decoupled from the upstream schema layout.
        annotations_obj = SimpleNamespace(model_dump=lambda: annotations)
    return SimpleNamespace(
        name=name,
        description=f"{name} desc",
        inputSchema={"type": "object", "properties": {}},
        meta=meta,
        annotations=annotations_obj,
    )


def test_d_mcp_listing_hides_internal_via_meta() -> None:
    """Sub-item (a): a tool with ``_meta.internal = true`` does not
    appear on the agent-visible tool list."""
    from opencomputer.mcp.client import _tool_is_internal

    tool = _fake_mcp_tool("ping", meta={"internal": True})
    assert _tool_is_internal(tool) is True


def test_d_mcp_listing_hides_owner_system_via_meta() -> None:
    """Sub-item (a): a tool with ``_meta.owner = "system"`` is hidden too."""
    from opencomputer.mcp.client import _tool_is_internal

    tool = _fake_mcp_tool("admin_ops", meta={"owner": "system"})
    assert _tool_is_internal(tool) is True


def test_d_mcp_listing_hides_via_annotations_extras() -> None:
    """Sub-item (a): tools that stash the flag on ``annotations``
    (extra='allow' on ToolAnnotations) are also filtered."""
    from opencomputer.mcp.client import _tool_is_internal

    tool = _fake_mcp_tool("control", annotations={"internal": True})
    assert _tool_is_internal(tool) is True


def test_e_mcp_listing_shows_public_tools() -> None:
    """Sub-item (a): default tools with no flags are exposed unchanged
    (the new gating is OFF by default)."""
    from opencomputer.mcp.client import _tool_is_internal

    plain = _fake_mcp_tool("get_weather")
    assert _tool_is_internal(plain) is False
    explicit_user = _fake_mcp_tool("get_weather", meta={"owner": "user"})
    assert _tool_is_internal(explicit_user) is False
    explicit_false = _fake_mcp_tool("get_weather", meta={"internal": False})
    assert _tool_is_internal(explicit_false) is False


def test_e_mcp_listing_handles_missing_carriers() -> None:
    """Tools that have no ``_meta`` and no ``annotations`` (older MCP
    server build) don't crash the filter."""
    from opencomputer.mcp.client import _tool_is_internal

    bare = SimpleNamespace(name="legacy")
    assert _tool_is_internal(bare) is False


@pytest.mark.asyncio
async def test_d_e_connect_filters_internal_tools_from_registry() -> None:
    """End-to-end: a server returning [public, internal, owner=system]
    only registers the public tool. Mirrors the hot-path agent
    discovery flow rather than just the helper."""
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPConnection

    cfg = MCPServerConfig(name="srv", transport="stdio", command="echo", args=("hi",))
    conn = MCPConnection(config=cfg)

    public = _fake_mcp_tool("public_tool")
    hidden_internal = _fake_mcp_tool("hidden_internal", meta={"internal": True})
    hidden_owner = _fake_mcp_tool("hidden_admin", meta={"owner": "system"})

    fake_session = MagicMock()
    fake_session.initialize = AsyncMock(
        return_value=SimpleNamespace(serverInfo=SimpleNamespace(version="1.0"))
    )
    fake_session.list_tools = AsyncMock(
        return_value=SimpleNamespace(tools=[public, hidden_internal, hidden_owner])
    )

    # Patch the bits that would otherwise spawn a real subprocess.
    with patch("opencomputer.mcp.client.stdio_client") as stdio_ctx, patch(
        "opencomputer.mcp.client.ClientSession"
    ) as session_cls:
        stdio_ctx_instance = AsyncMock()
        stdio_ctx_instance.__aenter__.return_value = (MagicMock(), MagicMock())
        stdio_ctx_instance.__aexit__.return_value = None
        stdio_ctx.return_value = stdio_ctx_instance
        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = fake_session
        session_cm.__aexit__.return_value = None
        session_cls.return_value = session_cm
        ok = await conn.connect(osv_check_enabled=False)
    assert ok
    tool_names = [t.tool_name for t in conn.tools]
    assert tool_names == ["public_tool"]
    assert "hidden_internal" not in tool_names
    assert "hidden_admin" not in tool_names
    await conn.disconnect()


# ─── shared redaction patterns (used by P-4 too) ────────────────


def test_redact_anthropic_key_label() -> None:
    """``sk-ant-XXXX`` is replaced with the anthropic-specific label,
    not the generic OpenAI label."""
    from opencomputer.evolution.redaction import redact

    redacted, counts = redact("key=sk-ant-abcdefghij123456")
    assert "<ANTHROPIC_KEY_REDACTED>" in redacted
    assert counts["anthropic_key"] == 1
    assert counts["openai_key"] == 0


def test_redact_openai_key() -> None:
    from opencomputer.evolution.redaction import redact

    redacted, counts = redact("key=sk-abcdefghij1234567890XYZ")
    assert "<OPENAI_KEY_REDACTED>" in redacted
    assert counts["openai_key"] == 1


def test_redact_slack_tokens() -> None:
    from opencomputer.evolution.redaction import redact

    redacted, counts = redact(
        "bot=xoxb-12345abcdef67890ghijk-extra personal=xoxp-9876543210ZYXWVUTSRQ"
    )
    assert "<SLACK_TOKEN_REDACTED>" in redacted
    assert counts["slack_token"] == 2


def test_redact_telegram_bot_token() -> None:
    from opencomputer.evolution.redaction import redact

    text = "TELEGRAM_BOT_TOKEN=123456789:abcdefghijABCDEFGHIJ_-1234567890"
    redacted, counts = redact(text)
    assert "<TELEGRAM_TOKEN_REDACTED>" in redacted
    assert counts["telegram_token"] == 1


def test_redact_aws_access_key_id() -> None:
    from opencomputer.evolution.redaction import redact

    redacted, counts = redact("aws_access_key_id=AKIAIOSFODNN7EXAMPLE")
    assert "<AWS_AKID_REDACTED>" in redacted
    assert counts["aws_akid"] == 1


def test_redact_bearer_token_uses_widened_alphabet() -> None:
    """The widened bearer pattern matches dotted JWT-style tokens —
    without breaking the existing behaviour for plain hex bearers."""
    from opencomputer.evolution.redaction import redact

    text = (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dotsig "
        "fallback Bearer 1234567890abcdef"
    )
    redacted, counts = redact(text)
    assert counts["bearer_token"] == 2
    assert "Bearer <REDACTED>" in redacted


def test_redact_no_false_positive_on_ordinary_text() -> None:
    """Words like ``sky`` or ``Bearer scheme`` (no token after) don't
    trip the pattern set."""
    from opencomputer.evolution.redaction import redact

    redacted, counts = redact("the sky is blue and the bearer scheme is fine")
    assert counts["openai_key"] == 0
    assert counts["bearer_token"] == 0
    assert "<" not in redacted
