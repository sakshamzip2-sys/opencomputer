# Hermes-Parity Tier S — Design

**Date:** 2026-04-30
**Status:** Approved (verbal). Auto mode.

## Problem

User typed `oc model` and got "No such command 'model'". Investigation revealed Hermes has ~10 CLI commands and ~12 slash commands worth-porting that OC lacks. Tier S ships the 8 highest-impact items.

## Scope (Tier S only)

### CLI commands
1. **`oc model`** — top-level alias for the `oc models add` interactive picker (closes literal failure)
2. **`oc login <provider>`** — interactive credential capture → `~/.opencomputer/<profile>/.env`
3. **`oc logout <provider>`** — clear stored credential

### Slash commands
4. **`/new` (alias `/reset`)** — clears session
5. **`/retry`** — resend last user message
6. **`/stop`** — kill all bg processes
7. **`/quit` (alias for `/exit`)** — add alias
8. **`/compress`** — manual trigger of `CompactionEngine`

## Architecture

**CLI:**
- Add 3 new `@app.command` decorators in `opencomputer/cli.py`
- `oc model` calls into the existing `_select_model_interactive()` helper or `oc models add` flow
- `oc login` / `oc logout` manage `.env` file in profile home using `python-dotenv` (already a dep)

**Slash commands:**
- Each new command in `opencomputer/agent/slash_commands_impl/<name>_cmd.py`
- Register in `_BUILTIN_COMMANDS` tuple in `slash_commands.py`
- `/new` and `/reset` and `/quit` are aliases — implemented as separate classes with shared logic
- `/retry` reads last user message from `runtime.custom["session_db"].get_messages()` and re-injects
- `/stop` iterates `runtime.custom["bg_processes"]` (or equivalent) and calls `KillProcess` semantics
- `/compress` calls `runtime.custom["compaction_engine"].maybe_run(messages, last_input_tokens, force=True)`

## Login/logout details

Per CLAUDE.md Phase 14.F supports per-profile `.env`. The login command:

```
$ oc login anthropic
Enter your Anthropic API key (paste, will not echo):
> sk-ant-...
Stored in /Users/saksham/.opencomputer/.env (chmod 600)
```

`oc logout anthropic` removes the line. Both validate the env var name against an allowlist:
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GROQ_API_KEY`
- `OPENROUTER_API_KEY`
- `GOOGLE_API_KEY`

Provider name maps to env var name. Unknown provider rejected.

## Error Handling

- `oc model` with no providers configured → falls through to `oc setup` recommendation
- `oc login <unknown>` → list valid provider names, exit 2
- `/retry` with no prior user message → "no previous user message to retry"
- `/stop` with no bg processes → "no background processes running"
- `/compress` mid-tool-call → defer until safe split point (existing CompactionEngine behavior)

## Testing

- Unit tests for `oc login` writing/reading `.env` correctly
- Unit tests for `oc logout` removing only the target line
- Slash command tests: `/new`, `/reset`, `/quit`, `/retry`, `/stop`, `/compress`
- Integration: `oc model` invocation routes to picker

Total: ~25 new tests, ~200 LOC.

## Out of Scope

- Tier A items (`/image`, `/tools`, `oc logs`, etc.) — defer to follow-up
- Tier B items (`oc backup/import`, `/background`, `oc dashboard`) — need own plans
- Provider-specific OAuth flows
- Credential pool rotation
- Web UI dashboard

## Migration / BC

- All additions; no breaking changes
- New env var allowlist is data, not API
- `_BUILTIN_COMMANDS` extension only
