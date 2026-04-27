"""Signal channel plugin — entry point.

Outbound only via signal-cli's JSON-RPC HTTP daemon. Inbound: poll
signal-cli's /receive endpoint or wire the webhook adapter (G.3) to
signal-cli's HTTP receiver.

Env vars: ``SIGNAL_CLI_URL`` (e.g. ``http://localhost:8080``) and
``SIGNAL_PHONE_NUMBER`` (E.164 — the number signal-cli is registered as).
Disabled by default.
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import SignalAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.signal.adapter import SignalAdapter  # package mode

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.signal")


def register(api) -> None:  # PluginAPI duck-typed
    url = os.environ.get("SIGNAL_CLI_URL", "").strip()
    phone = os.environ.get("SIGNAL_PHONE_NUMBER", "").strip()
    if not url or not phone:
        logger.info(
            "signal plugin: not registering "
            "(SIGNAL_CLI_URL or SIGNAL_PHONE_NUMBER unset)"
        )
        return
    adapter = SignalAdapter(
        config={"signal_cli_url": url, "phone_number": phone}
    )
    api.register_channel(Platform.SIGNAL.value, adapter)
    logger.info("signal plugin: registered for %s via %s", phone, url)
