"""Typed ErrorCode enum + WireResponse.code field.

Sub-project G (openclaw-parity) Task 9. Programmable error categories
for wire responses; old clients that only read ``error: str`` still
parse.
"""

from __future__ import annotations

import re

from opencomputer.gateway.error_codes import ErrorCode
from opencomputer.gateway.protocol import WireResponse


class TestErrorCode:
    def test_value_is_string(self) -> None:
        assert ErrorCode.PLUGIN_NOT_FOUND.value == "plugin_not_found"
        assert isinstance(ErrorCode.PLUGIN_NOT_FOUND.value, str)

    def test_str_enum_compares_to_string(self) -> None:
        # StrEnum semantics: enum value == its string value.
        assert ErrorCode.TOOL_DENIED == "tool_denied"

    def test_all_codes_lowercase_snake(self) -> None:
        for code in ErrorCode:
            assert re.match(r"^[a-z][a-z0-9_]*$", code.value), (
                f"{code.name}={code.value!r} not snake_case"
            )

    def test_codes_cover_expected_categories(self) -> None:
        names = {c.name for c in ErrorCode}
        for required in (
            "PLUGIN_NOT_FOUND",
            "PLUGIN_INCOMPATIBLE",
            "PROVIDER_AUTH_FAILED",
            "TOOL_DENIED",
            "CONSENT_BLOCKED",
            "METHOD_NOT_FOUND",
            "INVALID_PARAMS",
            "INTERNAL_ERROR",
            "RATE_LIMITED",
            "SESSION_NOT_FOUND",
        ):
            assert required in names, f"missing ErrorCode.{required}"


class TestWireResponseCode:
    def test_default_code_none(self) -> None:
        r = WireResponse(id="1", ok=True)
        assert r.code is None

    def test_explicit_code_persists(self) -> None:
        r = WireResponse(
            id="1",
            ok=False,
            error="not found",
            code=ErrorCode.PLUGIN_NOT_FOUND.value,
        )
        assert r.code == "plugin_not_found"

    def test_back_compat_old_response_without_code_parses(self) -> None:
        r = WireResponse(id="1", ok=False, error="boom")
        assert r.code is None
        assert r.error == "boom"

    def test_round_trip_through_dict(self) -> None:
        r = WireResponse(
            id="1",
            ok=False,
            error="x",
            code=ErrorCode.TOOL_DENIED.value,
        )
        d = r.model_dump()
        r2 = WireResponse.model_validate(d)
        assert r2.code == "tool_denied"
