"""WhatsApp Baileys-bridge channel plugin — entry point (PR 6.2).

Spawns a local Node.js bridge (Baileys) on a configurable port and
talks HTTP to it. Outbound POSTs ``/send``; inbound is delivered via
long-polling ``/messages``. QR-code login flow surfaces the QR text as
a system MessageEvent so the user can scan it from any wired-up
channel.

Env vars:
* ``WHATSAPP_BRIDGE_ENABLED`` — set to ``1``/``true`` to enable
  registration. The plugin defaults to OFF because it shells out a
  Node.js subprocess.
* ``WHATSAPP_BRIDGE_PORT`` — TCP port for the bridge HTTP API
  (default ``3001``).
* ``WHATSAPP_BRIDGE_HOST`` — bind host (default ``127.0.0.1``).
* ``WHATSAPP_BRIDGE_AUTH_DIR`` — directory the Node bridge persists
  its session credentials to. Default
  ``~/.opencomputer/whatsapp-bridge``.

Coexistence: if ``extensions/whatsapp/`` (Cloud API) is also enabled,
the plugin logs a WARNING — only one should be active at a time, but
we don't crash.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

try:
    from adapter import WhatsAppBridgeAdapter  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.whatsapp_bridge.adapter import WhatsAppBridgeAdapter

logger = logging.getLogger("opencomputer.ext.whatsapp_bridge")


_TRUTHY = {"1", "true", "yes", "on"}


def _enabled() -> bool:
    """Bridge is OPT-IN — explicit env flag required to register."""
    raw = os.environ.get("WHATSAPP_BRIDGE_ENABLED", "")
    return raw.strip().lower() in _TRUTHY


def _default_auth_dir() -> Path:
    return Path(os.path.expanduser("~/.opencomputer/whatsapp-bridge"))


def register(api) -> None:  # PluginAPI duck-typed
    if not _enabled():
        logger.info(
            "whatsapp-bridge plugin: not registering "
            "(set WHATSAPP_BRIDGE_ENABLED=1 to enable)"
        )
        return

    # Coexistence warning: surface a clear log line if Cloud API is also
    # configured. We don't query the loader here (that would create a
    # circular dep); env-var presence is a good-enough proxy.
    if os.environ.get("WHATSAPP_ACCESS_TOKEN") and os.environ.get(
        "WHATSAPP_PHONE_NUMBER_ID"
    ):
        logger.warning(
            "whatsapp-bridge: extensions/whatsapp (Cloud API) env vars are "
            "ALSO set — both adapters will register. Pick one to keep; "
            "running both will produce duplicate inbound + outbound."
        )

    config = {
        "port": int(os.environ.get("WHATSAPP_BRIDGE_PORT", "3001")),
        "host": os.environ.get("WHATSAPP_BRIDGE_HOST", "127.0.0.1"),
        "auth_dir": os.environ.get(
            "WHATSAPP_BRIDGE_AUTH_DIR", str(_default_auth_dir())
        ),
        "bridge_dir": str(Path(__file__).resolve().parent / "bridge"),
    }
    adapter = WhatsAppBridgeAdapter(config=config)
    # Register under a distinct key so it can coexist with the Cloud
    # API adapter (also keyed on Platform.WHATSAPP.value). The key is
    # used purely as a dictionary index — gateway code looks up by
    # adapter.platform when routing inbound, not by registration key.
    api.register_channel("whatsapp_bridge", adapter)
    logger.info(
        "whatsapp-bridge plugin: registered (host=%s port=%s)",
        config["host"],
        config["port"],
    )
