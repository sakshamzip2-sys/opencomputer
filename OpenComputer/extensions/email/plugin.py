"""Email channel plugin — entry point.

Disabled by default; user enables in their profile + sets the env vars below.

Env vars consumed (all required):

- ``EMAIL_IMAP_HOST`` (e.g. ``imap.gmail.com``)
- ``EMAIL_USERNAME`` (full email address)
- ``EMAIL_PASSWORD`` (Gmail App Password if 2FA enabled)

Optional:

- ``EMAIL_IMAP_PORT`` (default 993)
- ``EMAIL_SMTP_HOST`` (defaults to IMAP host)
- ``EMAIL_SMTP_PORT`` (default 465)
- ``EMAIL_FROM_ADDRESS`` (defaults to username)
- ``EMAIL_POLL_INTERVAL`` (default 60s)
- ``EMAIL_MAILBOX`` (default INBOX)
- ``EMAIL_ALLOWED_SENDERS`` (comma-separated list; if empty, no filtering)
"""

from __future__ import annotations

import logging
import os

try:
    from adapter import EmailAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.email.adapter import EmailAdapter  # package mode

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.ext.email")


def register(api) -> None:  # PluginAPI duck-typed
    host = os.environ.get("EMAIL_IMAP_HOST", "").strip()
    user = os.environ.get("EMAIL_USERNAME", "").strip()
    pw = os.environ.get("EMAIL_PASSWORD", "").strip()
    if not host or not user or not pw:
        logger.info(
            "email plugin: not registering (missing EMAIL_IMAP_HOST / EMAIL_USERNAME / EMAIL_PASSWORD)"
        )
        return

    config: dict = {
        "imap_host": host,
        "imap_port": int(os.environ.get("EMAIL_IMAP_PORT", "993")),
        "smtp_host": os.environ.get("EMAIL_SMTP_HOST") or host,
        "smtp_port": int(os.environ.get("EMAIL_SMTP_PORT", "465")),
        "username": user,
        "password": pw,
        "from_address": os.environ.get("EMAIL_FROM_ADDRESS") or user,
        "poll_interval_seconds": float(os.environ.get("EMAIL_POLL_INTERVAL", "60")),
        "mailbox": os.environ.get("EMAIL_MAILBOX", "INBOX"),
    }
    allowed = (os.environ.get("EMAIL_ALLOWED_SENDERS") or "").strip()
    if allowed:
        config["allowed_senders"] = [a.strip() for a in allowed.split(",") if a.strip()]

    adapter = EmailAdapter(config=config)
    api.register_channel(Platform.EMAIL.value, adapter)
    logger.info("email plugin: registered for %s on %s", user, host)
