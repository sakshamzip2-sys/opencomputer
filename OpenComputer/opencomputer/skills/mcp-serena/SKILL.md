---
name: mcp-serena
description: Use Serena MCP for semantic codebase search, symbol navigation, and project-context queries. Pairs with lsp-bridge — Serena handles symbol-level "where is X defined / referenced" queries; lsp-bridge handles file-level diagnostics. Use when the user says find this function across the repo, where is class Foo used, semantic search, navigate to definition, list references, or rename symbol. Read-only by default; mutates code only via explicit edit tools.
version: 0.1.0
---

# Serena MCP — Semantic Code Search

[Serena](https://github.com/oraios/serena) is an open-source MCP server
that turns a repo into a queryable index built on language-server
infrastructure. Where `lsp-bridge` answers "what's wrong with this
file", Serena answers "where in this codebase is X defined,
referenced, called, or named".

## Install (one-time)

```bash
# Clone + run via uvx (no Python install pollution)
uvx --from git+https://github.com/oraios/serena \
    serena-mcp-server --help

# Or install globally
uv pip install --user git+https://github.com/oraios/serena
```

Add to OC's MCP config (per profile):

```bash
oc mcp add serena \
  --command uvx \
  --args "--from,git+https://github.com/oraios/serena,serena-mcp-server,--project,$(pwd)"
```

Confirm: `oc mcp list | grep serena`.

## When to use Serena (vs alternatives)

| Question shape | Best tool |
|----------------|-----------|
| "What's wrong with this file?" | `LspDiagnostics` (lsp-bridge) |
| "Where is `compute_total` defined?" | Serena `find_symbol` |
| "Where is `User.email` referenced?" | Serena `find_referencing_symbols` |
| "List all classes in this directory" | Serena `get_symbols_overview` |
| "Insert this method into class X" | Serena `insert_after_symbol` |
| "Search for the string `TODO`" | Built-in `Grep` (faster for plain text) |
| "Find files matching `*.test.ts`" | Built-in `Glob` |

Serena's edge over plain Grep: it knows **symbols**, not just text.
"Where is `add` referenced" doesn't return random `add` words in
comments or unrelated `dict.add` calls — it returns the actual
references to the function definition you're sitting on.

## Typical workflow

1. **Onboard the project** (one-time per repo):
   `serena.activate_project(<path>)` — Serena indexes the codebase
   and writes a `.serena/` cache.
2. **Find the symbol**:
   `serena.find_symbol(name_path="compute_total", relative_path="src")`.
3. **Inspect references** before editing:
   `serena.find_referencing_symbols(<symbol-id>)`.
4. **Apply the change** with the right blast radius — manually or via
   Serena's `replace_symbol_body`.

The `.serena/` cache should be added to `.gitignore` (Serena does not
write back into source files unless an edit tool is invoked).

## What this skill does

When the user asks a "where is …" / "find … across the repo" / "rename
this symbol" question:

1. Confirm Serena is configured: `oc mcp list | grep serena`. If not,
   walk through the install above.
2. If the project hasn't been onboarded, offer to run
   `serena.activate_project(<cwd>)` first.
3. Call the right tool family:
   - **Discovery:** `find_symbol`, `get_symbols_overview`,
     `list_dir`, `find_file`.
   - **Navigation:** `find_referencing_symbols`, `read_memory`.
   - **Editing:** `replace_symbol_body`, `insert_after_symbol`,
     `insert_before_symbol`. Edit tools mutate source — confirm with
     the user before invoking unless they explicitly asked.

## Why pair with lsp-bridge

Serena and `lsp-bridge` (the bundled OC LSP-diagnostics plugin) are
complementary:

| | lsp-bridge | Serena |
|-|------------|--------|
| Granularity | one file | whole project |
| Latency | ~200ms / file | ~50ms / query (after index) |
| Output | diagnostics | symbol metadata + references |
| When | post-edit sanity | pre-edit research |

Use both: research with Serena, edit, verify with `LspDiagnostics`.

## See also

- `oc mcp` — generic MCP CLI surface
- `extensions/lsp-bridge/` — file-level LSP diagnostics
- `opencomputer/skills/native-mcp/` — how OC wires MCP servers in general
