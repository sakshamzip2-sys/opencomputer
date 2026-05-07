"""Allowlist gate — composes env-var allowlists, file overlay, and the
DM-pairing approved-store into one decision the dispatcher consults.

Resolution order:
    1. ``GATEWAY_ALLOW_ALL_USERS=true`` → allow always (escape hatch).
    2. ``<PLATFORM>_ALLOWED_USERS`` env (CSV) → allow if user_id member.
    3. ``GATEWAY_ALLOWED_USERS`` env (CSV) → catch-all.
    4. ``<profile>/allowlist.json`` overlay file.
    5. :class:`PairingCodeStore` approved-store.
    6. Default: deny + mint a fresh pairing code (subject to rate limits).

The dispatcher uses the returned :class:`AllowlistDecision` to decide
whether to dispatch the message or reply with a pairing-code prompt.

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T1.5)
Spec: docs/superpowers/specs/2026-05-08-messaging-gateway-parity-design.md (§5.16)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from opencomputer.channels.pairing_codes import PairingCodeStore

logger = logging.getLogger("opencomputer.channels.allowlist")

#: Per-platform env-var conventions matching Hermes spec.
PLATFORM_ENV_VARS: dict[str, str] = {
    "telegram":       "TELEGRAM_ALLOWED_USERS",
    "discord":        "DISCORD_ALLOWED_USERS",
    "slack":          "SLACK_ALLOWED_USERS",
    "signal":         "SIGNAL_ALLOWED_USERS",
    "sms":            "SMS_ALLOWED_USERS",
    "email":          "EMAIL_ALLOWED_USERS",
    "mattermost":     "MATTERMOST_ALLOWED_USERS",
    "matrix":         "MATRIX_ALLOWED_USERS",
    "dingtalk":       "DINGTALK_ALLOWED_USERS",
    "feishu":         "FEISHU_ALLOWED_USERS",
    "wecom":          "WECOM_ALLOWED_USERS",
    "wecom_callback": "WECOM_CALLBACK_ALLOWED_USERS",
    "whatsapp":       "WHATSAPP_ALLOWED_USERS",
    "weixin":         "WEIXIN_ALLOWED_USERS",
    "yuanbao":        "YUANBAO_ALLOWED_USERS",
    "qq":             "QQ_ALLOWED_USERS",
    "bluebubbles":    "BLUEBUBBLES_ALLOWED_USERS",
    "homeassistant":  "HOMEASSISTANT_ALLOWED_USERS",
    "irc":            "IRC_ALLOWED_USERS",
    "teams":          "TEAMS_ALLOWED_USERS",
}

ALLOW_ALL_ENV = "GATEWAY_ALLOW_ALL_USERS"
GLOBAL_ENV = "GATEWAY_ALLOWED_USERS"


# ── Decision dataclass ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AllowlistDecision:
    """Output of :meth:`AllowlistGate.check`.

    ``allowed`` is the only field the dispatcher must consult to gate
    delivery. ``source`` is for logs / observability. ``pairing_code``
    is populated when ``allowed=False`` and a fresh code was minted —
    the dispatcher uses it to format the bot's reply text.
    """

    allowed: bool
    source: str
    """One of: ``allow-all``, ``env-platform``, ``env-global``, ``file``,
    ``pairing-approved``, ``denied``."""
    pairing_code: Optional[str] = None
    user_id: str = ""


# ── Helpers ────────────────────────────────────────────────────────────────


def _parse_csv(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _truthy(s: str) -> bool:
    return s.lower() in ("1", "true", "yes", "on")


def _load_file_overlay(profile_home: Path) -> dict[str, list[str]]:
    """Return the optional ``<profile>/allowlist.json`` overlay.

    Schema: ``{"telegram": ["123", "456"], "discord": ["789"], ...}``.
    Missing or unreadable file → ``{}``. Never raises.
    """
    path = profile_home / "allowlist.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {k: list(v) for k, v in data.items() if isinstance(v, list)}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "allowlist.json unreadable at %s: %s — treating as empty", path, exc
        )
        return {}


# ── Gate ───────────────────────────────────────────────────────────────────


class AllowlistGate:
    """Composes env, file, and pairing sources into one decision."""

    def __init__(
        self,
        *,
        profile_home: Path,
        pairing_store: PairingCodeStore | None = None,
    ):
        self._profile_home = Path(profile_home)
        self._pairing = pairing_store or PairingCodeStore(profile_home)

    @property
    def pairing_store(self) -> PairingCodeStore:
        return self._pairing

    def check(
        self,
        platform: str,
        user_id: str,
        *,
        user_name: str = "",
    ) -> AllowlistDecision:
        platform = platform.lower()

        # 1) Global escape hatch.
        if _truthy(os.getenv(ALLOW_ALL_ENV, "")):
            return AllowlistDecision(True, "allow-all", user_id=user_id)

        # 2) Per-platform env.
        env_var = PLATFORM_ENV_VARS.get(platform)
        if env_var:
            allowed = _parse_csv(os.getenv(env_var, ""))
            if user_id in allowed:
                return AllowlistDecision(True, "env-platform", user_id=user_id)

        # 3) Catch-all env.
        global_allowed = _parse_csv(os.getenv(GLOBAL_ENV, ""))
        if user_id in global_allowed:
            return AllowlistDecision(True, "env-global", user_id=user_id)

        # 4) File overlay.
        file_overlay = _load_file_overlay(self._profile_home).get(platform, [])
        if user_id in file_overlay:
            return AllowlistDecision(True, "file", user_id=user_id)

        # 5) DM-pairing approvals.
        if self._pairing.is_approved(platform, user_id):
            return AllowlistDecision(True, "pairing-approved", user_id=user_id)

        # 6) Denied — mint a code (None if rate-limited / locked-out).
        code = self._pairing.generate_code(platform, user_id, user_name)
        return AllowlistDecision(
            False, "denied", pairing_code=code, user_id=user_id
        )


__all__ = [
    "PLATFORM_ENV_VARS",
    "ALLOW_ALL_ENV",
    "GLOBAL_ENV",
    "AllowlistDecision",
    "AllowlistGate",
]
