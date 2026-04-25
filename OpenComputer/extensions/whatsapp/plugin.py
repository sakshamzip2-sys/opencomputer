"""WhatsApp channel plugin — entry point.

Outbound only via Meta Cloud API. Inbound: use webhook adapter (G.3)
wired to a Cloud API webhook.

Env vars: ``WHATSAPP_ACCESS_TOKEN`` (Bearer token from Meta Business
Suite) and ``WHATSAPP_PHONE_NUMBER_ID`` (Phone Number ID, NOT the phone
number itself). Disabled by default.
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import WhatsAppAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.whatsapp.adapter import WhatsAppAdapter  # package mode

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.whatsapp")


def register(api) -> None:  # PluginAPI duck-typed
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    if not token or not phone_id:
        logger.info(
            "whatsapp plugin: not registering "
            "(WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID unset)"
        )
        return
    adapter = WhatsAppAdapter(
        config={"access_token": token, "phone_number_id": phone_id}
    )
    api.register_channel(Platform.WEB.value, adapter)
    logger.info("whatsapp plugin: registered for phone_number_id=%s", phone_id)
