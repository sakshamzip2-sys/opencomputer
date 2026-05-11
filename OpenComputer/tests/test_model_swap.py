"""Tests for ``opencomputer/cli_ui/_model_swap.py`` — scoped-models
keybinding helper (Alt+M cycles through a curated short list).

Mirrors ``test_cli_ui_resume_picker.py`` / ``_profile_swap.py`` test
shape so the new module slots into the existing UI-test pattern.

The "scoped models" concept: pi has it as a feature
(``scoped-models-selector.ts``) — a 2-3 model short list that the
user actually wants to flip between (e.g. opus for hard work, haiku
for cheap follow-ups), distinct from the full 300+ model registry.

OC stores the list at ``~/.opencomputer/<profile>/favorites.yaml``::

    models:
      - claude-opus-4-7
      - claude-sonnet-4-6
      - claude-haiku-4-5

Alt+M (Escape+M) cycles through them, mutating
``runtime.custom["pending_model_id"]``. The agent loop consumes the
pending swap at the next turn boundary.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from opencomputer.cli_ui._model_swap import (
    NO_OTHER_MODELS_HINT,
    cycle_model,
    list_favorite_models,
)


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``OPENCOMPUTER_HOME_ROOT`` at a tmp dir so favorites.yaml
    lookups land in a sandboxed location. The "default" profile dir
    is the root itself (per ``profiles.get_profile_dir``)."""
    home = tmp_path / "oc"
    home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(home))
    return home


def _make_runtime(active_model: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        custom={
            "active_model_id": active_model,
            "active_profile_id": "default",
        }
    )


def _write_favorites(home: Path, models: list[str]) -> None:
    # Default profile dir is the root itself.
    fav = home / "favorites.yaml"
    fav.write_text(yaml.safe_dump({"models": models}))


class TestListFavoriteModels:
    def test_empty_when_no_file(self, fake_home: Path) -> None:
        assert list_favorite_models(profile_id="default") == []

    def test_reads_yaml_list(self, fake_home: Path) -> None:
        _write_favorites(fake_home, ["model-a", "model-b", "model-c"])
        assert list_favorite_models(profile_id="default") == [
            "model-a",
            "model-b",
            "model-c",
        ]

    def test_empty_list_in_yaml_is_ok(self, fake_home: Path) -> None:
        _write_favorites(fake_home, [])
        assert list_favorite_models(profile_id="default") == []

    def test_malformed_yaml_returns_empty(self, fake_home: Path) -> None:
        fav = fake_home / "favorites.yaml"
        fav.write_text("not: a: valid: yaml: dict:")
        # Never raise — surface as empty so the keybinding falls back
        # to the no-other-models hint.
        assert list_favorite_models(profile_id="default") == []

    def test_non_string_entries_filtered(self, fake_home: Path) -> None:
        fav = fake_home / "favorites.yaml"
        fav.write_text(
            yaml.safe_dump({"models": ["valid-model", 42, None, "another-valid"]})
        )
        assert list_favorite_models(profile_id="default") == [
            "valid-model",
            "another-valid",
        ]


class TestCycleModel:
    def test_no_favorites_sets_hint(self, fake_home: Path) -> None:
        runtime = _make_runtime(active_model="anything")
        result = cycle_model(runtime)
        assert result is None
        assert runtime.custom["model_cycle_hint"] == NO_OTHER_MODELS_HINT

    def test_single_favorite_is_noop(self, fake_home: Path) -> None:
        _write_favorites(fake_home, ["only-one"])
        runtime = _make_runtime(active_model="only-one")
        # Single-element list = no cycling target.
        result = cycle_model(runtime)
        assert result is None
        assert runtime.custom["model_cycle_hint"] == NO_OTHER_MODELS_HINT

    def test_advances_to_next(self, fake_home: Path) -> None:
        _write_favorites(fake_home, ["a", "b", "c"])
        runtime = _make_runtime(active_model="a")
        result = cycle_model(runtime)
        assert result == "b"
        assert runtime.custom["pending_model_id"] == "b"

    def test_wraps_around(self, fake_home: Path) -> None:
        _write_favorites(fake_home, ["a", "b", "c"])
        runtime = _make_runtime(active_model="c")
        result = cycle_model(runtime)
        assert result == "a"

    def test_unknown_active_starts_at_first(self, fake_home: Path) -> None:
        _write_favorites(fake_home, ["a", "b", "c"])
        runtime = _make_runtime(active_model="not-in-list")
        result = cycle_model(runtime)
        # Falls through to first favorite — gives the user something
        # rather than a no-op when the active model isn't favorited.
        assert result == "a"

    def test_uses_pending_over_active(self, fake_home: Path) -> None:
        _write_favorites(fake_home, ["a", "b", "c"])
        runtime = _make_runtime(active_model="a")
        # Two consecutive cycles: a → b (pending), b → c (pending).
        cycle_model(runtime)
        result = cycle_model(runtime)
        assert result == "c"
        assert runtime.custom["pending_model_id"] == "c"


class TestCycleAfterSwap:
    """Regression: 2026-05-11 — after a ``/model`` swap, ``cycle_model``
    must advance FROM the swapped model, not restart from
    ``favorites[0]``. This works iff ``swap_model`` writes
    ``runtime.custom["active_model_id"]``, which it now does. Without
    that write the cycle's ``current = runtime.custom.get("active_model_id")``
    read returned None and every Alt+M tap snapped back to position 0."""

    def test_cycle_advances_from_swap_set_active_model(
        self,
        fake_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_favorites(fake_home, ["a", "b", "c"])
        runtime = SimpleNamespace(custom={"active_profile_id": "default"})

        # Simulate a /model swap into "b" via the canonical helper.
        # (Stub the resolver so we don't need a real Config.)
        import dataclasses

        @dataclasses.dataclass(frozen=True)
        class _Cfg:
            model: Any

        @dataclasses.dataclass(frozen=True)
        class _MCfg:
            model: str
            provider: str
            model_aliases: dict = dataclasses.field(default_factory=dict)

        class _P:
            def supports_native_thinking_for(self, _m: str) -> bool:
                return False

        loop = SimpleNamespace(
            config=_Cfg(model=_MCfg(model="a", provider="anthropic")),
            provider=_P(),
        )
        from opencomputer.agent import model_resolver as mr
        from opencomputer.agent.model_swap import swap_model

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)
        ok, _ = swap_model(loop=loop, runtime=runtime, new_model="b")
        assert ok is True
        # After the swap the cache anchors at "b" — Alt+M now advances
        # to "c" (the NEXT favorite), not "a" (favorites[0]).
        nxt = cycle_model(runtime)
        assert nxt == "c"

    def test_cycle_wraps_from_swap_set_last_favorite(
        self,
        fake_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the swap lands on the LAST favorite, Alt+M must wrap
        back to ``favorites[0]`` — confirms the modulo math reads
        ``active_model_id`` correctly post-swap."""
        _write_favorites(fake_home, ["a", "b", "c"])
        runtime = SimpleNamespace(custom={"active_profile_id": "default"})

        import dataclasses

        @dataclasses.dataclass(frozen=True)
        class _Cfg:
            model: Any

        @dataclasses.dataclass(frozen=True)
        class _MCfg:
            model: str
            provider: str
            model_aliases: dict = dataclasses.field(default_factory=dict)

        class _P:
            def supports_native_thinking_for(self, _m: str) -> bool:
                return False

        loop = SimpleNamespace(
            config=_Cfg(model=_MCfg(model="a", provider="anthropic")),
            provider=_P(),
        )
        from opencomputer.agent import model_resolver as mr
        from opencomputer.agent.model_swap import swap_model

        monkeypatch.setattr(mr, "resolve_model", lambda x, a, **_kw: x)
        swap_model(loop=loop, runtime=runtime, new_model="c")
        nxt = cycle_model(runtime)
        assert nxt == "a"
