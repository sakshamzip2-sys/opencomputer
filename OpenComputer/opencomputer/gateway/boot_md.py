"""``BOOT.md`` startup-instructions handler (Hermes Doc-2 community pattern).

Drop a ``~/.opencomputer/BOOT.md`` file with natural-language startup
instructions; the gateway reads it on ``gateway:startup`` and runs a
**one-shot** :class:`AIAgent`-equivalent to execute them. Use the
``[SILENT]`` token as the response when nothing needs attention so
the gateway doesn't deliver a meaningless "Hello, I'm an AI ..." reply.

The handler:

* Reads ``BOOT.md`` if present.
* Resolves the gateway's configured model + provider (no bare defaults
  — the boot agent uses the same credentials the gateway uses).
* Spawns a one-shot ``run_conversation`` against an isolated session
  id so the boot's tool calls don't leak into a user-facing session.
* Logs the response (or notes ``[SILENT]``).

This is intentionally minimal — power users wanting more capability
should write a proper plugin or gateway HOOK.yaml hook with their own
agent loop. BOOT.md is the lightweight "drop a file, get init"
ergonomic.

This module is wired up via ``register_boot_md_hook`` into the gateway
file-discovery hook engine; users do NOT need to drop a HOOK.yaml of
their own to enable it. They drop BOOT.md and it's picked up.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from opencomputer.gateway.event_hooks import GATEWAY_STARTUP, GatewayHookEngine

logger = logging.getLogger("opencomputer.gateway.boot_md")


SILENT_MARKER = "[SILENT]"


def boot_md_path() -> Path:
    home_env = os.environ.get("OPENCOMPUTER_HOME")
    base = Path(home_env) if home_env else Path.home() / ".opencomputer"
    return base / "BOOT.md"


async def _read_boot_md() -> str | None:
    """Return the BOOT.md text, or None if absent / empty / unreadable."""
    path = boot_md_path()
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None
    return text or None


async def boot_md_handler(event_type: str, context: dict[str, Any]) -> None:
    """``gateway:startup`` handler — runs BOOT.md instructions if present.

    Intentionally tolerant: any missing dependency (no provider
    configured, no LLM client importable, AIAgent unavailable) logs a
    warning + returns. The gateway must NOT block on BOOT.md.
    """
    if event_type != GATEWAY_STARTUP:
        return  # defensive — handler is only registered for startup
    text = await _read_boot_md()
    if text is None:
        return  # silent no-op when BOOT.md is absent

    logger.info("BOOT.md detected — running startup instructions")
    started = time.monotonic()

    # Resolve the gateway's model + provider — pattern from
    # opencomputer/agent/aux_llm.py so we inherit the user's auth.
    try:
        from opencomputer.agent.aux_llm import complete_text
    except Exception:  # noqa: BLE001 — never block the gateway
        logger.warning("aux_llm unavailable — BOOT.md skipped")
        return

    boot_session_id = f"boot-{uuid.uuid4().hex[:8]}"
    system = (
        "You are the OpenComputer gateway running BOOT.md startup "
        "instructions. Execute them concisely. If nothing requires "
        "user attention, respond with ONLY the literal string "
        f"{SILENT_MARKER!r} so the gateway suppresses delivery."
    )

    try:
        response = await complete_text(
            messages=[{"role": "user", "content": text}],
            system=system,
            max_tokens=2000,
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001 — log + return; never crash
        logger.warning(
            "BOOT.md model call failed (boot session %s): %s",
            boot_session_id, exc,
        )
        return

    elapsed = time.monotonic() - started
    response = (response or "").strip()
    if response == SILENT_MARKER or not response:
        logger.info(
            "BOOT.md ran silently in %.1fs (session %s)",
            elapsed, boot_session_id,
        )
        return
    logger.info(
        "BOOT.md ran in %.1fs (session %s) — response: %s",
        elapsed, boot_session_id,
        response if len(response) <= 200 else response[:200] + "...",
    )


def register_boot_md_hook(engine: GatewayHookEngine) -> None:
    """Wire the BOOT.md handler into the gateway hook engine.

    Called from ``Gateway.start`` AFTER ``engine.reload()`` so the
    user's filesystem-discovered hooks are loaded first; BOOT.md
    runs alongside them.
    """
    from opencomputer.gateway.event_hooks import GatewayHook

    engine._hooks.append(  # noqa: SLF001 — controlled internal append
        GatewayHook(
            name="__boot_md__",
            path=boot_md_path().parent,
            events=[GATEWAY_STARTUP],
            handler=boot_md_handler,
            description="BOOT.md community-pattern startup runner",
        )
    )


__all__ = [
    "SILENT_MARKER",
    "boot_md_handler",
    "boot_md_path",
    "register_boot_md_hook",
]
