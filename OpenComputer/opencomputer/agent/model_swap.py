"""Mid-session model swap — single source of truth.

Used by:

* ``cli.py::_on_model_swap`` — wired to the ``/model <id>`` slash.
* ``loop.py::run_conversation`` — consumes ``pending_model_id`` set by
  the Alt+M scoped-models keybinding.

Both paths must do the same work or they drift:

1. Detect the ``custom:<name>:<model_id>`` prefix and build a custom
   provider for it.
2. Otherwise resolve the model id through aliases.
3. Strip ``:nitro`` / ``:floor`` suffixes when the active provider
   isn't OpenRouter (those are OR-specific routing sugar).
4. Apply via ``dataclasses.replace`` so the config remains a frozen
   dataclass.
5. Refresh ``runtime.custom["_provider_supports_native_thinking"]``
   so the prompt-based fallback activates for the new model.
6. Fire ``HookEvent.BEFORE_MODEL_RESOLVE`` so plugins can observe the
   change.

Failures are honest tuples (``ok: bool, message: str``). Callers route
the message either through the slash UI or a log line — both already
work.
"""

from __future__ import annotations

import dataclasses as _dc
import logging
from typing import Any

_log = logging.getLogger(__name__)


def swap_model(
    *,
    loop: Any,
    runtime: Any,
    new_model: str,
    console: Any = None,
) -> tuple[bool, str]:
    """Apply a model swap to a running agent loop.

    Args:
        loop: The ``AgentLoop`` instance — must have ``.config`` and
            ``.provider``. Both are mutated.
        runtime: The ``RuntimeContext`` whose ``custom`` dict carries
            per-session flags (``_provider_supports_native_thinking``).
        new_model: The id the user requested. May be an alias, a
            ``custom:<name>:<model>`` spec, or a raw model id.
        console: Optional Rich Console for ``:nitro``/``:floor`` strip
            warnings. Falls back to ``_log.warning`` when absent.

    Returns:
        ``(ok, message)``. ``message`` is shown verbatim by the slash
        handler when ``ok=False``, and surfaced as a flash hint when
        the swap came from a keybinding (``ok=True``).
    """
    if not isinstance(new_model, str) or not new_model.strip():
        return (False, "model id is required (got empty string)")
    new_model = new_model.strip()

    # custom:<name>:<model_id> — build a custom provider then swap.
    if new_model.startswith("custom:"):
        return _swap_custom(loop=loop, runtime=runtime, spec=new_model)

    # Resolve aliases via the existing path used by /model.
    from opencomputer.agent.model_resolver import resolve_model

    aliases = getattr(loop.config.model, "model_aliases", None) or {}
    try:
        canonical = resolve_model(new_model, aliases)
    except ValueError as e:
        return (False, str(e))
    if not canonical or not isinstance(canonical, str):
        return (False, f"invalid model id: {new_model!r}")

    # :nitro / :floor are OpenRouter-specific — strip when not on OR.
    from opencomputer.agent.config import split_or_routing_suffix

    stripped, suffix = split_or_routing_suffix(canonical)
    if suffix is not None and loop.config.model.provider != "openrouter":
        msg = (
            f":{suffix} suffix is OpenRouter-only; stripping and using "
            f"{stripped!r} on provider {loop.config.model.provider!r}"
        )
        if console is not None:
            console.print(f"[yellow]⚠[/yellow] {msg}.")
        else:
            _log.warning(msg)
        canonical = stripped

    # Apply via dataclasses.replace — config is frozen.
    new_model_cfg = _dc.replace(loop.config.model, model=canonical)
    loop.config = _dc.replace(loop.config, model=new_model_cfg)

    # Refresh native-thinking flag so the prompt-based fallback turns
    # on/off correctly for the new model.
    try:
        runtime.custom["_provider_supports_native_thinking"] = (
            loop.provider.supports_native_thinking_for(canonical)
        )
    except Exception:  # noqa: BLE001 — defensive; some providers don't expose this
        runtime.custom["_provider_supports_native_thinking"] = False
        _log.debug(
            "provider %r has no supports_native_thinking_for; defaulting to False",
            type(loop.provider).__name__,
        )

    # Refresh the status-line cache + Alt+M cycle anchor so both pick up
    # the new model id immediately rather than waiting for the next
    # turn-entry rebuild at loop.py:1387. Without this write the swap
    # appears silent to users: ``loop.config`` flips and the very next
    # API call DOES use the new model, but the bottom-bar status line
    # (which reads ``runtime.custom["model_id"]``) keeps showing the
    # old id until the next user turn fires. That visual lag is
    # indistinguishable from "swap failed" — the bug Saksham hit on
    # 2026-05-11.
    #
    # Two keys, one write — ``model_id`` is the status-line read
    # (cli_ui/status_line.py); ``active_model_id`` is the Alt+M cycle
    # anchor (cli_ui/_model_swap.py::cycle_model). Keeping them in
    # lockstep means a /model swap and a subsequent Alt+M tap behave
    # consistently — the cycle advances FROM the current model, not
    # from favorites[0].
    _refresh_runtime_active_model_cache(runtime, canonical)

    # BEFORE_MODEL_RESOLVE hook — fire-and-forget so plugins can
    # observe mid-session model changes. Never blocks the swap.
    try:
        from opencomputer.hooks.engine import engine as _engine
        from plugin_sdk.hooks import HookContext, HookEvent

        sid = runtime.custom.get("session_id")
        _engine.fire_and_forget(
            HookContext(
                event=HookEvent.BEFORE_MODEL_RESOLVE,
                session_id=sid,
                runtime=runtime,
                # Pack the change into the messages slot since HookContext
                # doesn't have a dedicated model field — convention used
                # by other one-shot events.
                messages=[{"new_model": canonical, "source": "swap"}],
            )
        )
    except Exception:  # noqa: BLE001 — hooks must never break the loop
        _log.debug("BEFORE_MODEL_RESOLVE fire-and-forget failed", exc_info=True)

    _log.info("model swap: → %r (provider=%r)", canonical, loop.config.model.provider)
    return (True, f"swapped to {canonical}")


def _swap_custom(*, loop: Any, runtime: Any, spec: str) -> tuple[bool, str]:
    """``custom:<name>:<model_id>`` path. Builds a fresh provider
    instance and replaces both provider and config.model."""
    from opencomputer.agent.custom_provider_client import (
        build_custom_provider,
        parse_custom_model_spec,
    )

    try:
        cp_name, model_id = parse_custom_model_spec(spec)
        new_provider_inst = build_custom_provider(cp_name, loop.config)
    except (ValueError, RuntimeError) as e:
        return (False, str(e))

    loop.provider = new_provider_inst
    new_model_cfg = _dc.replace(
        loop.config.model,
        provider=f"custom:{cp_name}",
        model=model_id,
    )
    loop.config = _dc.replace(loop.config, model=new_model_cfg)

    try:
        runtime.custom["_provider_supports_native_thinking"] = (
            loop.provider.supports_native_thinking_for(model_id)
        )
    except Exception:  # noqa: BLE001
        runtime.custom["_provider_supports_native_thinking"] = False

    # Refresh the status-line cache + Alt+M cycle anchor — same rationale
    # as the canonical-swap branch above. Without this write the bottom-bar
    # would keep showing the previous model id until the next user turn.
    _refresh_runtime_active_model_cache(runtime, model_id)

    _log.info("model swap (custom): → %s:%s", cp_name, model_id)
    return (True, f"swapped to custom:{cp_name}:{model_id}")


def _refresh_runtime_active_model_cache(runtime: Any, model_id: str) -> None:
    """Update the per-session active-model cache on ``runtime.custom``.

    Two keys are written in lockstep:

    * ``model_id`` — read by ``cli_ui/status_line.py`` every keystroke
      to render the bottom-bar model name. Caching here avoids a hot-
      path attribute walk into ``loop.config.model.model`` on each
      prompt_toolkit redraw.
    * ``active_model_id`` — read by ``cli_ui/_model_swap.py::cycle_model``
      to determine where to start the Alt+M cycle. Without this anchor
      the cycle always restarts from ``favorites[0]`` after a /model
      swap, which is surprising.

    Failures are debug-logged rather than raised because the swap
    itself has already succeeded by the time we get here — a wedged
    runtime mutation must not retroactively fail a successful swap.
    """
    if runtime is None or not isinstance(model_id, str) or not model_id:
        return
    try:
        custom = runtime.custom
    except AttributeError as e:
        _log.debug(
            "swap_model: runtime has no .custom attr (%r); skipping cache refresh",
            e,
        )
        return
    try:
        custom["model_id"] = model_id
        custom["active_model_id"] = model_id
    except TypeError as e:
        # ``custom`` isn't a dict (some test stubs use plain objects).
        # Skip silently — the next turn's spread at loop.py:1387 will
        # repair the cache once a real dict is in place.
        _log.debug(
            "swap_model: runtime.custom is not subscriptable (%r); "
            "skipping cache refresh",
            e,
        )
