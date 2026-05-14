"""Minimal LSP JSON-RPC client.

Just enough of the protocol to spawn a server, open one file, collect
``publishDiagnostics`` notifications, and shut down cleanly.

Why minimal: the agent-facing use case is "what's wrong with this file"
— a few-second one-shot per file, not an editor-style long-running
session. The full LSP capability surface (hover, completion, go-to-def)
is intentionally NOT in scope; build a separate tool when it's needed.

The protocol framing is the standard Content-Length header followed by
the JSON body, both over the server's stdin/stdout. See
https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#headerPart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum time we wait for the server to publish diagnostics before
# returning whatever we have. pyright is fast on small files; tsserver
# can take a beat on first-open. 6s is a comfortable budget that won't
# wedge the agent loop on a stuck server.
DIAGNOSTICS_WAIT_SECONDS = 6.0

# Hard timeout on the entire operation. If the server is so wedged we
# can't even send `initialize`, we kill it after this.
HARD_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class Diagnostic:
    """One LSP diagnostic, normalized to a small Python-friendly shape."""

    file: str
    line: int  # 1-based
    column: int  # 1-based
    severity: str  # 'error' | 'warning' | 'info' | 'hint'
    message: str
    code: str | None = None
    source: str | None = None


@dataclass(slots=True)
class LspResult:
    """Aggregate result from one LspClient.collect() call."""

    file: str
    server: str
    diagnostics: list[Diagnostic] = field(default_factory=list)
    error: str | None = None  # populated on subprocess failure


_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _frame(payload: dict[str, Any]) -> bytes:
    """Wrap a JSON payload in the LSP Content-Length header."""
    body = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


async def _read_message(
    stream: asyncio.StreamReader,
) -> dict[str, Any] | None:
    """Read one Content-Length-framed JSON message, or None on EOF."""
    header_lines: list[bytes] = []
    while True:
        line = await stream.readline()
        if not line:
            return None  # EOF
        if line in (b"\r\n", b"\n"):
            break
        header_lines.append(line)
    length = 0
    for raw in header_lines:
        try:
            key, _, val = raw.decode("ascii").rstrip().partition(":")
        except UnicodeDecodeError:
            continue
        if key.strip().lower() == "content-length":
            length = int(val.strip())
    if length <= 0:
        return None
    body = await stream.readexactly(length)
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("LSP message not JSON: %s", exc)
        return None


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


async def collect_diagnostics(
    server_executable: str,
    server_args: tuple[str, ...],
    server_name: str,
    file_path: str,
    *,
    diagnostics_wait: float = DIAGNOSTICS_WAIT_SECONDS,
    hard_timeout: float = HARD_TIMEOUT_SECONDS,
) -> LspResult:
    """Spawn ``server_executable``, open ``file_path``, collect diagnostics.

    Returns an :class:`LspResult` no matter what happens. Subprocess
    crashes, timeouts, missing files, malformed responses all surface as
    a populated ``error`` field with empty ``diagnostics`` — never raise.
    """
    file = Path(file_path)
    if not file.exists():
        return LspResult(
            file=file_path,
            server=server_name,
            error=f"file not found: {file_path}",
        )
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return LspResult(
            file=file_path,
            server=server_name,
            error=f"could not read {file_path}: {exc}",
        )

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                server_executable,
                *server_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            timeout=5.0,
        )
    except (TimeoutError, FileNotFoundError) as exc:
        return LspResult(
            file=file_path,
            server=server_name,
            error=f"could not start {server_executable}: {exc}",
        )

    assert proc.stdin is not None and proc.stdout is not None

    diagnostics: list[Diagnostic] = []

    async def _interact() -> None:
        # 1. initialize
        proc.stdin.write(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "processId": os.getpid(),
                        "rootUri": file.parent.as_uri(),
                        "capabilities": {
                            "textDocument": {
                                "publishDiagnostics": {
                                    "relatedInformation": False,
                                },
                            },
                        },
                    },
                }
            )
        )
        await proc.stdin.drain()
        await _read_message(proc.stdout)  # initialize response

        proc.stdin.write(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "method": "initialized",
                    "params": {},
                }
            )
        )
        await proc.stdin.drain()

        # 2. didOpen — server begins analysis
        ext = file.suffix
        language_id = {
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".mjs": "javascript",
            ".cjs": "javascript",
        }.get(ext, "plaintext")
        proc.stdin.write(
            _frame(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            "uri": _file_uri(file),
                            "languageId": language_id,
                            "version": 1,
                            "text": text,
                        }
                    },
                }
            )
        )
        await proc.stdin.drain()

        # 3. drain notifications until we get publishDiagnostics or timeout
        deadline = asyncio.get_event_loop().time() + diagnostics_wait
        while asyncio.get_event_loop().time() < deadline:
            try:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                msg = await asyncio.wait_for(
                    _read_message(proc.stdout), timeout=remaining
                )
            except TimeoutError:
                break
            if msg is None:
                break
            if msg.get("method") == "textDocument/publishDiagnostics":
                params = msg.get("params") or {}
                msg_uri = params.get("uri", "")
                if msg_uri != _file_uri(file):
                    continue
                for d in params.get("diagnostics", []):
                    rng = d.get("range", {}).get("start", {})
                    diagnostics.append(
                        Diagnostic(
                            file=file_path,
                            line=int(rng.get("line", 0)) + 1,
                            column=int(rng.get("character", 0)) + 1,
                            severity=_SEVERITY.get(
                                int(d.get("severity", 1)), "info"
                            ),
                            message=str(d.get("message", "")),
                            code=str(d.get("code", "") or "") or None,
                            source=str(d.get("source", "") or "") or None,
                        )
                    )
                # Server may publish multiple times as it refines; one
                # batch is enough for the agent's "is this file broken"
                # use case.
                return

    try:
        await asyncio.wait_for(_interact(), timeout=hard_timeout)
    except TimeoutError:
        return LspResult(
            file=file_path,
            server=server_name,
            error=f"timed out waiting for {server_name} (>{hard_timeout}s)",
            diagnostics=diagnostics,
        )
    except (BrokenPipeError, ConnectionResetError) as exc:
        return LspResult(
            file=file_path,
            server=server_name,
            error=f"{server_name} died mid-conversation: {exc}",
            diagnostics=diagnostics,
        )
    finally:
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                proc.kill()
            except ProcessLookupError:
                pass

    return LspResult(file=file_path, server=server_name, diagnostics=diagnostics)
