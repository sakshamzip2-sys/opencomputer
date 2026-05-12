"""Gap 2 — skill-evolution defensive-default-on.

The deep-dive doc surfaced that skill-evolution silently defaulted to OFF
on malformed state.json. That was the *one* file shape under which the
subscriber would no-op despite the user having neither opted out nor
disabled it explicitly. With this fix, only an explicit
``{"enabled": false}`` opts out — every other shape (missing / empty
file / empty JSON object / missing key / malformed JSON / non-dict
JSON / weird coercion) defaults to enabled, with WARN-level logging on
adversarial shapes so corruption is surfaced rather than swallowed.

Privacy contract preserved: the subscriber still does no work without
either Stage 1 + Stage 2 + extractor pipeline passing; it just makes
sure a *broken state file* doesn't silently disable the whole feature.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from extensions.skill_evolution.subscriber import _is_enabled


@pytest.fixture()
def profile_home(tmp_path: Path) -> Path:
    """Profile-home with empty skills/ subdir."""
    (tmp_path / "skills").mkdir(parents=True)
    return tmp_path


def _state(home: Path, content: str | None) -> None:
    """Write (or remove) skills/evolution_state.json."""
    p = home / "skills" / "evolution_state.json"
    if content is None:
        if p.exists():
            p.unlink()
        return
    p.write_text(content, encoding="utf-8")


# ── Default-on invariants (the 7 file shapes) ───────────────────────


def test_missing_file_defaults_to_enabled(profile_home: Path) -> None:
    """No state file on a fresh install → enabled."""
    _state(profile_home, None)
    assert _is_enabled(profile_home) is True


def test_empty_file_defaults_to_enabled(profile_home: Path) -> None:
    """Zero-byte file (touch / aborted write) → enabled.

    This was the buggy edge case: empty file → JSONDecodeError → False.
    Must now default to True with a WARN log so corruption is visible.
    """
    _state(profile_home, "")
    assert _is_enabled(profile_home) is True


def test_empty_object_defaults_to_enabled(profile_home: Path) -> None:
    """``{}`` → no ``enabled`` key → default True via ``.get(default=True)``."""
    _state(profile_home, "{}")
    assert _is_enabled(profile_home) is True


def test_unrelated_keys_defaults_to_enabled(profile_home: Path) -> None:
    """A state file with other keys but no ``enabled`` → True (the get-default)."""
    _state(profile_home, json.dumps({"other_key": "value", "version": 2}))
    assert _is_enabled(profile_home) is True


def test_explicit_true_is_enabled(profile_home: Path) -> None:
    """The happy opt-in path stays correct."""
    _state(profile_home, json.dumps({"enabled": True}))
    assert _is_enabled(profile_home) is True


def test_explicit_false_opts_out(profile_home: Path) -> None:
    """The ONLY way to disable is explicit ``{"enabled": false}``.

    Preserves user agency — defensive default doesn't override an
    explicit opt-out. Privacy escape hatch must survive this fix.
    """
    _state(profile_home, json.dumps({"enabled": False}))
    assert _is_enabled(profile_home) is False


def test_malformed_json_defaults_to_enabled_with_warn(
    profile_home: Path, caplog
) -> None:
    """Adversarial: corrupted state.json (truncated, invalid syntax) →
    enabled, with WARN. Pre-fix this returned False (silent disable);
    that was the load-bearing change in Gap 2."""
    _state(profile_home, "{not json")
    with caplog.at_level(logging.WARNING, logger="opencomputer.skill_evolution.subscriber"):
        assert _is_enabled(profile_home) is True
    assert any("malformed" in r.message.lower() for r in caplog.records)


def test_non_dict_json_defaults_to_enabled(profile_home: Path) -> None:
    """Adversarial: state.json is a JSON list / scalar / string instead of
    dict → enabled (cannot be a meaningful opt-out)."""
    _state(profile_home, json.dumps([1, 2, 3]))
    assert _is_enabled(profile_home) is True
    _state(profile_home, json.dumps("a string"))
    assert _is_enabled(profile_home) is True
    _state(profile_home, json.dumps(42))
    assert _is_enabled(profile_home) is True


def test_non_utf8_state_file_defaults_to_enabled(profile_home: Path) -> None:
    """Adversarial: state.json contains non-UTF-8 bytes → enabled, WARN."""
    p = profile_home / "skills" / "evolution_state.json"
    p.write_bytes(b'{"enabled": true}\xff')  # trailing invalid byte
    assert _is_enabled(profile_home) is True


def test_enabled_field_as_truthy_string(profile_home: Path) -> None:
    """``{"enabled": "yes"}`` — string truthy → True via bool() coercion.
    Mirrors Python's ``bool(non_empty_str) == True`` semantics. Documents
    the existing coercion behavior under the fix."""
    _state(profile_home, json.dumps({"enabled": "yes"}))
    assert _is_enabled(profile_home) is True


def test_enabled_field_as_explicit_zero(profile_home: Path) -> None:
    """``{"enabled": 0}`` — int 0 → False via bool() coercion. Preserves
    the explicit-opt-out invariant: anything truthy → True, anything
    explicitly-falsy → False. No surprises."""
    _state(profile_home, json.dumps({"enabled": 0}))
    assert _is_enabled(profile_home) is False
