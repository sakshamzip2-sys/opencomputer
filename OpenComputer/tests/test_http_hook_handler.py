"""CC §6 — HTTP hook handler.

Spec: docs/OC-FROM-CLAUDE-CODE.md §6. Companion to shell + prompt +
agent hook handlers. POSTs the HookContext to a user URL; endpoint
replies with the same JSON decision shape shell hooks use on stdout.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from opencomputer.agent.config import HookHttpConfig
from opencomputer.hooks.http_handlers import make_http_hook_handler
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent

# ─── Test HTTP server scaffolding ─────────────────────────────────────────


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Test server that returns whatever the test fixture configured."""

    # Per-instance: set by the fixture via the class attribute.
    _SCRIPT: dict = {}
    received: list = []  # mutated by handlers

    def log_message(self, fmt, *args):  # quiet test output
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length) if length else b""
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError:
            decoded = body.decode("utf-8", errors="replace")
        self.__class__.received.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": decoded,
            }
        )
        status = self._SCRIPT.get("status", 200)
        response_body = self._SCRIPT.get("body", b"")
        if isinstance(response_body, dict):
            response_body = json.dumps(response_body).encode("utf-8")
        elif isinstance(response_body, str):
            response_body = response_body.encode("utf-8")
        delay_s = self._SCRIPT.get("delay_s", 0)
        if delay_s:
            import time as _time
            _time.sleep(delay_s)
        self.send_response(status)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)


@pytest.fixture
def http_server() -> Iterator[tuple[str, _ScriptedHandler]]:
    """Start a localhost HTTP server on a free port. Yields
    ``(base_url, handler_class)``. Tests mutate
    ``_ScriptedHandler._SCRIPT`` to control the next response."""
    _ScriptedHandler.received = []
    _ScriptedHandler._SCRIPT = {"status": 200, "body": {}}
    server = HTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", _ScriptedHandler
    finally:
        server.shutdown()
        server.server_close()


def _ctx() -> HookContext:
    return HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="sess-1",
        tool_call=ToolCall(id="c1", name="Bash", arguments={"command": "ls -la"}),
    )


# ─── Decision shapes ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_action_block_decision(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {
        "status": 200,
        "body": {"action": "block", "message": "no rm -rf"},
    }
    h = make_http_hook_handler(
        HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook", timeout_seconds=2.0)
    )
    decision = await h(_ctx())
    assert decision is not None
    assert decision.decision == "block"
    assert "no rm -rf" in decision.reason


@pytest.mark.asyncio
async def test_decision_block_alternate_shape(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {
        "status": 200,
        "body": {"decision": "block", "reason": "policy violation"},
    }
    h = make_http_hook_handler(
        HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook", timeout_seconds=2.0)
    )
    decision = await h(_ctx())
    assert decision and decision.decision == "block"
    assert "policy violation" in decision.reason


@pytest.mark.asyncio
async def test_action_approve_is_pass(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": {"action": "approve"}}
    h = make_http_hook_handler(
        HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook")
    )
    decision = await h(_ctx())
    assert decision is not None
    assert decision.decision == "pass"


@pytest.mark.asyncio
async def test_action_allow_is_pass(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": {"action": "allow"}}
    h = make_http_hook_handler(HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook"))
    decision = await h(_ctx())
    assert decision and decision.decision == "pass"


@pytest.mark.asyncio
async def test_empty_response_means_pass(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": b""}
    h = make_http_hook_handler(HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook"))
    assert (await h(_ctx())) is None


@pytest.mark.asyncio
async def test_unrecognized_keys_mean_pass(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": {"foo": "bar", "verdict": "yes"}}
    h = make_http_hook_handler(HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook"))
    assert (await h(_ctx())) is None


@pytest.mark.asyncio
async def test_malformed_json_means_pass(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": b"not-json{{"}
    h = make_http_hook_handler(HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook"))
    assert (await h(_ctx())) is None


# ─── Fail-open paths ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_2xx_means_pass(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 500, "body": b"oops"}
    h = make_http_hook_handler(HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook"))
    assert (await h(_ctx())) is None


@pytest.mark.asyncio
async def test_404_means_pass(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 404, "body": b""}
    h = make_http_hook_handler(HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook"))
    assert (await h(_ctx())) is None


@pytest.mark.asyncio
async def test_unreachable_url_means_pass():
    """Network error must fail-open, not raise."""
    h = make_http_hook_handler(
        HookHttpConfig(
            event="PreToolUse",
            url="http://127.0.0.1:1/nope",  # closed port
            timeout_seconds=0.5,
        )
    )
    assert (await h(_ctx())) is None


@pytest.mark.asyncio
async def test_timeout_means_pass(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": {"action": "block"}, "delay_s": 0.5}
    h = make_http_hook_handler(
        HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook", timeout_seconds=0.1)
    )
    assert (await h(_ctx())) is None


@pytest.mark.asyncio
async def test_oversized_response_means_pass(http_server):
    base_url, handler = http_server
    big_payload = ("x" * 200_000).encode("utf-8")
    handler._SCRIPT = {"status": 200, "body": big_payload}
    h = make_http_hook_handler(
        HookHttpConfig(
            event="PreToolUse",
            url=f"{base_url}/hook",
            max_response_bytes=1024,
        )
    )
    assert (await h(_ctx())) is None


# ─── Wire details ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_posts_context_as_json(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": {"action": "allow"}}
    h = make_http_hook_handler(
        HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook")
    )
    await h(_ctx())
    assert len(handler.received) == 1
    body = handler.received[0]["body"]
    assert isinstance(body, dict)
    # tool_call may be a dict (asdict) or repr'd — assert via body shape
    tool_call_field = body.get("tool_call")
    assert tool_call_field is not None
    if isinstance(tool_call_field, dict):
        assert tool_call_field.get("name") == "Bash"
    else:
        assert "Bash" in str(tool_call_field)
    assert body.get("session_id") == "sess-1"


@pytest.mark.asyncio
async def test_static_headers_forwarded(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": {"action": "allow"}}
    h = make_http_hook_handler(
        HookHttpConfig(
            event="PreToolUse",
            url=f"{base_url}/hook",
            headers=(("X-Test-Tag", "abc"), ("Authorization", "Bearer xyz")),
        )
    )
    await h(_ctx())
    headers = handler.received[0]["headers"]
    # Header keys come back capitalized by BaseHTTPServer.
    matched = {k.lower(): v for k, v in headers.items()}
    assert matched.get("x-test-tag") == "abc"
    assert matched.get("authorization") == "Bearer xyz"


@pytest.mark.asyncio
async def test_env_var_substitution_in_header_values(monkeypatch, http_server):
    monkeypatch.setenv("MY_TOKEN", "secret-123")
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": {"action": "allow"}}
    h = make_http_hook_handler(
        HookHttpConfig(
            event="PreToolUse",
            url=f"{base_url}/hook",
            headers=(("Authorization", "Bearer ${MY_TOKEN}"),),
        )
    )
    await h(_ctx())
    auth = next(
        v for k, v in handler.received[0]["headers"].items() if k.lower() == "authorization"
    )
    assert auth == "Bearer secret-123"


@pytest.mark.asyncio
async def test_user_agent_default(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": {"action": "allow"}}
    h = make_http_hook_handler(HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook"))
    await h(_ctx())
    ua = next(
        v for k, v in handler.received[0]["headers"].items() if k.lower() == "user-agent"
    )
    assert "OpenComputer" in ua


@pytest.mark.asyncio
async def test_empty_url_raises_at_factory():
    """A misconfigured hook should fail FAST at registration time, not
    at the first event fire."""
    with pytest.raises(ValueError):
        make_http_hook_handler(HookHttpConfig(event="PreToolUse", url=""))


@pytest.mark.asyncio
async def test_content_type_is_application_json(http_server):
    base_url, handler = http_server
    handler._SCRIPT = {"status": 200, "body": {"action": "allow"}}
    h = make_http_hook_handler(HookHttpConfig(event="PreToolUse", url=f"{base_url}/hook"))
    await h(_ctx())
    ct = next(
        v for k, v in handler.received[0]["headers"].items() if k.lower() == "content-type"
    )
    assert "application/json" in ct
