"""Built-in (non-plugin) slash commands.

V3.A-T10: Surfaces top-level skills and harness features as ``/`` commands
without requiring a full plugin. Built-in commands register directly into
the shared :mod:`opencomputer.plugins.registry` ``slash_commands`` dict at
import time of this module — exactly the same dispatch path used by
plugin-authored commands, just without the plugin lifecycle.

Currently registers:

- ``/scrape`` — invoke the profile-scraper skill (V3.A-T2)

Each command lives in its own module under
``opencomputer/agent/slash_commands_impl/`` and is wired into the shared
registry by :func:`register_builtin_slash_commands`. Tests use
:func:`get_registered_commands` to introspect what's registered without
having to reach into ``plugin_registry.slash_commands`` directly.

Idempotent: calling ``register_builtin_slash_commands()`` more than once
is safe — re-registration of an existing name is a no-op rather than an
error, so re-imports during test runs don't blow up.
"""
from __future__ import annotations

from typing import Any

from opencomputer.agent.slash_commands_impl.scrape import ScrapeCommand
from opencomputer.plugins.registry import registry as _plugin_registry

# The built-in slash command classes. Each is instantiated by
# ``register_builtin_slash_commands`` — list lets new built-ins drop in
# without touching the registration function body.
_BUILTIN_COMMANDS: tuple[type, ...] = (ScrapeCommand,)


def register_builtin_slash_commands() -> None:
    """Register every built-in slash command into the shared registry.

    Idempotent — if a name is already present (e.g. another import
    already registered it, or a plugin registered the same name first)
    we leave the existing entry alone. This matches the agent loop's
    expectation that ``slash_commands`` is read-mostly after startup.
    """
    for cls in _BUILTIN_COMMANDS:
        cmd = cls()
        name = getattr(cmd, "name", None)
        if not name:
            continue
        if name in _plugin_registry.slash_commands:
            continue
        _plugin_registry.slash_commands[name] = cmd


def get_registered_commands() -> list[Any]:
    """Return the live list of every slash command currently registered.

    Includes both plugin-authored commands AND built-ins from this module.
    The agent loop dispatches against the same dict, so this is the source
    of truth.
    """
    return list(_plugin_registry.slash_commands.values())


def dispatch_slash(message: str) -> str:
    """Synchronously dispatch ``message`` (e.g. ``"/scrape --diff"``).

    Convenience helper for tests + non-async callers (REPL, CLI debug).
    Wraps the async dispatcher behind ``asyncio.run`` and unwraps the
    :class:`SlashCommandResult` to its ``output`` string.

    Returns the empty string if ``message`` isn't a slash command or no
    matching command is registered — parallels the dispatcher's
    ``None``-return behaviour but in string form.
    """
    import asyncio

    from opencomputer.agent.slash_dispatcher import dispatch
    from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT

    # Make sure built-ins are present before we dispatch — callers that
    # import this module just to call dispatch_slash shouldn't need a
    # separate registration step.
    register_builtin_slash_commands()

    result = asyncio.run(
        dispatch(
            message,
            _plugin_registry.slash_commands,
            DEFAULT_RUNTIME_CONTEXT,
        )
    )
    if result is None:
        return ""
    return result.output


# Eager registration on import — keeps the surface area discoverable
# (any ``import opencomputer.agent.slash_commands`` puts the built-ins
# in place) without requiring the agent loop or CLI to know about it.
register_builtin_slash_commands()


__all__ = [
    "dispatch_slash",
    "get_registered_commands",
    "register_builtin_slash_commands",
]
