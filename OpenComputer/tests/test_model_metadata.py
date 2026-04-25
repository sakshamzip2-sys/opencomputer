"""Tests for G.32 — model metadata registry.

Covers:

1. Curated defaults are present and reasonable.
2. Lookup helpers (`context_length`, `cost_per_million`) match.
3. ``register_model`` adds + de-dupes correctly.
4. ``replace=True`` overrides; default ``False`` preserves curated.
5. ``list_models`` returns deterministic-sorted entries.
"""

from __future__ import annotations

import pytest

from opencomputer.agent.model_metadata import (
    ModelMetadata,
    context_length,
    cost_per_million,
    get_metadata,
    list_models,
    register_model,
    reset_to_defaults,
)


@pytest.fixture(autouse=True)
def _reset_between_tests():
    yield
    reset_to_defaults()


# ---------------------------------------------------------------------------
# Curated defaults
# ---------------------------------------------------------------------------


class TestCuratedDefaults:
    def test_anthropic_models_present(self) -> None:
        assert get_metadata("claude-opus-4-7") is not None
        assert get_metadata("claude-sonnet-4-6") is not None
        assert get_metadata("claude-haiku-4-5-20251001") is not None

    def test_openai_models_present(self) -> None:
        for mid in ("gpt-5.4", "gpt-4o", "o1", "o3", "o4-mini"):
            assert get_metadata(mid) is not None, f"missing default: {mid}"

    def test_unknown_model_returns_none(self) -> None:
        assert get_metadata("totally-fake-model") is None

    def test_context_length_helpers(self) -> None:
        # Claude family ships with 200k context.
        assert context_length("claude-opus-4-7") == 200_000
        assert context_length("claude-sonnet-4-6") == 200_000
        # GPT-4o caps at 128k.
        assert context_length("gpt-4o") == 128_000

    def test_cost_per_million_returns_tuple(self) -> None:
        c = cost_per_million("claude-opus-4-7")
        assert c is not None
        in_cost, out_cost = c
        # Output should be > input for every model — sanity check
        # against pricing data drift.
        assert out_cost > in_cost
        assert in_cost > 0


# ---------------------------------------------------------------------------
# register_model + replace semantics
# ---------------------------------------------------------------------------


class TestRegister:
    def test_new_model_registered(self) -> None:
        custom = ModelMetadata(
            model_id="custom-future-model",
            context_length=10_000_000,
            input_usd_per_million=0.05,
            output_usd_per_million=0.10,
        )
        register_model(custom)
        assert get_metadata("custom-future-model") == custom

    def test_collision_without_replace_preserves_curated(self) -> None:
        original = get_metadata("claude-opus-4-7")
        assert original is not None
        # Try to override with garbage; should be silently ignored.
        register_model(
            ModelMetadata(model_id="claude-opus-4-7", context_length=999),
        )
        # Curated default still wins.
        assert get_metadata("claude-opus-4-7") == original

    def test_collision_with_replace_overrides(self) -> None:
        register_model(
            ModelMetadata(
                model_id="claude-opus-4-7",
                context_length=1_000_000,
                input_usd_per_million=0.0,
            ),
            replace=True,
        )
        assert context_length("claude-opus-4-7") == 1_000_000


# ---------------------------------------------------------------------------
# Cost tuple edge cases
# ---------------------------------------------------------------------------


class TestCostEdgeCases:
    def test_partial_entry_returns_zero_for_missing_field(self) -> None:
        register_model(
            ModelMetadata(
                model_id="halfprice-model",
                input_usd_per_million=1.0,
                # output_usd_per_million unset
            )
        )
        c = cost_per_million("halfprice-model")
        assert c == (1.0, 0.0)

    def test_no_cost_fields_returns_none(self) -> None:
        register_model(
            ModelMetadata(
                model_id="ctx-only-model",
                context_length=42_000,
                # both costs unset
            )
        )
        assert cost_per_million("ctx-only-model") is None
        # context_length still works.
        assert context_length("ctx-only-model") == 42_000


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestList:
    def test_list_models_sorted(self) -> None:
        ids = [m.model_id for m in list_models()]
        assert ids == sorted(ids)

    def test_list_models_returns_immutable_snapshot(self) -> None:
        # Caller mutating the returned list shouldn't corrupt the
        # registry.
        snapshot = list_models()
        snapshot.clear()
        # Registry intact.
        assert len(list_models()) > 0


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_third_party_entries(self) -> None:
        register_model(
            ModelMetadata(model_id="ephemeral-model", context_length=100)
        )
        assert get_metadata("ephemeral-model") is not None
        reset_to_defaults()
        assert get_metadata("ephemeral-model") is None
        # Curated entries still present.
        assert get_metadata("claude-opus-4-7") is not None
