"""Tests for G.31 — smart model fallback routing.

Covers:

1. ``is_transient_error`` classifier — true positives + true negatives.
2. ``call_with_fallback`` happy path / partial failure / full failure.
3. ``ModelConfig.fallback_models`` field defaults / accepts a tuple.

Streaming-mode fallback is intentionally not in scope (see module
docstring); this suite only covers the non-streaming helper.
"""

from __future__ import annotations

import pytest

from opencomputer.agent.config import ModelConfig
from opencomputer.agent.fallback import call_with_fallback, is_transient_error

# ---------------------------------------------------------------------------
# is_transient_error classifier
# ---------------------------------------------------------------------------


class TestIsTransient:
    @pytest.mark.parametrize(
        "msg",
        [
            "HTTP 429 Too Many Requests",
            "rate_limit_error",
            "rate limit hit",
            "HTTP 500 Internal Server Error",
            "HTTP 502 Bad Gateway",
            "HTTP 503 service_unavailable",
            "HTTP 504 Gateway Timeout",
            "model overloaded — try again",
            "connection refused",
            "connection reset by peer",
            "request timed out after 30s",
        ],
    )
    def test_transient_strings_detected(self, msg: str) -> None:
        assert is_transient_error(Exception(msg)) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "HTTP 401 unauthorized",  # auth — DON'T retry
            "HTTP 403 forbidden",  # auth — DON'T retry
            "HTTP 404 not found",  # nothing fallback fixes
            "invalid model name 'foo'",  # config bug
            "tool 'X' is not registered",  # plugin bug
            "code-429-special-day",  # bare 429 substring inside word
        ],
    )
    def test_non_transient_strings_not_flagged(self, msg: str) -> None:
        assert is_transient_error(Exception(msg)) is False


# ---------------------------------------------------------------------------
# call_with_fallback
# ---------------------------------------------------------------------------


class TestCallWithFallback:
    @pytest.mark.asyncio
    async def test_happy_path_no_fallback_needed(self) -> None:
        seen: list[str] = []

        async def call(model: str) -> str:
            seen.append(model)
            return f"ok-from-{model}"

        out = await call_with_fallback(
            call, primary_model="primary", fallback_models=("backup",)
        )
        assert out == "ok-from-primary"
        # Backup never invoked.
        assert seen == ["primary"]

    @pytest.mark.asyncio
    async def test_primary_fails_transient_then_backup_succeeds(self) -> None:
        seen: list[str] = []

        async def call(model: str) -> str:
            seen.append(model)
            if model == "primary":
                raise Exception("HTTP 429 Too Many Requests")
            return f"ok-from-{model}"

        out = await call_with_fallback(
            call, primary_model="primary", fallback_models=("backup",)
        )
        assert out == "ok-from-backup"
        assert seen == ["primary", "backup"]

    @pytest.mark.asyncio
    async def test_full_chain_failure_reraises_last_error(self) -> None:
        seen: list[str] = []

        async def call(model: str) -> str:
            seen.append(model)
            raise Exception(f"HTTP 503 from {model}")

        with pytest.raises(Exception, match="HTTP 503 from cheaper"):
            await call_with_fallback(
                call,
                primary_model="primary",
                fallback_models=("backup", "cheaper"),
            )
        assert seen == ["primary", "backup", "cheaper"]

    @pytest.mark.asyncio
    async def test_non_transient_error_short_circuits(self) -> None:
        seen: list[str] = []

        async def call(model: str) -> str:
            seen.append(model)
            raise Exception("HTTP 401 unauthorized")

        with pytest.raises(Exception, match="401"):
            await call_with_fallback(
                call, primary_model="primary", fallback_models=("backup",)
            )
        # Auth errors don't burn the fallback chain.
        assert seen == ["primary"]

    @pytest.mark.asyncio
    async def test_empty_fallback_collapses_to_single_call(self) -> None:
        seen: list[str] = []

        async def call(model: str) -> str:
            seen.append(model)
            return "ok"

        out = await call_with_fallback(
            call, primary_model="primary", fallback_models=()
        )
        assert out == "ok"
        assert seen == ["primary"]

    @pytest.mark.asyncio
    async def test_empty_fallback_with_failure_raises(self) -> None:
        async def call(_model: str) -> str:
            raise Exception("HTTP 429")

        with pytest.raises(Exception, match="429"):
            await call_with_fallback(
                call, primary_model="primary", fallback_models=()
            )


# ---------------------------------------------------------------------------
# ModelConfig field
# ---------------------------------------------------------------------------


class TestModelConfigField:
    def test_default_is_empty_tuple(self) -> None:
        cfg = ModelConfig()
        assert cfg.fallback_models == ()

    def test_accepts_tuple(self) -> None:
        cfg = ModelConfig(
            model="claude-opus-4-7",
            fallback_models=("claude-sonnet-4-6", "claude-haiku-4-5"),
        )
        assert cfg.fallback_models == ("claude-sonnet-4-6", "claude-haiku-4-5")

    def test_dataclass_remains_hashable(self) -> None:
        # Frozen + slots + tuple-valued field → still hashable, which the
        # SDK relies on for memoisation in a few places.
        cfg = ModelConfig(fallback_models=("a", "b"))
        hash(cfg)  # would raise if not hashable
