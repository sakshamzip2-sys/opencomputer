"""API Server channel plugin — entry point.

REST endpoint exposing the agent over HTTP. The endpoint shape:
``POST /v1/chat`` with Bearer auth and a JSON body of
``{session_id, message}``.

Env vars:
- ``API_SERVER_HOST``  — defaults to ``127.0.0.1``. Set to ``0.0.0.0``
  ONLY when you understand the auth model + have set a strong token.
- ``API_SERVER_PORT``  — defaults to ``18791``.
- ``API_SERVER_TOKEN`` — REQUIRED. Bearer token for the
  ``Authorization: Bearer <token>`` header.

Disabled by default; without a token, registration is a no-op.
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import APIServerAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.api_server.adapter import APIServerAdapter  # package mode

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.api_server")


def register(api) -> None:  # PluginAPI duck-typed
    token = os.environ.get("API_SERVER_TOKEN", "").strip()
    if not token:
        logger.info(
            "api-server plugin: not registering "
            "(API_SERVER_TOKEN unset — required for auth)"
        )
        return
    host = os.environ.get("API_SERVER_HOST", "127.0.0.1").strip()
    try:
        port = int(os.environ.get("API_SERVER_PORT", "18791"))
    except ValueError:
        logger.warning(
            "api-server plugin: API_SERVER_PORT must be an integer; "
            "falling back to default 18791"
        )
        port = 18791
    adapter = APIServerAdapter(
        config={"host": host, "port": port, "token": token}
    )
    api.register_channel(Platform.WEB.value, adapter)
    logger.info(
        "api-server plugin: registered on http://%s:%d/v1/chat", host, port
    )
