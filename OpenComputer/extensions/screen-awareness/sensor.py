"""ScreenAwarenessSensor — orchestrates capture, dedup, filter, ring append.

Single entry point: ``capture_now(session_id, trigger, tool_call_id=None)``.
Returns the resulting ScreenCapture or None if any guard skipped capture.

Guards (in order):
1. Cooldown — skip if last capture was within ``cooldown_seconds``.
2. Lock detect — skip if screen is locked / asleep.
3. Sensitive-app filter — skip if foreground app matches denylist.
4. OCR failure — log + skip.

On success, hashes OCR text, dedupes against last entry, appends to ring.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable

from .ring_buffer import ScreenCapture, ScreenRingBuffer, TriggerSource

_log = logging.getLogger("opencomputer.screen_awareness.sensor")

#: Default cooldown — 1s minimum between captures.
DEFAULT_COOLDOWN_SECONDS = 1.0

#: Type of the optional foreground-app callback injected by the host.
#: Returns the active app name (best-effort) or empty string.
ForegroundAppCallback = Callable[[], str]


class ScreenAwarenessSensor:
    """Capture orchestrator. Threads in dependencies as injectable methods
    so tests can mock without monkey-patching the import graph.

    ``foreground_app_callback`` (optional, kw-only) lets the host wire a
    foreground-app source at registration time without screen-awareness
    importing across the plugin boundary. When None, the sensitive-app
    filter sees an empty app name (still works on supplied input via
    text-content matching, but no active-window context).
    """

    def __init__(
        self,
        ring_buffer: ScreenRingBuffer,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        *,
        foreground_app_callback: ForegroundAppCallback | None = None,
    ) -> None:
        self._ring = ring_buffer
        self._cooldown = cooldown_seconds
        self._last_capture_at = 0.0
        self._lock = threading.Lock()
        self._foreground_cb = foreground_app_callback

    # ─── Injectable dependency boundaries (mocked in tests) ────────────

    def _ocr_screen(self) -> str:
        """OCR the primary monitor. Raises on capture / OCR failure.

        Uses the inline OCR helper rather than reaching into
        coding-harness — cross-plugin boundary stays clean.
        """
        from .ocr_inline import ocr_text_from_screen

        return ocr_text_from_screen()

    def _is_locked(self) -> bool:
        from .lock_detect import is_screen_locked

        return is_screen_locked()

    def _foreground_app_name(self) -> str:
        """Best-effort foreground app — empty string by default.

        If a callback was injected at construction time (e.g. by
        plugin.py wiring ambient-sensors's foreground sampler via
        ``importlib.import_module``), call it. Any callback exception
        is swallowed and treated as "no info" so the sensor never
        crashes on a misbehaving callback.
        """
        if self._foreground_cb is None:
            return ""
        try:
            return self._foreground_cb() or ""
        except Exception:  # noqa: BLE001 — never let a callback wedge the sensor
            return ""

    def _is_sensitive(self, app_name: str) -> bool:
        """True iff the active app name OR the OCR text suggests a
        sensitive context. ``app_name`` is the value returned from
        :meth:`_foreground_app_name` (empty by default for v1)."""
        from .sensitive_apps import is_app_sensitive

        return is_app_sensitive(app_name)

    # ─── Public capture ────────────────────────────────────────────────

    def capture_now(
        self,
        *,
        session_id: str,
        trigger: TriggerSource,
        tool_call_id: str | None = None,
    ) -> ScreenCapture | None:
        """Capture, dedupe, filter, append. Returns the ScreenCapture
        appended to the ring, or the cached latest if cooldown/dedup
        suppressed a new append, or None if a guard skipped.
        """
        now = time.time()

        # Cooldown — return the cached latest so caller still has a
        # capture to work with, but don't take a fresh OCR.
        with self._lock:
            since_last = now - self._last_capture_at
        if since_last < self._cooldown:
            _log.debug(
                "cooldown active (%.2fs since last) — reusing latest", since_last
            )
            return self._ring.latest()

        if self._is_locked():
            _log.info("screen locked — capture skipped")
            return None

        try:
            app_name = self._foreground_app_name()
        except Exception:  # noqa: BLE001
            app_name = ""
        if app_name and self._is_sensitive(app_name):
            _log.info("sensitive app in foreground — capture skipped")
            return None

        try:
            text = self._ocr_screen()
        except Exception:  # noqa: BLE001
            _log.warning("OCR failed — capture skipped", exc_info=True)
            return None

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # Dedup against last entry
        latest = self._ring.latest()
        if latest is not None and latest.sha256 == digest:
            _log.debug("identical OCR — dedup, no new append")
            with self._lock:
                self._last_capture_at = now
            return latest

        cap = ScreenCapture(
            captured_at=now,
            text=text,
            sha256=digest,
            trigger=trigger,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
        self._ring.append(cap)
        with self._lock:
            self._last_capture_at = now
        return cap


__all__ = ["DEFAULT_COOLDOWN_SECONDS", "ScreenAwarenessSensor"]
