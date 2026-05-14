---
name: mcp-context7
description: Use Context7 MCP for up-to-date documentation lookups for any library, framework, SDK, API, CLI tool, or cloud service. Use when the user asks about library X version Y, how do I use the new API, what changed in framework Z, library-specific debugging, setup instructions, or CLI tool usage. Always prefer Context7 over web search for library docs because it returns version-pinned snippets the model can quote verbatim. Read-only.
version: 0.1.0
---

# Context7 MCP — Live Documentation Lookup

[Context7](https://context7.com) indexes documentation for thousands
of libraries, SDKs, and frameworks and serves it through an MCP server
that returns the actual source-of-truth snippets — not paraphrases or
hallucinated APIs.

## Install (one-time)

The Context7 MCP server is shipped as a hosted MCP. Add it via:

```bash
oc mcp add context7 \
  --transport http \
  --url https://mcp.context7.com/mcp
```

Confirm: `oc mcp list | grep context7`.

The server is unauthenticated for the public docs catalog. Some
private libraries require a Context7 API key — see context7.com for
sign-up.

## Two tools

| Tool | When |
|------|------|
| `resolve-library-id` | First call when you have a library name like "react" or "prisma" — returns the Context7 library id (e.g. `/vercel/next.js`) |
| `query-docs`         | Pull actual doc snippets for a topic within that library |

Typical pattern:

```
resolve-library-id(name="prisma") → /prisma/prisma
query-docs(library_id="/prisma/prisma", topic="middleware deprecation")
  → returns the official deprecation notice + recommended migration
```

## When to use Context7 (vs alternatives)

Use Context7 when:

- You're about to write code against a library and want the **current**
  API shape — not what your training data remembers.
- The user asks "how do I do X with Y" for any library Y.
- You're debugging a library-specific error message.
- The library released v2/v3/v4 recently — Context7 returns the
  version-pinned snippet you can trust.

Do NOT use Context7 for:

- General programming concepts (no library involved).
- Refactoring decisions about user code.
- Code review of business logic.
- Searching the user's own codebase — use `Grep` / `Glob` /
  `mcp-serena` for that.

## Why this matters for OC

OC's training data has a knowledge cutoff. Library APIs move every
release. Context7 is the bridge — it lets the agent quote *the docs as
they exist today* rather than guess from the most recent training
snapshot. For frameworks like Next.js, Prisma, or AWS SDK that ship
breaking-change minor versions, Context7 is the difference between
working code and apologetic stack traces.

## Typical workflow

When the user asks a library-related question:

1. Identify the library name from the prompt.
2. `resolve-library-id(name=...)` → get the canonical id.
3. Pick a tight `topic` string for `query-docs` — the more specific
   the better (e.g. `"server actions cookies"` beats `"actions"`).
4. Quote relevant snippets back to the user with the source URL
   Context7 returns, so they can verify.
5. Write code against the snippet — not against memory.

## See also

- `mcp-serena` — semantic code search for the **user's own** codebase
- `opencomputer/skills/native-mcp/` — how OC wires MCP servers
- `oc mcp list` / `oc mcp remove`
