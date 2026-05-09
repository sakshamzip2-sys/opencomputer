"""API Server channel plugin — entry point.

REST endpoint exposing the agent over HTTP. The endpoint shape:
``POST /v1/chat`` with Bearer auth and a JSON body of
``{session_id, message}``.

Env vars:
- ``API_SERVER_HOST``  — defaults to ``127.0.0.1``. Set to ``0.0.0.0``
  ONLY when you understand the auth model + have set a strong token.
- ``API_SERVER_PORT``  — defaults to ``18791``.
- ``API_SERVER_TOKEN`` — Bearer token for the
  ``Authorization: Bearer <token>`` header.
- ``API_SERVER_KEY``   — Hermes-spec alias for ``API_SERVER_TOKEN``
  (G6, 2026-05-09). ``_TOKEN`` wins when both are set.
- ``API_SERVER_ENABLED`` — Hermes-spec opt-in flag (G7, 2026-05-09).
  ``true|1|yes|on`` auto-enables registration even when no token is
  set (the registration still no-ops if no token is found, since auth
  is mandatory). Returns ``None`` when unset → caller defers to
  profile plugin-enable list.

Disabled by default; without a token, registration is a no-op.
"""

from __future__ import annotations

import logging
import os

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.api_server")


def _resolve_api_server_config() -> dict:
    """Build the {host, port, token} dict from env vars.

    Hermes parity G6 (2026-05-09): ``API_SERVER_TOKEN`` (OC) takes
    precedence; ``API_SERVER_KEY`` (Hermes spec) is accepted as a
    fallback. Either being set is sufficient.
    """
    token = (
        os.environ.get("API_SERVER_TOKEN", "").strip()
        or os.environ.get("API_SERVER_KEY", "").strip()
    )
    host = os.environ.get("API_SERVER_HOST", "127.0.0.1").strip()
    try:
        port = int(os.environ.get("API_SERVER_PORT", "18791"))
    except ValueError:
        logger.warning(
            "api-server plugin: API_SERVER_PORT must be an integer; "
            "falling back to default 18791"
        )
        port = 18791
    return {"token": token, "host": host, "port": port}


def _is_api_server_enabled() -> bool | None:
    """Hermes parity G7 (2026-05-09): API_SERVER_ENABLED env override.

    Returns ``True``/``False`` when env is set; ``None`` when unset,
    so callers can defer to the profile's plugin-enable list.
    """
    raw = os.environ.get("API_SERVER_ENABLED", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def register(api) -> None:  # PluginAPI duck-typed
    cfg = _resolve_api_server_config()
    token = cfg["token"]
    if not token:
        logger.info(
            "api-server plugin: not registering "
            "(API_SERVER_TOKEN/API_SERVER_KEY unset — required for auth)"
        )
        return
    # Lazy adapter import — keeps the helpers (_resolve_api_server_config,
    # _is_api_server_enabled) testable in isolation without aiohttp setup.
    try:
        from adapter import APIServerAdapter  # plugin-loader mode
    except ImportError:  # pragma: no cover
        from extensions.api_server.adapter import (
            APIServerAdapter,  # package mode
        )
    adapter = APIServerAdapter(config=cfg)
    api.register_channel(Platform.WEB.value, adapter)
    logger.info(
        "api-server plugin: registered on http://%s:%d/v1/chat",
        cfg["host"],
        cfg["port"],
    )
