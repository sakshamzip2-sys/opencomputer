"""SMS channel plugin — entry point.

Reads Twilio + webhook env vars, instantiates :class:`SmsAdapter`,
registers via the :class:`PluginAPI` channel surface. Skips silently
when ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN`` are not set —
matches the Discord/Telegram pattern.
"""
from __future__ import annotations

import os

try:
    from adapter import SmsAdapter
except ImportError:  # pragma: no cover
    from extensions.sms.adapter import SmsAdapter

from plugin_sdk.core import Platform


def register(api) -> None:  # PluginAPI duck-typed
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not token:
        return
    config = {
        "account_sid": sid,
        "auth_token": token,
        "from_number": os.environ.get("TWILIO_PHONE_NUMBER", "").strip(),
        "webhook_port": int(os.environ.get("SMS_WEBHOOK_PORT", "8080")),
        "webhook_host": os.environ.get("SMS_WEBHOOK_HOST", "0.0.0.0"),
        "webhook_url": os.environ.get("SMS_WEBHOOK_URL", "").strip(),
        "insecure_no_signature": os.environ.get(
            "SMS_INSECURE_NO_SIGNATURE", ""
        ).lower() == "true",
    }
    adapter = SmsAdapter(config=config)
    api.register_channel(Platform.SMS.value, adapter)
