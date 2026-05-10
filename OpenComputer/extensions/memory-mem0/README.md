# memory-mem0

Vector-ranked fact memory backed by [Mem0](https://mem0.ai) (Apache-2.0).

This is the second alternative memory backend (after `memory-honcho`) — see
the Hermes deep-comparison rationale for why exactly two: Honcho specialises
in Theory-of-Mind user modelling; Mem0 specialises in fact extraction +
semantic search over a long history. They complement each other.

## Status

- **Default OFF.** Enable via `oc plugin enable memory-mem0`.
- **Optional dep:** install `mem0ai` (`pip install opencomputer[mem0]` or
  `pip install mem0ai`). The plugin gracefully degrades to a no-op if the
  dep isn't present, so the rest of the system keeps working.

## Configuration

Environment variables (read at plugin register time):

- `MEM0_API_KEY` — for the hosted Mem0 cloud (default).
- `MEM0_BASE_URL` — for self-hosted Mem0 (overrides the cloud).
- `MEM0_USER_ID` — namespace for your memories. Defaults to the active
  OpenComputer profile (mirrors the Honcho `host_key` pattern).

## Tools exposed

When enabled and the SDK is installed, three namespaced tools become
available to the agent:

- `mem0_search(query, limit=5)` — semantic recall.
- `mem0_remember(content)` — explicit fact write.
- `mem0_forget(memory_id)` — explicit removal.

The provider also injects a brief "## Memory context" section into the
system prompt summarising the most relevant memories for the current
session (cadence: every turn).

## Why not vendor mem0?

Mem0 is Apache-2.0 — vendoring is fine licence-wise, but the upstream
churns quickly. We pin a SDK version range and depend at install time
rather than vendoring source. See `pyproject.toml`'s `[mem0]` extra.
