# Hermes-Parity Tier S — Implementation Plan

> Use superpowers:executing-plans to implement task-by-task.

**Goal:** Ship 8 commands (3 CLI + 5 slash) that close the highest-value Hermes parity gaps in OC.

**Architecture:** Add to existing CLI module + slash_commands_impl directory. No new modules.

---

## Task 1: `oc model` CLI command

**Files:**
- Modify: `opencomputer/cli.py`

- [ ] **Step 1: Locate `oc models add` interactive picker entry point**
```bash
cd /tmp/oc-hermes-parity/OpenComputer && grep -n "models.*command\|_select_model\|interactive_pick_model\|@models_app\|@app\.command(\"models\"\|def models" opencomputer/cli.py | head -20
```

- [ ] **Step 2: Add `model` as top-level alias.** Single command that delegates to `oc models add` (or its underlying function).

```python
@app.command(name="model")
def model_alias() -> None:
    """Select default model and provider (interactive). Alias for `oc models add`."""
    # Delegate to the same function `oc models add` calls
    from opencomputer.cli_models import models_add  # or wherever the impl lives
    models_add()
```

- [ ] **Step 3: Test** — run `oc model` from worktree, assert exit code 0 + same picker output as `oc models add`.

- [ ] **Step 4: Commit**

---

## Task 2: `oc login` CLI command

**Files:**
- Create: `opencomputer/cli_login.py`
- Modify: `opencomputer/cli.py` (register subcommand)

- [ ] **Step 1: Define provider→env var mapping**

```python
PROVIDER_ENV_MAP: dict[str, str] = {
    "anthropic":  "ANTHROPIC_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "groq":       "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "google":     "GOOGLE_API_KEY",
}
```

- [ ] **Step 2: Implement login function**

```python
def login(provider: str) -> None:
    if provider not in PROVIDER_ENV_MAP:
        typer.echo(f"Unknown provider '{provider}'. Valid: {list(PROVIDER_ENV_MAP)}", err=True)
        raise typer.Exit(2)
    env_var = PROVIDER_ENV_MAP[provider]
    key = typer.prompt(f"Enter {provider} API key", hide_input=True)
    if not key.strip():
        typer.echo("Empty key — aborting.", err=True)
        raise typer.Exit(1)
    env_path = _home() / ".env"
    _upsert_env_var(env_path, env_var, key.strip())
    env_path.chmod(0o600)
    typer.echo(f"✓ Stored {env_var} in {env_path}")
```

- [ ] **Step 3: `_upsert_env_var` helper** — read `.env`, replace or append the matching line, write back atomically.

- [ ] **Step 4: Tests** — temp dir as profile home, login writes correctly, validates allowlist, idempotent on re-login.

- [ ] **Step 5: Commit**

---

## Task 3: `oc logout` CLI command

**Files:**
- Modify: `opencomputer/cli_login.py`
- Modify: `opencomputer/cli.py` (register subcommand)

- [ ] **Step 1: Implement logout**

```python
def logout(provider: str) -> None:
    if provider not in PROVIDER_ENV_MAP:
        typer.echo(f"Unknown provider '{provider}'", err=True)
        raise typer.Exit(2)
    env_var = PROVIDER_ENV_MAP[provider]
    env_path = _home() / ".env"
    if not env_path.exists():
        typer.echo(f"No credentials stored for any provider.", err=True)
        raise typer.Exit(1)
    removed = _remove_env_var(env_path, env_var)
    if removed:
        typer.echo(f"✓ Cleared {env_var}")
    else:
        typer.echo(f"({env_var} was not stored)")
```

- [ ] **Step 2: `_remove_env_var` helper** — read `.env`, drop the matching line, write back. Return bool whether removal happened.

- [ ] **Step 3: Tests + commit**

---

## Task 4: `/new` and `/reset` slash command (aliases)

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/new_cmd.py`
- Modify: `opencomputer/agent/slash_commands.py` (register)

- [ ] **Step 1: Implement**

```python
class NewSessionCommand(SlashCommand):
    name = "new"
    description = "Start a fresh session (alias for /clear)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        # Delegate to /clear — clear runtime session_id, mark for fresh start
        # The actual session-creation happens in the next user turn
        runtime.custom["force_new_session"] = True
        return SlashCommandResult(
            output="✓ Session cleared. Next message starts a fresh session.",
            handled=True,
        )


class ResetSessionCommand(NewSessionCommand):
    name = "reset"
    description = "Alias for /new"
```

- [ ] **Step 2: Tests + register + commit**

---

## Task 5: `/retry` slash command

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/retry_cmd.py`
- Modify: `opencomputer/agent/slash_commands.py`

- [ ] **Step 1: Implement**

```python
class RetryCommand(SlashCommand):
    name = "retry"
    description = "Resend the last user message"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sid = runtime.custom.get("session_id")
        db = runtime.custom.get("session_db")
        if not sid or db is None:
            return SlashCommandResult(
                output="No active session.", handled=True,
            )
        messages = db.get_messages(sid)
        last_user = next(
            (m for m in reversed(messages) if m.role == "user"),
            None,
        )
        if last_user is None:
            return SlashCommandResult(
                output="No previous user message to retry.", handled=True,
            )
        # Set runtime flag to inject this message as next user input
        runtime.custom["retry_message"] = last_user.content
        return SlashCommandResult(
            output=f"↻ Retrying last message ({len(str(last_user.content))} chars)…",
            handled=True,
        )
```

- [ ] **Step 2: Wire `retry_message` consumption in agent loop** (lookup runtime.custom at turn start)

- [ ] **Step 3: Tests + commit**

---

## Task 6: `/stop` slash command

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/stop_cmd.py`
- Modify: `opencomputer/agent/slash_commands.py`

- [ ] **Step 1: Implement**

```python
class StopCommand(SlashCommand):
    name = "stop"
    description = "Kill all background processes for this session"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sid = runtime.custom.get("session_id")
        bg_mgr = runtime.custom.get("bg_process_manager")
        if not sid or bg_mgr is None:
            return SlashCommandResult(
                output="No bg-process manager available.", handled=True,
            )
        killed = bg_mgr.kill_all_for_session(sid)
        if killed == 0:
            return SlashCommandResult(
                output="No background processes running.", handled=True,
            )
        return SlashCommandResult(
            output=f"✓ Killed {killed} background process(es).", handled=True,
        )
```

- [ ] **Step 2: Verify bg_process_manager API** — `grep` for actual class/method names

- [ ] **Step 3: Tests + commit**

---

## Task 7: `/quit` alias for `/exit`

**Files:**
- Modify: `opencomputer/cli_ui/slash_handlers.py` (where `/exit` is handled)

- [ ] **Step 1: Add `/quit` to the same code path that handles `/exit`**

```python
elif command in ("exit", "quit"):  # was: elif command == "exit":
    return SlashHandlerResult(quit=True, ...)
```

- [ ] **Step 2: Test that `/quit` exits the same as `/exit` + commit**

---

## Task 8: `/compress` slash command

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/compress_cmd.py`
- Modify: `opencomputer/agent/slash_commands.py`

- [ ] **Step 1: Implement**

```python
class CompressCommand(SlashCommand):
    name = "compress"
    description = "Manually compress conversation history (summarize older turns)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        compaction = runtime.custom.get("compaction_engine")
        messages = runtime.custom.get("messages")
        if compaction is None or messages is None:
            return SlashCommandResult(
                output="Compaction not available outside agent loop.",
                handled=True,
            )
        try:
            result = await compaction.maybe_run(
                list(messages),
                runtime.custom.get("last_input_tokens", 0),
                force=True,
            )
        except TypeError:
            # Older signature without force param — call with high token count
            result = await compaction.maybe_run(
                list(messages),
                10_000_000,  # forces threshold
            )
        if result.did_compact:
            runtime.custom["messages"] = result.messages
            return SlashCommandResult(
                output=f"✓ Compressed: {len(messages)} → {len(result.messages)} messages.",
                handled=True,
            )
        return SlashCommandResult(
            output="No compression — context not large enough yet.", handled=True,
        )
```

- [ ] **Step 2: Verify CompactionEngine API + force flag** — `grep maybe_run`

- [ ] **Step 3: Tests + commit**

---

## Task 9: Run pytest + ruff + audit + push

- [ ] **Step 1: Full pytest** — verify no regressions
- [ ] **Step 2: Ruff** — clean
- [ ] **Step 3: Audit subagent** — find any BLOCKERs before push
- [ ] **Step 4: Push + open PR**
