"""Invokes an adapter's ``async def run(args, ctx)`` and maps to ``ToolResult``.

The runner:
  1. Coerces ``call.arguments`` into the dict the adapter expects
     (filling defaults from ``AdapterArg`` entries).
  2. Builds an ``AdapterContext`` for this run.
  3. Awaits ``spec.run(args, ctx)`` under a timeout budget.
  4. Catches the typed adapter errors + maps to ``ToolResult(is_error=True,
     content=<message>)``. The ``code`` field is propagated verbatim.
  5. Formats the return value (list-of-dicts → JSON; raw → JSON) into
     a ``ToolResult.content`` string.

The runner is the single seam between the adapter contract (Python
function) and the OpenComputer tool contract (``BaseTool.execute``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from plugin_sdk.core import ToolResult

from ._ctx import AdapterContext
from ._decorator import AdapterArg, AdapterSpec

_log = logging.getLogger("opencomputer.adapter_runner")


def coerce_args(spec: AdapterSpec, raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate + coerce ``call.arguments`` to typed values.

    - Required args without a value → ``ValueError`` (caller maps to
      ``AdapterConfigError`` via ``_runner.run_adapter``).
    - Optional args fall back to the declared default (or ``None``).
    - Type coercion is best-effort; pure-string sites accept whatever
      came in.
    """
    raw = dict(raw or {})
    out: dict[str, Any] = {}
    for arg in spec.args:
        if arg.name in raw and raw[arg.name] is not None:
            out[arg.name] = _coerce(arg, raw[arg.name])
        elif arg.required:
            raise ValueError(f"missing required arg {arg.name!r}")
        else:
            out[arg.name] = arg.default
    # Pass-through any extra keys — adapters may accept undeclared
    # fields (rare; useful for power users).
    for key, value in raw.items():
        if key not in out:
            out[key] = value
    return out


def _coerce(arg: AdapterArg, value: Any) -> Any:
    t = arg.type.lower()
    if t in ("int", "integer"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if t in ("float", "number"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if t in ("bool", "boolean"):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y", "on")
        return bool(value)
    return value


async def run_adapter(
    spec: AdapterSpec,
    *,
    arguments: dict[str, Any],
    profile_home: Path,
    profile: str | None = None,
    browser_actions: Any | None = None,
    http_client: Any | None = None,
    timeout_override: float | None = None,
    trace: Any | None = None,
    call_id: str | None = None,
) -> ToolResult:
    """Execute an adapter, returning a ``ToolResult``.

    Catches the 5 typed adapter errors + ``asyncio.TimeoutError`` and
    maps each to a ``ToolResult(is_error=True, content=<msg>)`` with a
    deterministic ``code`` field for the agent to branch on.
    """
    # Lazy import — the typed errors live in browser-control, which
    # may not be importable in pure-PUBLIC test contexts.
    try:
        from extensions.browser_control._utils.errors import (  # type: ignore[import-not-found]
            AdapterConfigError,
            AdapterEmptyResultError,
            AdapterNotFoundError,
            AdapterTimeoutError,
            AuthRequiredError,
            BrowserServiceError,
        )
    except ImportError:  # pragma: no cover - fallback for partial installs
        AdapterConfigError = AdapterEmptyResultError = AdapterTimeoutError = (
            AuthRequiredError
        ) = AdapterNotFoundError = BrowserServiceError = Exception  # type: ignore[misc, assignment]

    try:
        coerced = coerce_args(spec, arguments)
    except ValueError as exc:
        return ToolResult(
            tool_call_id=call_id,
            content=f"adapter config error: {exc}",
            is_error=True,
        )

    ctx = AdapterContext.create(
        spec=spec,
        profile_home=profile_home,
        profile=profile,
        browser_actions=browser_actions,
        http_client=http_client,
        trace=trace,
    )

    timeout = timeout_override if timeout_override is not None else spec.timeout_seconds
    coro: Awaitable[Any] = spec.run(coerced, ctx)
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        msg = f"adapter {spec.tool_name} exceeded {timeout}s budget"
        return ToolResult(
            tool_call_id=call_id,
            content=msg,
            is_error=True,
        )
    except AuthRequiredError as exc:
        return ToolResult(
            tool_call_id=call_id,
            content=f"auth required: {exc}",
            is_error=True,
        )
    except AdapterTimeoutError as exc:  # type: ignore[misc]
        return ToolResult(
            tool_call_id=call_id,
            content=f"adapter timeout: {exc}",
            is_error=True,
        )
    except AdapterEmptyResultError as exc:  # type: ignore[misc]
        return ToolResult(
            tool_call_id=call_id,
            content=f"empty result: {exc}",
            is_error=True,
        )
    except AdapterConfigError as exc:  # type: ignore[misc]
        return ToolResult(
            tool_call_id=call_id,
            content=f"adapter config error: {exc}",
            is_error=True,
        )
    except AdapterNotFoundError as exc:  # type: ignore[misc]
        return ToolResult(
            tool_call_id=call_id,
            content=f"adapter not found: {exc}",
            is_error=True,
        )
    except BrowserServiceError as exc:  # type: ignore[misc]
        return ToolResult(
            tool_call_id=call_id,
            content=f"browser error: {exc}",
            is_error=True,
        )
    except Exception as exc:  # noqa: BLE001
        _log.exception("adapter %s raised", spec.tool_name)
        return ToolResult(
            tool_call_id=call_id,
            content=f"adapter {spec.tool_name} raised: {exc}",
            is_error=True,
        )

    return ToolResult(
        tool_call_id=call_id,
        content=_format_output(result),
    )


def _format_output(value: Any) -> str:
    """Format the adapter's return into a model-readable string.

    - ``list[dict]`` → indented JSON
    - ``dict`` / scalar → JSON
    - ``str`` → passthrough
    - bytes → utf-8 decode (replace) → string
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", errors="replace")
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


__all__ = ["coerce_args", "run_adapter"]
