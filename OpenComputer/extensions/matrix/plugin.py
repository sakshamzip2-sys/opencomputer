"""Matrix channel plugin — entry point.

Outbound + reactions + inbound /sync (Wave 6.E.3). The inbound /sync
loop is opt-in via env or config so existing outbound-only users see
zero behavior change.

Env vars:
  MATRIX_HOMESERVER          (e.g. ``https://matrix.org``)
  MATRIX_ACCESS_TOKEN        (long-lived bot token)
  MATRIX_INBOUND_SYNC=true   (opt in to /sync long-poll)
  MATRIX_CONSENT_HANDLER=true + MATRIX_CONSENT_CHAT_ID=!room:server
                             (Wave 6.E.7 — auto-wire the ConsentGate
                             matrix bridge so any tool gated by
                             ConsentGate.request_approval prompts the
                             user via a Matrix room reaction)
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


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def register(api) -> None:  # PluginAPI duck-typed
    homeserver = os.environ.get("MATRIX_HOMESERVER", "").strip()
    token = os.environ.get("MATRIX_ACCESS_TOKEN", "").strip()
    if not homeserver or not token:
        logger.info(
            "matrix plugin: not registering (MATRIX_HOMESERVER or MATRIX_ACCESS_TOKEN unset)"
        )
        return
    inbound_sync = _bool_env("MATRIX_INBOUND_SYNC")
    consent_enabled = _bool_env("MATRIX_CONSENT_HANDLER")
    consent_chat_id = os.environ.get("MATRIX_CONSENT_CHAT_ID", "").strip()

    config: dict = {
        "homeserver": homeserver,
        "access_token": token,
        # Consent handler implies inbound_sync — without /sync polling
        # no reaction can ever resolve a future, so we force-enable it
        # whenever the bridge is requested.
        "inbound_sync": inbound_sync or consent_enabled,
    }
    adapter = MatrixAdapter(config=config)
    api.register_channel(Platform.MATRIX.value, adapter)
    logger.info(
        "matrix plugin: registered for %s (inbound_sync=%s)",
        homeserver, config["inbound_sync"],
    )

    # Wave 6.E.7 — auto-install the consent bridge if enabled.
    if not consent_enabled:
        return
    if not consent_chat_id:
        logger.warning(
            "matrix plugin: MATRIX_CONSENT_HANDLER=true but "
            "MATRIX_CONSENT_CHAT_ID is empty — bridge NOT installed"
        )
        return
    try:
        try:
            from consent_bridge import (  # type: ignore[import-untyped]
                make_matrix_prompt_handler,
            )
        except ImportError:
            from extensions.matrix.consent_bridge import (
                make_matrix_prompt_handler,
            )
    except ImportError as exc:
        logger.warning(
            "matrix plugin: consent_bridge import failed (%s); "
            "bridge NOT installed",
            exc,
        )
        return

    # Read the gate the gateway bound onto the shared PluginAPI.
    # Without the gate we can't build a handler that ever resolves —
    # log + skip so the rest of the matrix surface still works.
    gate = getattr(api, "_consent_gate", None)
    if gate is None:
        logger.warning(
            "matrix plugin: api has no _consent_gate (gateway may "
            "not be running); consent handler NOT installed"
        )
        return
    handler = make_matrix_prompt_handler(
        gate=gate, adapter=adapter, chat_id=consent_chat_id,
    )
    api.set_consent_prompt_handler(handler)
    logger.info(
        "matrix plugin: ConsentGate bridge installed (chat_id=%s)",
        consent_chat_id,
    )
