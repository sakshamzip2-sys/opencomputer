# Transport choice — `stdio` vs `sse` vs `http`

Quick decision matrix for which transport to pick when running a
FastMCP server.

| Transport | When to pick | Pros | Cons |
|-----------|--------------|------|------|
| **stdio** | Local dev, single-agent, ephemeral lifetime | Zero network setup, fork-and-forget, no port collision risk | One process per agent — no sharing across IDEs |
| **http** | Shared service, multiple agents/IDEs | Multiple concurrent clients, REST-style debugging via curl | Needs a port + auth + lifecycle management |
| **sse** | Legacy clients only | Backwards compat with pre-2025-03 spec | Deprecated in favour of `http`; pick `http` for new servers |

## stdio specifics

- The agent's loop spawns the process and pipes JSON-RPC over
  stdin/stdout.
- The process exits when the agent disconnects; expect frequent
  cold-start overhead.
- **Never** `print()` to stdout — that stream is the wire. Use
  `logging` to stderr instead.
- Heavy imports at module top level slow every cold-start; defer them
  inside the tool function or use lazy module loaders.

## http specifics

- Bind explicitly via `server.run(transport="http", host="...",
  port=...)`.
- Default to `127.0.0.1` for local dev. Bind `0.0.0.0` only when you
  understand the auth model (FastMCP itself does not enforce auth —
  put it behind a reverse proxy or wrap with FastAPI middleware).
- Idle servers stay running between calls — no cold-start, but you
  pay always-on memory + you need a process supervisor (launchd,
  systemd, supervisord, Docker).

## sse specifics

- Same shape as http but uses SSE for the streaming response. The MCP
  spec marks this as a legacy fallback for clients that haven't moved
  to streamable HTTP.
- Don't pick `sse` for new servers unless you know a target client
  needs it.

## Choosing in code

```python
import os
import sys


def main() -> None:
    if "--http" in sys.argv:
        server.run(transport="http", host="127.0.0.1", port=8765)
    elif "--sse" in sys.argv:
        server.run(transport="sse", host="127.0.0.1", port=8765)
    else:
        server.run(transport="stdio")
```

This pattern lets the same server file run both ways — useful when
you want a single codebase but multiple deploy modes.
