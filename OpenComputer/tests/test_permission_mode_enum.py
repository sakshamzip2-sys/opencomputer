"""PermissionMode enum + effective_permission_mode() helper."""

from __future__ import annotations

import pytest

from plugin_sdk import (
    PermissionMode,
    RuntimeContext,
    effective_permission_mode,
)


class TestPermissionModeEnum:
    def test_four_canonical_values(self) -> None:
        assert PermissionMode.DEFAULT.value == "default"
        assert PermissionMode.PLAN.value == "plan"
        assert PermissionMode.ACCEPT_EDITS.value == "accept-edits"
        assert PermissionMode.AUTO.value == "auto"

    def test_string_enum(self) -> None:
        # StrEnum so it serializes as the string value.
        assert str(PermissionMode.AUTO) == "auto"

    def test_round_trip_from_value(self) -> None:
        assert PermissionMode("accept-edits") is PermissionMode.ACCEPT_EDITS


class TestEffectivePermissionModeResolution:
    def test_default_when_nothing_set(self) -> None:
        rt = RuntimeContext()
        assert effective_permission_mode(rt) == PermissionMode.DEFAULT

    def test_legacy_plan_field(self) -> None:
        rt = RuntimeContext(plan_mode=True)
        assert effective_permission_mode(rt) == PermissionMode.PLAN

    def test_legacy_yolo_field(self) -> None:
        rt = RuntimeContext(yolo_mode=True)
        assert effective_permission_mode(rt) == PermissionMode.AUTO

    def test_new_field_overrides_legacy(self) -> None:
        rt = RuntimeContext(
            plan_mode=True,
            permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        assert effective_permission_mode(rt) == PermissionMode.ACCEPT_EDITS

    def test_legacy_custom_plan(self) -> None:
        rt = RuntimeContext(custom={"plan_mode": True})
        assert effective_permission_mode(rt) == PermissionMode.PLAN

    def test_legacy_custom_yolo_session(self) -> None:
        rt = RuntimeContext(custom={"yolo_session": True})
        assert effective_permission_mode(rt) == PermissionMode.AUTO

    def test_legacy_custom_accept_edits(self) -> None:
        rt = RuntimeContext(custom={"accept_edits": True})
        assert effective_permission_mode(rt) == PermissionMode.ACCEPT_EDITS

    def test_canonical_custom_wins_over_legacy(self) -> None:
        rt = RuntimeContext(
            yolo_mode=True,
            custom={"permission_mode": "accept-edits", "yolo_session": True},
        )
        assert effective_permission_mode(rt) == PermissionMode.ACCEPT_EDITS

    def test_plan_wins_over_auto_on_conflict(self) -> None:
        # Matches existing CLI precedence (plan beats yolo).
        rt = RuntimeContext(plan_mode=True, yolo_mode=True)
        assert effective_permission_mode(rt) == PermissionMode.PLAN


class TestRuntimeContextStillFrozen:
    def test_cannot_mutate_field(self) -> None:
        rt = RuntimeContext()
        with pytest.raises(Exception):  # FrozenInstanceError
            rt.permission_mode = PermissionMode.AUTO  # type: ignore[misc]
