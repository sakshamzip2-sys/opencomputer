"""Tests for `opencomputer models add` (Round 2A P-11).

Covers the four contracts called out in the plan:

1. **Add new** — `models add` registers a new entry; `list_models()`
   includes it.
2. **Idempotent** — re-running with no flag changes is a no-op (logs
   "already registered"); the registry is unchanged.
3. **Restart simulation** — the YAML file persists across resets; a
   fresh ``apply_overrides_file`` call re-applies the entries.
4. **Non-destructive** — pre-existing curated entries (Anthropic +
   OpenAI defaults) survive even when the user adds a brand-new one,
   and an UPDATE on a curated id only mutates the touched fields.

Plus a few edge cases:

* Atomic write — corrupted ``model_overrides.yaml`` doesn't crash
  startup; ``apply_overrides_file`` returns 0.
* ``--alias`` adds a second resolvable id that maps to the same meta.
* Filtering ``models list --provider`` returns only matching rows.
* File mode is 0600 (private to the user).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from opencomputer.agent.model_metadata import (
    ADD_STATUS_ADDED,
    ADD_STATUS_NOOP,
    ADD_STATUS_UPDATED,
    apply_overrides_file,
    get_metadata,
    list_models,
    register_user_model,
    reset_to_defaults,
    upsert_override_file,
)
from opencomputer.cli_models import models_app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``_home()`` at a fresh tmp_path so each test sees an empty
    profile (no leftover ``model_overrides.yaml`` from prior runs)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Restore the curated registry between tests so cross-test pollution
    doesn't sneak in (the registry is module-level state)."""
    yield
    reset_to_defaults()


# ---------------------------------------------------------------------------
# register_user_model — the underlying merge primitive
# ---------------------------------------------------------------------------


class TestRegisterUserModel:
    def test_add_new_returns_added_status(self) -> None:
        status, meta = register_user_model(
            provider_id="vendor",
            model_id="brand-new-model",
            context_length=32_000,
            input_usd_per_million=0.5,
            output_usd_per_million=2.0,
        )
        assert status == ADD_STATUS_ADDED
        assert meta.context_length == 32_000
        assert meta.input_usd_per_million == 0.5
        assert meta.provider_id == "vendor"
        assert get_metadata("brand-new-model") == meta

    def test_idempotent_reapply_returns_noop(self) -> None:
        register_user_model(
            provider_id="vendor",
            model_id="repeat-model",
            context_length=8_000,
        )
        status, _ = register_user_model(
            provider_id="vendor",
            model_id="repeat-model",
            context_length=8_000,
        )
        assert status == ADD_STATUS_NOOP

    def test_changing_a_field_returns_updated(self) -> None:
        register_user_model(
            provider_id="vendor",
            model_id="growable-model",
            context_length=8_000,
        )
        status, meta = register_user_model(
            provider_id="vendor",
            model_id="growable-model",
            context_length=64_000,
        )
        assert status == ADD_STATUS_UPDATED
        assert meta.context_length == 64_000

    def test_partial_update_preserves_untouched_fields(self) -> None:
        register_user_model(
            provider_id="vendor",
            model_id="partial-model",
            context_length=10_000,
            input_usd_per_million=1.0,
            output_usd_per_million=4.0,
        )
        # Update only the context — costs should survive.
        register_user_model(
            provider_id="vendor",
            model_id="partial-model",
            context_length=20_000,
        )
        meta = get_metadata("partial-model")
        assert meta is not None
        assert meta.context_length == 20_000
        assert meta.input_usd_per_million == 1.0
        assert meta.output_usd_per_million == 4.0

    def test_alias_resolves_to_same_metadata(self) -> None:
        register_user_model(
            provider_id="anthropic",
            model_id="claude-haiku-4-5-20251001",
            alias="claude-fast",
            context_length=200_000,
        )
        canonical = get_metadata("claude-haiku-4-5-20251001")
        aliased = get_metadata("claude-fast")
        assert canonical is not None and aliased is not None
        # Same context_length / costs, just a different model_id key.
        assert canonical.context_length == aliased.context_length
        assert canonical.input_usd_per_million == aliased.input_usd_per_million

    def test_curated_entry_survives_when_unrelated_model_added(self) -> None:
        # Sanity check: curated G.32 defaults must not be wiped by an
        # add of a completely new entry.
        original = get_metadata("claude-opus-4-7")
        assert original is not None
        register_user_model(
            provider_id="vendor",
            model_id="something-else",
            context_length=42,
        )
        assert get_metadata("claude-opus-4-7") == original


# ---------------------------------------------------------------------------
# Persistence — upsert_override_file + apply_overrides_file
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_upsert_creates_file_with_atomic_mode_0600(
        self, isolated_home: Path
    ) -> None:
        path = upsert_override_file(
            provider_id="anthropic",
            model_id="my-private-claude",
            context_length=128_000,
            input_usd_per_million=2.0,
            output_usd_per_million=10.0,
        )
        assert path.exists()
        # Mode bits — owner read/write only (0600).
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_apply_overrides_file_replays_into_registry(
        self, isolated_home: Path
    ) -> None:
        # Simulate a restart: write the file, reset the registry, re-apply.
        upsert_override_file(
            provider_id="custom",
            model_id="restart-model",
            context_length=99_000,
            input_usd_per_million=0.25,
            output_usd_per_million=1.0,
        )
        # Wipe in-memory state — pretend we restarted the process.
        reset_to_defaults()
        assert get_metadata("restart-model") is None

        applied = apply_overrides_file()
        assert applied == 1
        meta = get_metadata("restart-model")
        assert meta is not None
        assert meta.context_length == 99_000
        assert meta.provider_id == "custom"

    def test_upsert_preserves_other_entries(
        self, isolated_home: Path
    ) -> None:
        upsert_override_file(
            provider_id="anthropic",
            model_id="model-a",
            context_length=1_000,
        )
        upsert_override_file(
            provider_id="openai",
            model_id="model-b",
            context_length=2_000,
        )
        # Both entries should now be on disk.
        path = isolated_home / "model_overrides.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        ids = sorted(e["model_id"] for e in data["models"])
        assert ids == ["model-a", "model-b"]

    def test_upsert_replaces_same_pair(self, isolated_home: Path) -> None:
        upsert_override_file(
            provider_id="anthropic",
            model_id="evolving-model",
            context_length=1_000,
        )
        upsert_override_file(
            provider_id="anthropic",
            model_id="evolving-model",
            context_length=4_000,
        )
        path = isolated_home / "model_overrides.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        # Only ONE entry — the second upsert merged in place.
        assert len(data["models"]) == 1
        assert data["models"][0]["context_length"] == 4_000

    def test_corrupt_yaml_returns_zero_no_crash(
        self, isolated_home: Path
    ) -> None:
        path = isolated_home / "model_overrides.yaml"
        path.write_text(":\n:\n:invalid yaml here\n", encoding="utf-8")
        applied = apply_overrides_file()
        assert applied == 0  # fail-safe — log error, treat as empty

    def test_missing_file_returns_zero(self, isolated_home: Path) -> None:
        # No file written — apply should be a clean no-op.
        applied = apply_overrides_file()
        assert applied == 0

    def test_malformed_entry_skipped(self, isolated_home: Path) -> None:
        path = isolated_home / "model_overrides.yaml"
        # One valid entry + one missing model_id; valid one survives.
        path.write_text(
            yaml.safe_dump(
                {
                    "models": [
                        {"provider_id": "anthropic"},  # missing model_id — skip
                        {
                            "provider_id": "openai",
                            "model_id": "good-model",
                            "context_length": 16_000,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        applied = apply_overrides_file()
        assert applied == 1
        assert get_metadata("good-model") is not None


# ---------------------------------------------------------------------------
# CLI surface — `opencomputer models add` / `models list`
# ---------------------------------------------------------------------------


class TestCliAdd:
    def test_add_new_persists_and_lists(self, isolated_home: Path) -> None:
        result = runner.invoke(
            models_app,
            [
                "add", "anthropic", "claude-experimental",
                "--context", "150000",
                "--cost-input", "1.5",
                "--cost-output", "7.5",
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "added" in result.stdout.lower()
        # File got written under isolated_home.
        path = isolated_home / "model_overrides.yaml"
        assert path.exists()
        # Live registry has it without a restart.
        meta = get_metadata("claude-experimental")
        assert meta is not None
        assert meta.context_length == 150_000
        assert meta.provider_id == "anthropic"

    def test_re_add_same_pair_is_noop(self, isolated_home: Path) -> None:
        runner.invoke(models_app, ["add", "vendor", "stable", "--context", "1000"])
        result = runner.invoke(
            models_app, ["add", "vendor", "stable", "--context", "1000"]
        )
        assert result.exit_code == 0
        assert "already registered" in result.stdout.lower()

    def test_re_add_with_changed_flag_updates(self, isolated_home: Path) -> None:
        runner.invoke(models_app, ["add", "vendor", "growing", "--context", "1000"])
        result = runner.invoke(
            models_app, ["add", "vendor", "growing", "--context", "8000"]
        )
        assert result.exit_code == 0
        assert "updated" in result.stdout.lower()
        meta = get_metadata("growing")
        assert meta is not None and meta.context_length == 8_000

    def test_alias_flag_creates_resolvable_alias(
        self, isolated_home: Path
    ) -> None:
        result = runner.invoke(
            models_app,
            [
                "add", "anthropic", "claude-haiku-4-5-20251001",
                "--alias", "fast",
                "--context", "200000",
            ],
        )
        assert result.exit_code == 0
        assert get_metadata("fast") is not None

    def test_list_default_shows_curated_plus_added(
        self, isolated_home: Path
    ) -> None:
        runner.invoke(
            models_app,
            ["add", "anthropic", "user-added", "--context", "10000"],
        )
        result = runner.invoke(models_app, ["list"])
        assert result.exit_code == 0
        # Both a curated default and the user-added entry present.
        flat = "".join(c for c in result.stdout if c.isalnum() or c == "-")
        assert "claude-opus-4-7" in flat
        assert "user-added" in flat

    def test_list_filter_by_provider(self, isolated_home: Path) -> None:
        runner.invoke(models_app, ["add", "anthropic", "filtered-claude"])
        runner.invoke(models_app, ["add", "openai", "filtered-gpt"])
        result = runner.invoke(models_app, ["list", "--provider", "openai"])
        assert result.exit_code == 0
        flat = "".join(c for c in result.stdout if c.isalnum() or c == "-")
        assert "filtered-gpt" in flat
        # The Anthropic-tagged user entry MUST be filtered out.
        assert "filtered-claude" not in flat

    def test_empty_provider_argument_rejected(
        self, isolated_home: Path
    ) -> None:
        result = runner.invoke(models_app, ["add", " ", "model-id"])
        assert result.exit_code != 0

    def test_empty_model_argument_rejected(
        self, isolated_home: Path
    ) -> None:
        result = runner.invoke(models_app, ["add", "vendor", " "])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Restart simulation — full round-trip through the CLI
# ---------------------------------------------------------------------------


class TestRestartSimulation:
    def test_yaml_drives_apply_after_reset(self, isolated_home: Path) -> None:
        # Step 1 — user adds via CLI.
        runner.invoke(
            models_app,
            [
                "add", "anthropic", "claude-restart",
                "--context", "300000",
                "--cost-input", "2.5",
                "--cost-output", "12.5",
            ],
        )
        # Step 2 — pretend the process restarted (registry empty).
        reset_to_defaults()
        assert get_metadata("claude-restart") is None
        # Step 3 — startup hook fires.
        applied = apply_overrides_file()
        assert applied >= 1
        meta = get_metadata("claude-restart")
        assert meta is not None
        assert meta.context_length == 300_000
        assert meta.input_usd_per_million == 2.5
        assert meta.output_usd_per_million == 12.5

    def test_curated_defaults_still_present_after_restart_apply(
        self, isolated_home: Path
    ) -> None:
        runner.invoke(
            models_app, ["add", "vendor", "user-only", "--context", "500"]
        )
        reset_to_defaults()
        apply_overrides_file()
        # Curated stays: G.32 defaults are loaded by ``reset_to_defaults``.
        assert get_metadata("claude-opus-4-7") is not None
        # User entry replayed from disk:
        assert get_metadata("user-only") is not None


# ---------------------------------------------------------------------------
# Non-destructive contract — guarantee against accidental data loss
# ---------------------------------------------------------------------------


class TestNonDestructive:
    def test_adding_user_model_does_not_remove_other_models(
        self, isolated_home: Path
    ) -> None:
        before = {m.model_id for m in list_models()}
        runner.invoke(models_app, ["add", "vendor", "addition", "--context", "1"])
        after = {m.model_id for m in list_models()}
        # Every prior id is still present.
        assert before <= after
        assert "addition" in after

    def test_apply_overrides_overwrites_curated_when_user_opts_in(
        self, isolated_home: Path
    ) -> None:
        # User explicitly overrides a curated default's context_length.
        upsert_override_file(
            provider_id="anthropic",
            model_id="claude-opus-4-7",
            context_length=999_999,
        )
        reset_to_defaults()
        apply_overrides_file()
        meta = get_metadata("claude-opus-4-7")
        assert meta is not None
        # User's value wins because they explicitly added it.
        assert meta.context_length == 999_999
