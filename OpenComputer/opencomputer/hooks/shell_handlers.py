"""Shell-command hook handler factory.

III.6 — mirrors Claude Code's settings-based hook invocation
(``sources/claude-code/plugins/plugin-dev/skills/hook-development/SKILL.md``).

Users declare shell-command hooks in the top-level ``hooks:`` key of
``config.yaml``. :func:`make_shell_hook_handler` wraps each
:class:`HookCommandConfig` in an async handler suitable for
:class:`plugin_sdk.hooks.HookSpec`.

Contract (matches Claude Code):
  - A small JSON blob carrying the :class:`HookContext` fields is piped to
    the subprocess on stdin so scripts can read it with ``input=$(cat)``
    then ``jq``-parse.
  - Exit 0 → ``HookDecision(decision="pass")`` (no opinion; tool runs).
  - Exit 2 → ``HookDecision(decision="block", reason=<stderr>)``.
  - Any other non-zero exit or script crash → logged at WARNING, returns
    ``decision="pass"`` (fail-open — settings hooks must never brick the CLI).
  - Timeout → subprocess killed, logged, returns ``decision="pass"``.

Subprocess env contract (in addition to parent env):
  - ``OPENCOMPUTER_PROFILE_HOME`` — active profile's home dir.
  - ``OPENCOMPUTER_EVENT``       — hook event name (e.g. ``"PreToolUse"``).
  - ``OPENCOMPUTER_TOOL_NAME``   — tool name for Pre/PostToolUse, else ``""``.
  - ``OPENCOMPUTER_SESSION_ID``  — current session id.
  - ``CLAUDE_PLUGIN_ROOT``       — aliased to the profile home dir so scripts
    authored for Claude Code's plugin contract still work out of the box.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shlex
from typing import Any

from opencomputer.agent.config import HookCommandConfig, _home
from plugin_sdk.hooks import HookContext, HookDecision, HookHandler

_log = logging.getLogger("opencomputer.hooks.shell")


def _ctx_payload(ctx: HookContext) -> dict[str, Any]:
    """Serialise a :class:`HookContext` into a JSON-safe payload.

    Mirrors the shape Claude Code's hook stdin carries (see
    ``sources/claude-code/plugins/plugin-dev/skills/hook-development/SKILL.md``
    §"Hook Input Format"). Missing / ``None`` fields are omitted rather
    than nulled so scripts can do ``.tool_name // empty`` checks cleanly.
    """
    payload: dict[str, Any] = {
        "hook_event_name": ctx.event.value,
        "session_id": ctx.session_id,
    }
    if ctx.tool_call is not None:
        payload["tool_name"] = ctx.tool_call.name
        try:
            payload["tool_input"] = dict(ctx.tool_call.arguments)
        except (TypeError, ValueError):
            payload["tool_input"] = {}
    if ctx.tool_result is not None:
        payload["tool_result"] = {
            "tool_call_id": ctx.tool_result.tool_call_id,
            "content": ctx.tool_result.content,
            "is_error": ctx.tool_result.is_error,
        }
    if ctx.message is not None:
        payload["message"] = {"role": ctx.message.role, "content": ctx.message.content}
    if ctx.runtime is not None:
        from plugin_sdk import effective_permission_mode

        try:
            payload["runtime"] = dataclasses.asdict(ctx.runtime)
        except TypeError:
            payload["runtime"] = {
                "plan_mode": getattr(ctx.runtime, "plan_mode", False),
                "yolo_mode": getattr(ctx.runtime, "yolo_mode", False),
            }
        # Canonical mode value alongside the legacy bools — settings-hooks
        # can read OPENCOMPUTER_PERMISSION_MODE without inferring from the
        # two-bool combination.
        payload["runtime"]["permission_mode"] = effective_permission_mode(
            ctx.runtime
        ).value
    return payload


def _build_env(ctx: HookContext) -> dict[str, str]:
    """Assemble the env dict handed to the subprocess.

    Parent env is inherited verbatim plus the OpenComputer-specific keys
    documented in the module docstring. ``CLAUDE_PLUGIN_ROOT`` is cheap
    goodwill: existing Claude Code hook scripts reference it for
    portability, so aliasing it to the profile home lets users drop those
    scripts into OpenComputer unchanged.
    """
    env = dict(os.environ)
    tool_name = ctx.tool_call.name if ctx.tool_call is not None else ""
    profile_home = str(_home())
    env.update(
        {
            "OPENCOMPUTER_PROFILE_HOME": profile_home,
            "OPENCOMPUTER_EVENT": ctx.event.value,
            "OPENCOMPUTER_TOOL_NAME": tool_name,
            "OPENCOMPUTER_SESSION_ID": ctx.session_id,
            # Alias for Claude Code hook-script compatibility.
            "CLAUDE_PLUGIN_ROOT": profile_home,
        }
    )
    return env


def make_shell_hook_handler(config: HookCommandConfig) -> HookHandler:
    """Return an async :class:`HookHandler` that invokes ``config.command``.

    The returned coroutine:

    1. Splits ``config.command`` via :func:`shlex.split` and spawns the
       argv under ``asyncio.create_subprocess_exec`` with ``shell=False``
       — injection-safe; no implicit shell interpretation.
    2. Pipes a JSON-serialised :class:`HookContext` to stdin.
    3. Enforces ``config.timeout_seconds`` via :func:`asyncio.wait_for`.
       On timeout: kill the process, log, return ``decision="pass"``.
    4. Translates the exit code into a :class:`HookDecision` per the
       module-level contract.

    The whole point is settings-declared hooks *can't* brick the CLI: any
    exception or unexpected failure returns a passing decision.
    """

    async def _run(ctx: HookContext) -> HookDecision | None:
        try:
            argv = shlex.split(config.command)
        except ValueError as e:
            _log.warning(
                "settings hook: command %r failed to split (%s); passing", config.command, e
            )
            return HookDecision(decision="pass")
        if not argv:
            _log.warning("settings hook: empty command; passing")
            return HookDecision(decision="pass")

        payload = json.dumps(_ctx_payload(ctx)).encode("utf-8")
        env = _build_env(ctx)

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as e:
            _log.warning(
                "settings hook: executable not found for %r (%s); passing", config.command, e
            )
            return HookDecision(decision="pass")
        except Exception as e:  # noqa: BLE001 — fail-open
            _log.warning(
                "settings hook: spawn failed for %r (%s); passing", config.command, e
            )
            return HookDecision(decision="pass")

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=payload),
                timeout=config.timeout_seconds,
            )
        except TimeoutError:
            _log.warning(
                "settings hook: command %r exceeded timeout of %ss; killing",
                config.command,
                config.timeout_seconds,
            )
            try:
                proc.kill()
                # Drain so the process can exit cleanly and we don't leak a zombie.
                await proc.wait()
            except ProcessLookupError:
                pass
            except Exception as e:  # noqa: BLE001
                _log.debug("settings hook: post-kill cleanup swallowed: %s", e)
            return HookDecision(decision="pass")
        except Exception as e:  # noqa: BLE001 — fail-open
            _log.warning(
                "settings hook: communicate() raised for %r (%s); passing",
                config.command,
                e,
            )
            return HookDecision(decision="pass")

        rc = proc.returncode
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""

        if rc == 0:
            return HookDecision(decision="pass")
        if rc == 2:
            # Matches Claude Code's convention: exit 2 = blocking error
            # with stderr as the reason fed back to the model.
            reason = stderr_text or "blocked by settings hook"
            return HookDecision(decision="block", reason=reason)

        _log.warning(
            "settings hook: command %r exited with rc=%s (stderr=%r); passing",
            config.command,
            rc,
            stderr_text,
        )
        return HookDecision(decision="pass")

    return _run


__all__ = ["make_shell_hook_handler"]
