"""Slash-command dispatch. Called from ``run_conversation`` before the
LLM is invoked — if the user's message starts with ``/<name>`` and a
matching command is registered, execute it instead of (or alongside,
depending on ``handled``) the LLM call.

Formalization of the Phase-6f duck-typed contract. Accepts BOTH proper
``plugin_sdk.SlashCommand`` subclass instances AND legacy duck-typed
objects with the shape ``{name, description, execute}``. Legacy
commands that return a bare string are wrapped into a
``SlashCommandResult`` transparently.

Exception safety: if a registered command's ``execute`` raises, the
dispatcher catches it and returns a ``SlashCommandResult`` whose output
describes the failure. The agent loop will surface that to the user as
the turn's assistant reply; no traceback escapes into the loop.
"""

from __future__ import annotations

import inspect
from typing import Any

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommandResult


def parse_slash(message: str) -> tuple[str, str] | None:
    """Return ``(command_name, args)`` if ``message`` starts with ``/``,
    else ``None``. Whitespace-separates name and args.
    """
    if not message.startswith("/"):
        return None
    rest = message[1:]
    if not rest:
        return None
    parts = rest.split(None, 1)
    name = parts[0]
    args = parts[1] if len(parts) > 1 else ""
    return (name, args)


async def dispatch(
    message: str,
    slash_commands: dict[str, Any],
    runtime: RuntimeContext,
    fallback: Any | None = None,
) -> SlashCommandResult | None:
    """Dispatch ``message`` to a registered slash command if it matches.

    Returns the command's result, or ``None`` if the message isn't a
    slash command or no matching command is registered AND no fallback
    resolves it.

    ``fallback`` (Tier 2.A `/<skill-name>` auto-dispatch): optional
    callable invoked when the primary dict lookup misses. Signature is
    ``fallback(name: str, args: str, runtime: RuntimeContext)`` returning
    ``SlashCommandResult | str | None``. The agent loop wires this to a
    skill-resolver so e.g. ``/pead-screener`` loads the SKILL.md body
    inline. Exceptions from the fallback are caught the same way as
    direct command failures.
    """
    parsed = parse_slash(message)
    if parsed is None:
        return None
    name, args = parsed

    # Hermes-CLI parity A6 — quick commands run BEFORE registry lookup so
    # they can shadow a slash name. ``runtime.custom["_quick_commands"]``
    # is loaded once at session start by the chat entry point.
    quick = runtime.custom.get("_quick_commands")
    if quick is not None:
        try:
            qresult = quick.run(name, args)
        except Exception as exc:  # noqa: BLE001 — never crash the dispatch
            return SlashCommandResult(
                output=f"quick-command /{name} failed: {type(exc).__name__}: {exc}",
                handled=True,
            )
        if qresult is not None:
            return SlashCommandResult(
                output=qresult.output or ("(timed out)" if qresult.timed_out else ""),
                handled=True,
            )

    cmd = slash_commands.get(name)
    if cmd is None:
        if fallback is not None:
            try:
                raw = fallback(name, args, runtime)
                if inspect.isawaitable(raw):
                    raw = await raw
            except Exception as exc:  # noqa: BLE001
                return SlashCommandResult(
                    output=f"slash fallback for '/{name}' raised {type(exc).__name__}: {exc}",
                    handled=True,
                )
            if raw is None:
                return None
            if isinstance(raw, str):
                return SlashCommandResult(output=raw, handled=True)
            if isinstance(raw, SlashCommandResult):
                return raw
            return SlashCommandResult(output=str(raw), handled=True)
        return None
    try:
        raw = cmd.execute(args, runtime)
        # Support both async and sync execute() — await anything awaitable.
        if inspect.isawaitable(raw):
            raw = await raw
    except Exception as exc:  # noqa: BLE001
        return SlashCommandResult(
            output=f"slash command '/{name}' raised {type(exc).__name__}: {exc}",
            handled=True,
        )
    # Duck-typed Phase-6f commands may return a bare string; wrap it.
    if isinstance(raw, str):
        result = SlashCommandResult(output=raw, handled=True)
    elif isinstance(raw, SlashCommandResult):
        result = raw
    else:
        # Anything else — coerce to string for safety.
        result = SlashCommandResult(output=str(raw), handled=True)

    # CC §2 (2026-05-11) — fire USER_PROMPT_EXPANSION when a slash
    # command transforms the user's literal slash text into a synthetic
    # prompt that gets fed back to the LLM. A slash whose ``output`` is
    # the empty string is a pure side-effect command (e.g. ``/quit``);
    # it does NOT count as an "expansion" to a prompt. We emit only
    # when there's prompt text to report.
    expanded_text = (result.output or "").strip()
    if expanded_text:
        try:
            from opencomputer.hooks.engine import engine as _hook_engine_upe
            from plugin_sdk.hooks import HookContext as _HookContextUPE
            from plugin_sdk.hooks import HookEvent as _HookEventUPE

            sid = runtime.custom.get("session_id") if runtime is not None else None
            _hook_engine_upe.fire_and_forget(
                _HookContextUPE(
                    event=_HookEventUPE.USER_PROMPT_EXPANSION,
                    session_id=str(sid or ""),
                    runtime=runtime,
                    expansion_source=name,
                    prompt_text=expanded_text,
                )
            )
        except Exception:  # noqa: BLE001 — observer must never wedge dispatch
            pass
    return result


__all__ = ["dispatch", "parse_slash"]
