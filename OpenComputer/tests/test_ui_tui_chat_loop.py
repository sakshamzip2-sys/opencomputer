"""Conversation-loop integration test — the TUI fixes that close the
functional defects a brutal audit surfaced.

Five defects were fixed in app.tsx and this proves each, by driving the
real <App> against a SCRIPTED mock wire server that emits a full turn:

  #1 tool.result   — tool output used to be invisible
  #2 permission.request — a consent prompt used to hang the turn forever
  #3 stream.retry  — retries used to be a silent frozen spinner
  #4 scrollback    — older messages past 25 used to be unreachable
  #5 input history — up-arrow used to do nothing

The mock scripts: turn.begin → 30× tool.call → tool.result →
permission.request (pauses for the response) → stream.retry →
assistant.message → turn.end. The harness drives a message through it,
answers the prompt, scrolls back, and recalls history.

Skips when the Node toolchain / built bundle is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHAT_BUNDLE = _REPO / "opencomputer" / "ui-tui" / "dist" / "chatHarness.js"


def _find_node() -> str | None:
    hit = shutil.which("node")
    if hit:
        return hit
    for base in sorted(
        (Path.home() / ".nvm" / "versions" / "node").glob("v*"), reverse=True
    ):
        cand = base / "bin" / "node"
        if cand.exists():
            return str(cand)
    return None


_NODE = _find_node()

pytestmark = pytest.mark.skipif(
    _NODE is None or not _CHAT_BUNDLE.is_file(),
    reason="Node toolchain or built chat-harness bundle unavailable",
)


async def _mock_wire_handler(ws) -> None:  # noqa: ANN001 — websockets server proto
    """A scripted wire server: one canned turn covering every event type."""

    async def res(mid: str, payload: dict) -> None:
        await ws.send(
            json.dumps({"type": "res", "id": mid, "ok": True, "payload": payload})
        )

    async def ev(name: str, payload: dict) -> None:
        await ws.send(json.dumps({"type": "event", "event": name, "payload": payload}))

    async for raw in ws:
        msg = json.loads(raw)
        method = msg.get("method")
        mid = msg.get("id", "")
        if method == "hello":
            await res(
                mid,
                {
                    "server": "opencomputer",
                    "version": "mock",
                    "methods": ["m"] * 27,
                    "events": ["turn.end"],
                },
            )
        elif method == "slash.list":
            await res(mid, {"commands": []})
        elif method == "chat":
            await res(
                mid,
                {
                    "final_message": "",
                    "session_id": "s1",
                    "iterations": 1,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )
            await ev("turn.begin", {"request_id": "r1"})
            await ev("assistant.message", {"delta": "Running steps.\n\n"})
            # 30 tool calls → >25 turns → scrollback (#4) becomes reachable.
            for i in range(30):
                await ev("tool.call", {"name": f"step-{i}"})
            # #1 — tool output, previously invisible.
            await ev(
                "tool.result",
                {"content": "hello from the tool", "is_error": False},
            )
            # #2 — consent prompt; the rest of the turn waits for the answer.
            await ev(
                "permission.request",
                {
                    "request_id": "rq1",
                    "session_id": "s1",
                    "capability_id": "shell.exec",
                    "scope": "once",
                    "context": "rm -rf /tmp/demo",
                },
            )
        elif method == "permission.response":
            await res(mid, {"request_id": "rq1", "resolved": True})
            # #3 — retry banner, previously a silent frozen spinner.
            await ev(
                "stream.retry",
                {
                    "error_kind": "overloaded",
                    "attempt": 1,
                    "next_attempt": 2,
                    "max_attempts": 4,
                    "delay_seconds": 1.0,
                    "exhausted": False,
                },
            )
            await asyncio.sleep(0.9)
            await ev(
                "assistant.message",
                {"delta": "# Done\n\nThe **answer** is `42`."},
            )
            await ev("turn.end", {"request_id": "r1"})
        else:
            await res(mid, {})


@pytest.mark.asyncio
async def test_chat_loop_closes_all_functional_defects() -> None:
    import websockets

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    assert _NODE is not None  # guaranteed by pytestmark
    async with websockets.serve(_mock_wire_handler, "127.0.0.1", port):
        proc = await asyncio.create_subprocess_exec(
            _NODE,
            str(_CHAT_BUNDLE),
            f"ws://127.0.0.1:{port}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=35)

    text = out_b.decode()
    assert "<<END>>" in text, (
        f"chat harness did not finish.\nstdout={text!r}\nstderr={err_b.decode()!r}"
    )

    def section(name: str) -> str:
        return text.split(f"<<{name}>>", 1)[1].split("<<", 1)[0]

    mid = section("MID_TURN")
    retry = section("RETRY")
    after = section("AFTER")
    scrolled = section("SCROLLED")
    hist = section("HISTORY")

    # #1 — tool call AND tool result render in the transcript.
    assert "step-" in mid, f"tool.call turns missing:\n{mid}"
    assert "hello from the tool" in mid, f"tool.result not rendered (#1):\n{mid}"
    # #2 — the permission prompt appeared and paused the turn.
    assert "Permission needed" in mid, f"permission prompt missing (#2):\n{mid}"
    assert "shell.exec" in mid, f"permission capability missing:\n{mid}"
    # #3 — the retry banner showed (it was a silent frozen spinner before).
    assert "retry" in retry.lower(), f"stream.retry banner missing (#3):\n{retry}"
    # The turn completed: final markdown rendered, prompt + retry cleared.
    assert "answer" in after, f"final assistant message not rendered:\n{after}"
    assert "Permission needed" not in after, f"permission prompt stuck:\n{after}"
    # #4 — >25 turns, so PageUp genuinely scrolls (a "newer" marker appears).
    assert "newer" in scrolled, f"scrollback did not move (#4):\n{scrolled}"
    # #5 — up-arrow recalled the submitted input into the composer.
    assert "hello there" in hist, f"input history not recalled (#5):\n{hist}"
    assert "type a message" not in hist, (
        f"composer still empty — history recall did nothing (#5):\n{hist}"
    )
