"""BypassManager honours AUTO permission mode (closes a pre-existing gap)."""

from __future__ import annotations

import os
from unittest.mock import patch

from opencomputer.agent.consent.bypass import BypassManager
from plugin_sdk import PermissionMode, RuntimeContext


class TestBypassWithoutRuntime:
    def test_env_var_bypass_unchanged(self) -> None:
        with patch.dict(os.environ, {"OPENCOMPUTER_CONSENT_BYPASS": "1"}):
            assert BypassManager.is_active() is True

    def test_no_env_no_runtime_not_active(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCOMPUTER_CONSENT_BYPASS", None)
            assert BypassManager.is_active() is False


class TestBypassWithRuntime:
    def _clear_env(self) -> None:
        os.environ.pop("OPENCOMPUTER_CONSENT_BYPASS", None)

    def test_default_mode_not_bypass(self) -> None:
        self._clear_env()
        rt = RuntimeContext()
        assert BypassManager.is_active(rt) is False

    def test_plan_mode_not_bypass(self) -> None:
        self._clear_env()
        rt = RuntimeContext(permission_mode=PermissionMode.PLAN)
        assert BypassManager.is_active(rt) is False

    def test_accept_edits_not_bypass(self) -> None:
        # accept-edits auto-approves edits via a hook, not a gate bypass.
        self._clear_env()
        rt = RuntimeContext(permission_mode=PermissionMode.ACCEPT_EDITS)
        assert BypassManager.is_active(rt) is False

    def test_auto_mode_bypasses(self) -> None:
        self._clear_env()
        rt = RuntimeContext(permission_mode=PermissionMode.AUTO)
        assert BypassManager.is_active(rt) is True

    def test_legacy_yolo_mode_bypasses(self) -> None:
        # Backwards-compat: --yolo CLI flag still bypasses.
        self._clear_env()
        rt = RuntimeContext(yolo_mode=True)
        assert BypassManager.is_active(rt) is True

    def test_legacy_yolo_session_bypasses(self) -> None:
        # Backwards-compat: /yolo on still bypasses.
        self._clear_env()
        rt = RuntimeContext(custom={"yolo_session": True})
        assert BypassManager.is_active(rt) is True
