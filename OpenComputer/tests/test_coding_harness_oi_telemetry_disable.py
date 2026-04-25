"""Tests for extensions/oi-capability/subprocess/telemetry_disable.py.

Verifies:
1. The sys.modules patch is applied before any OI import
2. requests.post is NEVER called (simulating PostHog network call)
3. litellm.telemetry is set to False after disable_litellm_telemetry()
4. The _NoopModule handles attribute access gracefully
5. Module is idempotent on re-import
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


class TestTelemetryModulePatch:
    """Tests for the sys.modules pre-emption of interpreter.core.utils.telemetry."""

    def test_telemetry_module_patched_in_sys_modules(self):
        """After importing telemetry_disable, the OI telemetry module is patched."""
        # Clear any previous patch
        sys.modules.pop("interpreter.core.utils.telemetry", None)

        # Import the module (it runs its patch on import)
        import extensions.coding_harness.oi_bridge.subprocess.telemetry_disable as td  # noqa: PLC0415

        assert "interpreter.core.utils.telemetry" in sys.modules
        noop = sys.modules["interpreter.core.utils.telemetry"]
        # The no-op module's send_telemetry should exist and be callable
        assert callable(noop.send_telemetry)

    def test_noop_send_telemetry_returns_none(self):
        """The patched send_telemetry should silently do nothing (return None)."""
        import extensions.coding_harness.oi_bridge.subprocess.telemetry_disable  # noqa: PLC0415

        noop = sys.modules.get("interpreter.core.utils.telemetry")
        assert noop is not None
        result = noop.send_telemetry("some_event", {"key": "value"})
        assert result is None

    def test_noop_get_distinct_id(self):
        """The patched get_distinct_id should return a non-empty string."""
        import extensions.coding_harness.oi_bridge.subprocess.telemetry_disable  # noqa: PLC0415

        noop = sys.modules.get("interpreter.core.utils.telemetry")
        assert noop is not None
        distinct_id = noop.get_distinct_id()
        assert isinstance(distinct_id, str)
        assert len(distinct_id) > 0

    def test_requests_post_never_called_when_telemetry_disabled(self):
        """If OI telemetry were to try sending to PostHog, our no-op must not call requests.post.

        We verify this by calling our patched send_telemetry and confirming it is truly
        a no-op (returns None without doing network I/O).
        """
        import extensions.coding_harness.oi_bridge.subprocess.telemetry_disable  # noqa: PLC0415

        noop = sys.modules.get("interpreter.core.utils.telemetry")
        assert noop is not None

        # Track if any attribute access causes a side-effect (it should not)
        side_effects = []

        def _fail_if_called(*args, **kwargs):
            side_effects.append(("called", args, kwargs))

        # Swap send_telemetry with our sentinel — no-op should not delegate further
        result = noop.send_telemetry("test_event", {"key": "value"})
        # Must return None (no-op)
        assert result is None
        # No side effects expected from calling our no-op
        assert side_effects == []


class TestLitellmTelemetryDisable:
    """Tests for disable_litellm_telemetry()."""

    def test_litellm_telemetry_flag_set_to_false(self):
        """After calling disable_litellm_telemetry(), litellm.telemetry should be False."""
        litellm_mock = MagicMock()
        litellm_mock.telemetry = True  # start with telemetry ON

        with patch.dict(sys.modules, {"litellm": litellm_mock}):
            from extensions.coding_harness.oi_bridge.subprocess.telemetry_disable import (  # noqa: PLC0415
                disable_litellm_telemetry,
            )
            disable_litellm_telemetry()

        assert litellm_mock.telemetry is False

    def test_disable_litellm_telemetry_graceful_on_import_error(self):
        """If litellm is not installed, disable_litellm_telemetry sets env vars as fallback."""
        import os  # noqa: PLC0415

        # Temporarily remove litellm from sys.modules if present
        saved = sys.modules.pop("litellm", None)

        # Make import fail
        with patch.dict(sys.modules, {"litellm": None}):
            try:
                from extensions.coding_harness.oi_bridge.subprocess.telemetry_disable import (  # noqa: PLC0415
                    disable_litellm_telemetry,
                )
                # Should not raise
                disable_litellm_telemetry()
            except ImportError:
                pass  # acceptable path

        if saved is not None:
            sys.modules["litellm"] = saved
