"""Delivery routing for cron jobs, /sethome auto-deliver, and inter-session
message routing.

PR-2 Task B5 of the messaging-gateway parity plan. Mirrors Hermes
``gateway/delivery.py`` semantics; we re-implement the shape here rather
than copying because OpenComputer's :class:`Platform` enum has no
``LOCAL`` member and the home-channel store lives at
``<profile>/gateway/home_channels.json`` (written by ``oc gateway sethome``).

Exposes:

* :class:`SessionSource` — minimal record of an inbound session origin
  used by :meth:`DeliveryTarget.parse` when the target string is
  ``"origin"``.
* :class:`DeliveryTarget` — frozen dataclass that parses and round-trips
  string forms of delivery destinations.
* :class:`DeliveryRouter` — async router that resolves
  :class:`DeliveryTarget` lists into ``adapter.send()`` calls,
  truncating oversized output and (by default) mirroring the sent text
  back into the recipient session via
  :func:`opencomputer.gateway.mirror.mirror_to_session`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from plugin_sdk.core import Platform

logger = logging.getLogger("opencomputer.gateway.delivery")


# Telegram caps a single message at ~4096 characters; pick conservative
# constants that fit every channel adapter's limit and reserve a margin
# for the ``[truncated, full output saved to <path>]`` suffix.
MAX_PLATFORM_OUTPUT = 4000
TRUNCATED_VISIBLE = 3800


# Sentinel string used for ``DeliveryTarget.platform`` when the target
# is ``"local"`` (file-only). :class:`Platform` has no ``LOCAL`` member;
# defining the sentinel here keeps comparisons string-equal so callers
# can do ``target.platform == LOCAL_PLATFORM`` regardless of provenance.
LOCAL_PLATFORM = "local"


@dataclass(frozen=True, slots=True)
class SessionSource:
    """Where an inbound session originated.

    Hermes' upstream :class:`SessionSource` carries more fields; we
    inline a minimal version here because :meth:`DeliveryTarget.parse`
    only needs ``platform``/``chat_id``/``thread_id`` to back-route an
    ``"origin"`` target.
    """

    platform: Platform
    chat_id: str | None = None
    thread_id: str | None = None
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """A single delivery target.

    Represents where a cron / agent message should be sent:

    * ``"origin"``                 — back to source (if origin given,
      else local)
    * ``"local"``                  — local files only
    * ``"<platform>"``             — that platform's home channel (set
      via ``oc gateway sethome``)
    * ``"<platform>:<chat>"``      — specific chat
    * ``"<platform>:<chat>:<thr>"``— specific thread within a chat

    Unknown platforms degrade to :data:`LOCAL_PLATFORM` so a typo in a
    cron job spec doesn't lose the output.
    """

    platform: Platform | str
    chat_id: str | None = None
    thread_id: str | None = None
    is_origin: bool = False
    is_explicit: bool = False

    @classmethod
    def parse(
        cls, target: str, origin: SessionSource | None = None,
    ) -> DeliveryTarget:
        """Parse a string spec into a :class:`DeliveryTarget`.

        See class docstring for the supported forms.
        """
        target_stripped = (target or "").strip()
        target_lower = target_stripped.lower()

        if target_lower == "origin":
            if origin is not None:
                return cls(
                    platform=origin.platform,
                    chat_id=origin.chat_id,
                    thread_id=origin.thread_id,
                    is_origin=True,
                )
            # Fallback to local when no origin available.
            return cls(platform=LOCAL_PLATFORM, is_origin=True)

        if target_lower == "local":
            return cls(platform=LOCAL_PLATFORM)

        # platform[:chat[:thread]] form
        if ":" in target_stripped:
            parts = target_stripped.split(":", 2)
            platform_str = parts[0].lower()
            chat_id = parts[1] if len(parts) > 1 else None
            thread_id = parts[2] if len(parts) > 2 else None
            try:
                platform = Platform(platform_str)
                return cls(
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    is_explicit=True,
                )
            except ValueError:
                return cls(platform=LOCAL_PLATFORM)

        # Bare platform name (use that platform's home channel).
        try:
            platform = Platform(target_lower)
            return cls(platform=platform)
        except ValueError:
            return cls(platform=LOCAL_PLATFORM)

    def to_string(self) -> str:
        """Round-trip the target back to its string form.

        ``"origin"`` is preserved when ``is_origin`` is True even if
        ``platform`` is a real platform — that's how downstream code
        re-recognises an origin echo.
        """
        if self.is_origin:
            return "origin"
        if self.platform == LOCAL_PLATFORM:
            return "local"
        platform_str = self.platform.value if isinstance(self.platform, Platform) else str(self.platform)
        if self.chat_id and self.thread_id:
            return f"{platform_str}:{self.chat_id}:{self.thread_id}"
        if self.chat_id:
            return f"{platform_str}:{self.chat_id}"
        return platform_str


class DeliveryRouter:
    """Resolves :class:`DeliveryTarget` lists to ``adapter.send()`` calls.

    Used by cron jobs, ``/sethome`` auto-deliver, and inter-session
    routing. Truncates platform output that exceeds
    :data:`MAX_PLATFORM_OUTPUT` to :data:`TRUNCATED_VISIBLE` chars
    plus a ``[truncated, full output saved to <path>]`` suffix; the
    full text is saved under ``<profile>/cron/output/`` so the user
    can recover it.

    When ``mirror=True`` (the default), every successful platform
    delivery is also mirrored into the recipient session's transcript
    via :func:`opencomputer.gateway.mirror.mirror_to_session` so the
    receiving-side agent has context for what was sent on its behalf.
    """

    def __init__(self, gateway: Any, mirror: bool = True) -> None:
        self._gateway = gateway
        self._mirror = mirror

    def _adapters_by_platform(self) -> dict[str, Any]:
        """Best-effort: pull live adapters off ``self._gateway``.

        We accept a few common shapes — ``gateway._adapters`` (list
        with ``adapter.platform`` attribute) and ``gateway.adapters``
        (property returning the same list). Falls back to an empty
        mapping if neither attribute is present.
        """
        for attr in ("_adapters", "adapters"):
            try:
                seq = getattr(self._gateway, attr)
            except AttributeError:
                continue
            if seq is None:
                continue
            try:
                # Property returning iterable, or a list-like attr.
                candidates = list(seq() if callable(seq) else seq)
            except TypeError:
                continue
            mapping: dict[str, Any] = {}
            for adapter in candidates:
                plat = getattr(adapter, "platform", None)
                if plat is None:
                    continue
                key = plat.value if isinstance(plat, Platform) else str(plat)
                mapping[key] = adapter
            if mapping:
                return mapping
        return {}

    def _profile_home(self) -> Path:
        """Resolve the active profile's home dir, the same way the CLI does.

        Lazily imports ``opencomputer.agent.config._home`` so this
        module stays cheap to import in test contexts that don't
        construct a full agent.
        """
        try:
            from opencomputer.agent.config import _home
            return _home()
        except Exception:  # noqa: BLE001 — fall back to env
            import os
            base = Path(
                os.environ.get(
                    "OPENCOMPUTER_HOME",
                    str(Path.home() / ".opencomputer"),
                ),
            )
            base.mkdir(parents=True, exist_ok=True)
            return base

    def _resolve_home_channel(
        self, platform: Platform,
    ) -> tuple[str | None, str | None]:
        """Look up ``<profile>/gateway/home_channels.json`` for ``platform``.

        Returns ``(chat_id, thread_id)`` (either may be ``None``).
        Returns ``(None, None)`` when the file is missing or the
        platform isn't set.
        """
        path = self._profile_home() / "gateway" / "home_channels.json"
        if not path.exists():
            return (None, None)
        try:
            mapping = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return (None, None)
        raw = mapping.get(platform.value)
        if not raw:
            return (None, None)
        # Stored as either ``"<chat>"`` or ``"<chat>:<thread>"`` (see
        # ``oc gateway sethome``).
        if ":" in raw:
            chat_id, thread_id = raw.split(":", 1)
            return (chat_id, thread_id)
        return (raw, None)

    def _save_full_output(self, content: str, source_label: str) -> Path:
        """Save full output to ``<profile>/cron/output/<label>_<ts>.txt``."""
        out_dir = self._profile_home() / "cron" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Sanitise label minimally — slashes / colons would break the
        # filename on Windows.
        safe = "".join(
            c if c.isalnum() or c in ("_", "-", ".") else "_"
            for c in (source_label or "manual")
        )[:32] or "manual"
        path = out_dir / f"{safe}_{ts}.txt"
        try:
            path.write_text(content, encoding="utf-8")
        except OSError:
            logger.exception("delivery: failed to save full output to %s", path)
        return path

    async def route(
        self,
        message_text: str,
        targets: list[DeliveryTarget],
        source_label: str = "manual",
    ) -> dict[str, bool]:
        """Send ``message_text`` to every target.

        Returns a mapping of ``target.to_string() -> success_bool``.
        Local-only targets always succeed (they don't go through any
        adapter); platform targets succeed iff ``adapter.send`` returned
        without error AND truthy ``success``.
        """
        adapters = self._adapters_by_platform()
        results: dict[str, bool] = {}

        for target in targets:
            label = target.to_string()
            try:
                if target.platform == LOCAL_PLATFORM:
                    # Local targets just save the file; never an error path.
                    self._save_full_output(message_text, source_label)
                    results[label] = True
                    continue

                if not isinstance(target.platform, Platform):
                    logger.warning(
                        "delivery: target %s has non-Platform value %r",
                        label, target.platform,
                    )
                    results[label] = False
                    continue

                chat_id = target.chat_id
                thread_id = target.thread_id
                if chat_id is None:
                    chat_id, home_thread = self._resolve_home_channel(
                        target.platform,
                    )
                    if not chat_id:
                        logger.warning(
                            "delivery: no home channel set for %s",
                            target.platform.value,
                        )
                        results[label] = False
                        continue
                    if thread_id is None:
                        thread_id = home_thread

                adapter = adapters.get(target.platform.value)
                if adapter is None:
                    logger.warning(
                        "delivery: no live adapter for platform %s",
                        target.platform.value,
                    )
                    results[label] = False
                    continue

                body = message_text
                if len(body) > MAX_PLATFORM_OUTPUT:
                    saved = self._save_full_output(body, source_label)
                    logger.info(
                        "delivery: truncating %d chars; full output → %s",
                        len(body), saved,
                    )
                    body = (
                        body[:TRUNCATED_VISIBLE]
                        + f"\n\n... [truncated, full output saved to {saved}]"
                    )

                kwargs: dict[str, Any] = {}
                if thread_id:
                    kwargs["thread_id"] = thread_id

                send_result = await adapter.send(chat_id, body, **kwargs)
                ok = bool(getattr(send_result, "success", True)) and not getattr(
                    send_result, "error", None,
                )
                results[label] = ok

                if ok and self._mirror:
                    # Best-effort mirror — never blocks delivery.
                    try:
                        from opencomputer.gateway.mirror import mirror_to_session
                        mirror_to_session(
                            platform=target.platform.value,
                            chat_id=chat_id,
                            message_text=message_text,
                            source_label=source_label,
                            thread_id=thread_id,
                        )
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "delivery: mirror_to_session swallowed error",
                            exc_info=True,
                        )
            except Exception:  # noqa: BLE001 — never let one target wedge the rest
                logger.exception(
                    "delivery: target %s raised; marking failed", label,
                )
                results[label] = False

        return results


__all__ = [
    "LOCAL_PLATFORM",
    "MAX_PLATFORM_OUTPUT",
    "TRUNCATED_VISIBLE",
    "DeliveryRouter",
    "DeliveryTarget",
    "SessionSource",
]
