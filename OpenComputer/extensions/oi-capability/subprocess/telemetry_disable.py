"""Telemetry kill-switch for the OI subprocess.

MUST be the FIRST import in subprocess/server.py — before any 'from interpreter'.

Pre-empts the PostHog telemetry module by registering a no-op replacement in
``sys.modules`` before Open Interpreter can import it. OI's telemetry module
lives at ``interpreter.core.utils.telemetry`` and is imported by multiple OI
sub-modules at startup; patching ``sys.modules`` ensures every subsequent
``from interpreter.core.utils import telemetry`` or
``from interpreter.core.utils.telemetry import send_telemetry``
receives the no-op version.

Also disables litellm telemetry (OI's LLM routing layer), which has its own
separate usage-data collection.
"""

from __future__ import annotations

import sys


class _NoopTelemetry:
    @staticmethod
    def send_telemetry(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    @staticmethod
    def get_distinct_id() -> str:
        return "opencomputer-subprocess-noop"


class _NoopModule:
    """Drop-in replacement for interpreter.core.utils.telemetry."""

    send_telemetry = staticmethod(lambda *a, **k: None)
    get_distinct_id = staticmethod(lambda: "opencomputer-subprocess-noop")

    # Some OI versions access posthog client directly
    posthog = None

    def __getattr__(self, name: str):  # noqa: ANN001
        # Any attribute access returns a no-op callable or None
        return lambda *a, **k: None


# Register before any OI import — this is the kill-switch
_noop = _NoopModule()
sys.modules["interpreter.core.utils.telemetry"] = _noop  # type: ignore[assignment]
# Belt-and-suspenders: also patch parent module path variants
sys.modules["interpreter.core.utils"] = _noop  # type: ignore[assignment]


def disable_litellm_telemetry() -> None:
    """Disable litellm's own telemetry collection.

    Call this after the sys.modules patch above but before any litellm import.
    If litellm is already imported (e.g. OI pulled it in), patch the live module.
    """
    try:
        import litellm  # noqa: PLC0415

        litellm.telemetry = False
        # litellm may expose a helper to turn off message logging
        turn_off = getattr(litellm, "_turn_off_message_logging", None)
        if callable(turn_off):
            turn_off()
    except ImportError:
        # litellm not yet installed — set env var as a fallback hint
        import os  # noqa: PLC0415

        os.environ["LITELLM_TELEMETRY"] = "False"
        os.environ["LITELLM_LOG"] = "ERROR"
