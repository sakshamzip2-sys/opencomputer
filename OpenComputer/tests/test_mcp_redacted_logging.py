"""Gap E — redacted error logging for MCP code paths.

mcp-openclaw-port follow-up. OC has a strong runtime-redaction sweep
(``opencomputer.security.redact.redact_runtime_text``) that strips
GitHub PATs, OpenAI keys, bearer tokens, postgres URLs, etc. The
LLM-facing path (``MCPTool.execute``) already pipes errors through
it.

But MCP CODE PATH logger.* calls historically passed raw exception
text (URLs, headers, auth params) straight to the log file. A malicious
or sloppy MCP server can dump credentials into the OC log via stderr
or by including them in error messages. Gap E adds:

* :func:`redact_mcp_log_text` — convenience wrapper around the
  central redact module + an extra pass for MCP-shaped URL/header
  content that the central module doesn't recognise (e.g. query
  strings on MCP HTTP URLs, ``Authorization: Bearer`` slips).
* A pattern check that the helper actually strips the targets.
"""

from __future__ import annotations

from opencomputer.mcp.redaction import redact_mcp_log_text


def test_redacts_bearer_token() -> None:
    raw = "MCP error: Authorization: Bearer abc123secret failed"
    out = redact_mcp_log_text(raw)
    assert "abc123secret" not in out
    assert "Bearer" in out  # the structural marker survives


def test_redacts_url_with_token_query() -> None:
    raw = "GET https://api.example.com/data?token=supersecret123&user=x"
    out = redact_mcp_log_text(raw)
    assert "supersecret123" not in out
    # The host stays so the log is still useful
    assert "api.example.com" in out


def test_redacts_url_with_api_key_query() -> None:
    raw = "fetch failed: https://api.example.com/v1?api_key=skLeakedKey"
    out = redact_mcp_log_text(raw)
    assert "skLeakedKey" not in out


def test_redacts_x_api_key_header() -> None:
    raw = "MCP error: x-api-key=sk-leaky-token-xxx returned 401"
    out = redact_mcp_log_text(raw)
    assert "sk-leaky-token-xxx" not in out


def test_passes_through_no_secret_text() -> None:
    raw = "MCP server 'memory' connect raised: ConnectionRefusedError"
    out = redact_mcp_log_text(raw)
    # Idempotent + structure-preserving when there's nothing to strip
    assert out == raw


def test_handles_empty_string() -> None:
    assert redact_mcp_log_text("") == ""


def test_redacts_github_pat() -> None:
    raw = "MCP error: ghp_abc123def456ghi789jkl012mno345pqr678stu in headers"
    out = redact_mcp_log_text(raw)
    assert "ghp_abc123def456ghi789jkl012mno345pqr678stu" not in out


def test_idempotent() -> None:
    raw = "https://api.example.com/data?token=secret123abc"
    once = redact_mcp_log_text(raw)
    twice = redact_mcp_log_text(once)
    assert once == twice
