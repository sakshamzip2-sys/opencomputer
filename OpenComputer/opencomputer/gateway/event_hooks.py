"""Gateway file-discovery hook system (Hermes Doc-2 parity, 2026-05-08).

This is the **third** hook surface in OC, alongside:

1. **Plugin hooks** — :class:`plugin_sdk.hooks.HookSpec` registered via a
   plugin's ``register(api)``. Strongly typed, full ``HookContext``.
2. **Shell hooks** — declarative ``hooks:`` block in ``config.yaml``,
   shell command per call. Claude-Code-shape exit-code contract.
3. **Gateway file-discovery hooks** *(this module)* — drop a Python
   file in ``~/.opencomputer/hooks/<name>/`` with one async ``handle()``
   function. Listens for gateway-level lifecycle events (``gateway:startup``,
   ``session:*``, ``agent:*``, ``command:*``).

Why three surfaces:

* Plugin hooks need a manifest + register call — too heavy for "drop a
  Python file and listen for one event."
* Shell hooks shell out per call — too slow for in-process orchestration
  (every event becomes a process spawn).
* Gateway file-discovery is the in-between: one async callback, no
  plugin overhead, runs in the gateway process.

Whether long-term these consolidate is an open design question; for
Hermes Doc-2 parity we ship the third surface.

## Discovery

At gateway startup, walk ``~/.opencomputer/hooks/<name>/``. Each
sub-directory is one hook. Required files:

* ``HOOK.yaml`` — manifest. ``events: [list of event names]``. Optional
  ``description`` for ``oc hooks list``.
* ``handler.py`` — defines ``async def handle(event_type: str, context:
  dict) -> None``. Errors are caught and logged.

Example:

```
~/.opencomputer/hooks/log-startups/
├── HOOK.yaml
└── handler.py
```

```yaml
# HOOK.yaml
events:
  - gateway:startup
  - session:start
description: Log every session start
```

```python
# handler.py
async def handle(event_type: str, context: dict) -> None:
    print(f"[{event_type}] {context.get('session_id')}")
```

## Dispatch

Handlers run concurrently when an event fires (one task per handler).
Errors are caught + logged; one bad hook never breaks the others.

## Module-cache safety (CLAUDE.md gotcha #1)

Multiple hook directories may share ``handler.py`` filenames. Python's
``sys.modules`` returns the first-loaded one for all imports if we use
plain ``importlib``. We synthesize unique module names per hook
directory so each handler is its own object.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("opencomputer.gateway.event_hooks")


# ─── Public event names ────────────────────────────────────────────────


GATEWAY_STARTUP = "gateway:startup"
SESSION_START = "session:start"
SESSION_END = "session:end"
SESSION_RESET = "session:reset"
AGENT_START = "agent:start"
AGENT_STEP = "agent:step"
AGENT_END = "agent:end"
COMMAND_PREFIX = "command:"  # actual events are "command:<slug>"


KNOWN_EVENTS: frozenset[str] = frozenset({
    GATEWAY_STARTUP,
    SESSION_START, SESSION_END, SESSION_RESET,
    AGENT_START, AGENT_STEP, AGENT_END,
})


# ─── Hook record ───────────────────────────────────────────────────────


HookHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class GatewayHook:
    """One discovered hook directory + its loaded handler."""

    name: str
    path: Path
    events: list[str] = field(default_factory=list)
    handler: HookHandler | None = None
    description: str = ""

    def matches(self, event_type: str) -> bool:
        """True if this hook subscribes to ``event_type``.

        Supports the ``command:*`` wildcard: a hook listening for
        ``command:*`` matches every ``command:<slug>`` event.
        """
        if event_type in self.events:
            return True
        for pattern in self.events:
            if pattern.endswith(":*"):
                prefix = pattern[:-1]  # strip the *, keep the :
                if event_type.startswith(prefix):
                    return True
        return False


# ─── Discovery ─────────────────────────────────────────────────────────


def hooks_root() -> Path:
    """Where the gateway looks for filesystem hooks.

    Honours ``$OPENCOMPUTER_HOME`` so tests can point at a tmp dir.
    Falls back to ``~/.opencomputer/hooks``.
    """
    home_env = os.environ.get("OPENCOMPUTER_HOME")
    base = Path(home_env) if home_env else Path.home() / ".opencomputer"
    return base / "hooks"


def discover_hooks(root: Path | None = None) -> list[GatewayHook]:
    """Scan ``root`` (or :func:`hooks_root` default) for hook directories.

    Returns one :class:`GatewayHook` per valid directory. Invalid
    directories (missing ``HOOK.yaml``, broken ``handler.py``) are
    logged and skipped — discovery never raises.

    Each ``handler.py`` is imported under a synthetic unique module
    name (``opencomputer._gateway_hooks.<dirname>``) to dodge
    ``sys.modules`` collisions when multiple hook dirs share filenames.
    """
    root = root or hooks_root()
    out: list[GatewayHook] = []
    if not root.exists() or not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "HOOK.yaml"
        handler_path = entry / "handler.py"
        if not manifest_path.is_file() or not handler_path.is_file():
            logger.debug(
                "gateway hook %r missing HOOK.yaml or handler.py — skipped",
                entry.name,
            )
            continue
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            logger.warning(
                "gateway hook %r has malformed HOOK.yaml: %s — skipped",
                entry.name, exc,
            )
            continue
        events = manifest.get("events") or []
        if not isinstance(events, list) or not all(isinstance(e, str) for e in events):
            logger.warning(
                "gateway hook %r: HOOK.yaml 'events' must be list[str] — skipped",
                entry.name,
            )
            continue
        description = str(manifest.get("description") or "").strip()
        try:
            handler = _load_handler_module(entry, handler_path)
        except Exception as exc:  # noqa: BLE001 — bad import never crashes
            logger.warning(
                "gateway hook %r: handler.py failed to import — %s — skipped",
                entry.name, exc,
            )
            continue
        out.append(GatewayHook(
            name=entry.name, path=entry,
            events=list(events), handler=handler, description=description,
        ))
    return out


def _load_handler_module(hook_dir: Path, handler_path: Path) -> HookHandler:
    """Import ``handler.py`` under a synthetic unique module name + return its
    ``handle`` async callable.

    Raises ``RuntimeError`` if the module doesn't define ``handle`` or
    defines it as something other than an async callable.
    """
    module_name = f"opencomputer._gateway_hooks.{hook_dir.name}"
    spec = importlib.util.spec_from_file_location(module_name, handler_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build import spec for {handler_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = getattr(module, "handle", None)
    if not callable(handler):
        raise RuntimeError("handler.py must define `async def handle(event_type, context)`")
    if not asyncio.iscoroutinefunction(handler):
        raise RuntimeError("handler.handle must be `async def`")
    return handler  # type: ignore[return-value]


# ─── Dispatch ──────────────────────────────────────────────────────────


class GatewayHookEngine:
    """Holds the discovered hooks + dispatches events to them.

    Single instance per gateway process. Reload via :meth:`reload`
    (e.g. when a user adds a new hook directory at runtime).
    """

    def __init__(self) -> None:
        self._hooks: list[GatewayHook] = []

    def reload(self, root: Path | None = None) -> None:
        """Rediscover from disk, replacing the in-memory list atomically."""
        self._hooks = discover_hooks(root)
        logger.info(
            "gateway hooks: loaded %d hooks (%s)",
            len(self._hooks),
            ", ".join(h.name for h in self._hooks) or "<none>",
        )

    def hooks(self) -> list[GatewayHook]:
        return list(self._hooks)

    async def fire(
        self, event_type: str, context: dict[str, Any] | None = None,
    ) -> None:
        """Dispatch ``event_type`` to every subscribed hook.

        Handlers are launched concurrently; exceptions are caught + logged.
        Returns after all handlers complete (so the gateway can rely on
        ``await fire(...)`` to wait for hooks). For fire-and-forget,
        wrap the call in :func:`asyncio.create_task`.
        """
        ctx = dict(context or {})
        ctx.setdefault("event", event_type)
        targets = [h for h in self._hooks if h.matches(event_type)]
        if not targets:
            return
        results = await asyncio.gather(
            *(_run_one(h, event_type, ctx) for h in targets),
            return_exceptions=True,
        )
        for hook, res in zip(targets, results, strict=False):
            if isinstance(res, Exception):
                logger.warning(
                    "gateway hook %r raised on %s: %s",
                    hook.name, event_type, res,
                )


async def _run_one(
    hook: GatewayHook, event_type: str, ctx: dict[str, Any],
) -> None:
    if hook.handler is None:
        return
    await hook.handler(event_type, ctx)


# Module-level singleton — gateway code calls ``engine.fire(...)``.
engine = GatewayHookEngine()


__all__ = [
    "AGENT_END",
    "AGENT_START",
    "AGENT_STEP",
    "COMMAND_PREFIX",
    "GATEWAY_STARTUP",
    "KNOWN_EVENTS",
    "SESSION_END",
    "SESSION_RESET",
    "SESSION_START",
    "GatewayHook",
    "GatewayHookEngine",
    "discover_hooks",
    "engine",
    "hooks_root",
]
