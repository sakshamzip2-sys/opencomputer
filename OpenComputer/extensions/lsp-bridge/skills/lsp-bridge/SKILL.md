---
name: lsp-bridge
description: Run a Language Server (pyright / typescript-language-server) against one source file to get type errors and lint diagnostics. Use when the user says lint this file, type-check this file, are there errors in this Python file, or for any post-edit sanity check on a Python or TS/JS file. Subsumes the language-shell plugins Claude Code ships by exposing a single LspDiagnostics tool that selects the right server per file extension.
version: 0.1.0
---

# LSP Bridge

OpenComputer's bridge to the Language Server Protocol — agent-callable
type checking and lint via a single tool, no editor involved.

## Quick reference

```
LspDiagnostics(path="src/foo.py")
```

Returns:

```
pyright: 2 diagnostic(s) for src/foo.py
  12:5 error [reportArgumentType] (Pyright): Argument of type "int" cannot be assigned to parameter "x" of type "str"
  44:1 warning [reportUnusedImport] (Pyright): Import "os" is not accessed
```

Or, for a clean file:

```
pyright: no diagnostics for src/foo.py (file is clean).
```

Or, when the server isn't installed:

```
'pyright-langserver' not found on PATH. Install with: npm install -g pyright
```

## Supported languages today

| Extension | Server | Install |
|-----------|--------|---------|
| `.py`, `.pyi` | `pyright-langserver` | `npm install -g pyright` |
| `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs` | `typescript-language-server` | `npm install -g typescript typescript-language-server` |

Adding a new language is a one-line entry in
`extensions/lsp-bridge/language_server.py:SERVERS` plus the install
hint. No code changes elsewhere — the `LspDiagnostics` tool is
language-agnostic.

## When to use

- After editing a `.py` / `.ts` file — fast follow-up check
- Before suggesting a fix — confirm the diagnostic exists
- When the user asks "is this file broken" / "type-check this"

## When NOT to use

- Running the project's test suite — use `Bash` with `pytest` / `vitest`
- Whole-project type-checking — call once per file you care about; LSP
  is a per-file API
- Lint configuration debugging — use the linter's own CLI for that
  (e.g. `ruff check`, `eslint --print-config`)

## Architecture (~250 LOC total)

```
extensions/lsp-bridge/
├── plugin.json                  — manifest (kind=mixed)
├── plugin.py                    — register(api) → LspDiagnostics
├── language_server.py           — (extension → server) lookup table
├── lsp_client.py                — JSON-RPC client (initialize → didOpen → collect)
├── lsp_diagnostics_tool.py      — BaseTool subclass exposing the bridge
└── skills/lsp-bridge/SKILL.md   — this doc
```

The client supports only the subset of LSP needed for diagnostics:
`initialize` → `initialized` → `textDocument/didOpen` → drain
`textDocument/publishDiagnostics`. Hover, completion, go-to-def are
intentionally NOT in scope — build a separate tool when those become
relevant agent affordances.

## Tuning

Both timeouts can be patched per call by importing
`lsp_client.collect_diagnostics` directly:

| Knob | Default | What it controls |
|------|---------|------------------|
| `diagnostics_wait` | 6.0s | How long after `didOpen` before returning the current diagnostic batch |
| `hard_timeout` | 30.0s | Absolute ceiling on the whole call (kills the subprocess past this) |

Defaults are tuned for "open one small file, get a fast answer" — the
agent-in-the-loop use case. Bump them for huge files or slow CI
machines.

## Failure modes & responses

| Symptom | What you get back |
|---------|-------------------|
| Server not on PATH | `'X' not found on PATH. Install with: …` |
| File doesn't exist | `error: file not found: …` |
| Server crashes mid-conversation | partial diagnostics + `note: X died mid-conversation` |
| Server hangs | partial diagnostics + `note: timed out waiting for X (>30s)` |

The tool is `is_error=True` only for the first two (caller-fixable);
crashes / timeouts return `is_error=False` so a partial answer still
flows back through the agent loop.

## See also

- `opencomputer/tools/python_exec.py` — for running snippets, not linting
- `opencomputer/tools/bash.py` — fall back to `ruff check`, `eslint`,
  etc. when whole-project linting is needed
- The 12 empty `*-lsp` plugins in Anthropic's catalogue
  (`pyright-lsp`, `typescript-lsp`, `gopls-lsp`, …) are signals to
  Claude Code's built-in LSP host. OC subsumes them with this single
  bridge — no per-language plugin needed.
