"""Approvals config — Hermes-parity ``security.approvals.{mode,timeout}``.

Maps the Hermes ``approvals.mode: manual|smart|off`` knob into
OpenComputer's idioms:

| Hermes mode | OC behaviour |
|---|---|
| ``manual`` (default) | standard consent gate flow (PER_ACTION tier prompts) |
| ``off`` | equivalent to ``--auto``: auto-allow consent prompts at the session level |
| ``smart`` | parsed but not yet implemented — falls back to ``manual`` and logs a one-shot warning |

``timeout`` overrides the consent gate's default 300s wait.

Config-schema independence: like
:mod:`opencomputer.security.website_blocklist`, this module reads YAML
directly rather than going through the central ``SecurityConfig``
dataclass. Independence from concurrent ``security.*`` schema PRs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("opencomputer.security.approvals")


VALID_MODES: frozenset[str] = frozenset({"manual", "smart", "off"})
DEFAULT_MODE: str = "manual"
DEFAULT_TIMEOUT_S: float = 300.0  # 5 minutes — matches consent gate default


@dataclass(frozen=True, slots=True)
class ApprovalsConfig:
    """Resolved approvals settings — what callers consult.

    Attributes:
        mode: one of ``manual``, ``smart``, ``off``. ``smart`` currently
            falls back to ``manual`` (logged once); future PRs will wire
            an auxiliary LLM risk assessor.
        timeout_s: seconds the consent gate waits for a user response
            before auto-denying. Mirrors the Hermes
            ``approvals.timeout`` knob.
    """

    mode: str = DEFAULT_MODE
    timeout_s: float = DEFAULT_TIMEOUT_S

    @property
    def auto_allow(self) -> bool:
        """True iff the caller should treat consent prompts as pre-approved.

        Mirrors ``--auto`` / OC's existing yolo_mode semantics. Intended
        for callers that want a single-question "should I prompt or not"
        without each rebuilding the mode-vs-flag logic.
        """
        return self.mode == "off"


def parse_mode(raw: object) -> str:
    """Normalise a raw config value into a known mode name.

    Unknown / missing → :data:`DEFAULT_MODE`. ``smart`` is accepted but
    logged once as "not yet wired". PyYAML quirk: unquoted ``off`` /
    ``no`` parse as boolean False, so both False and the string ``off``
    are honoured equally.
    """
    # YAML quirk: unquoted ``off`` parses as boolean False.
    if raw is False:
        return "off"
    if raw is True:
        # ``on`` / ``yes`` don't map to any mode in Hermes — user almost
        # certainly meant ``manual``. Don't silently miscoerce.
        logger.warning(
            "security.approvals.mode parsed as boolean True (likely "
            "unquoted 'on'/'yes'); falling back to %r. Quote the value "
            "in config.yaml if you meant something specific.",
            DEFAULT_MODE,
        )
        return DEFAULT_MODE
    if not isinstance(raw, str):
        return DEFAULT_MODE
    candidate = raw.strip().lower()
    if candidate not in VALID_MODES:
        logger.warning(
            "security.approvals.mode=%r is unknown; falling back to %r. "
            "Valid: %s",
            raw, DEFAULT_MODE, ", ".join(sorted(VALID_MODES)),
        )
        return DEFAULT_MODE
    if candidate == "smart":
        logger.warning(
            "security.approvals.mode=smart is recognised but not yet "
            "wired — auxiliary LLM risk assessor lands in a future PR. "
            "Falling back to 'manual' for this session.",
        )
        return "manual"
    return candidate


def parse_timeout(raw: object) -> float:
    """Normalise a raw config value into a float seconds value.

    Non-numeric / missing → :data:`DEFAULT_TIMEOUT_S`. Negative values
    are clamped to 1.0 (a 0-or-negative timeout would auto-deny every
    prompt instantly which is not what any user means).
    """
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if v <= 0:
        return 1.0
    return v


def load_approvals_from_active_config() -> ApprovalsConfig:
    """Read ``security.approvals.{mode,timeout}`` from the active profile's
    ``config.yaml``.

    On any error returns the safe default (``manual``, 300s). This is
    the public hot-path callers should use.
    """
    try:
        import yaml

        from opencomputer.profiles import (
            profile_home_dir,
            read_active_profile,
        )

        prof = read_active_profile()
        if prof is None:
            return ApprovalsConfig()
        config_path = profile_home_dir(prof) / "config.yaml"
        if not config_path.exists():
            return ApprovalsConfig()
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        appr = (data.get("security") or {}).get("approvals") or {}
        return ApprovalsConfig(
            mode=parse_mode(appr.get("mode")),
            timeout_s=parse_timeout(appr.get("timeout")),
        )
    except Exception:  # noqa: BLE001
        return ApprovalsConfig()


__all__ = [
    "DEFAULT_MODE",
    "DEFAULT_TIMEOUT_S",
    "VALID_MODES",
    "ApprovalsConfig",
    "load_approvals_from_active_config",
    "parse_mode",
    "parse_timeout",
]
