"""Matrix channel plugin — entry point.

Outbound only via Client-Server API. Inbound: use webhook adapter (G.3)
wired to your homeserver's appservice / hookshot / matrix-bridge of
choice. Disabled by default.

Env vars: ``MATRIX_HOMESERVER`` (e.g. ``https://matrix.org``) and
``MATRIX_ACCESS_TOKEN``.
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import MatrixAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.matrix.adapter import MatrixAdapter  # package mode

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.matrix")


def register(api) -> None:  # PluginAPI duck-typed
    homeserver = os.environ.get("MATRIX_HOMESERVER", "").strip()
    token = os.environ.get("MATRIX_ACCESS_TOKEN", "").strip()
    if not homeserver or not token:
        logger.info(
            "matrix plugin: not registering (MATRIX_HOMESERVER or MATRIX_ACCESS_TOKEN unset)"
        )
        return
    adapter = MatrixAdapter(config={"homeserver": homeserver, "access_token": token})
    api.register_channel(Platform.WEB.value, adapter)
    logger.info("matrix plugin: registered for %s", homeserver)
