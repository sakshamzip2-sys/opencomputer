"""Recipe A — coding-harness bootstrap WARN + shared recommended-plugins constant.

These tests pin Recipe A (M1.0) from
``docs/refs/2026-05-17-coding-harness-and-orchestration-gaps.md``:

* a shared ``RECOMMENDED_PLUGINS`` tuple lives in one place — the setup
  wizard and the bootstrap WARN must read from the same source;
* ``is_harness_dark`` correctly classifies the four states (wildcard /
  not installed / loaded / dark);
* ``maybe_warn_harness_dark`` emits a single yellow line pointing at
  ``oc plugin enable coding-harness`` exactly when the harness is
  installed but the active plugin filter excludes it — and is silent
  otherwise (env-var suppress, profile.yaml suppress, harness loaded,
  harness not installed).
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

# The module under test does not exist yet (RED).
from opencomputer.plugins.recommended import (
    RECOMMENDED_PLUGINS,
    is_harness_dark,
    maybe_warn_harness_dark,
)

# ──────────────────────────────────────────────────────────────────────
# RECOMMENDED_PLUGINS constant — single source of truth
# ──────────────────────────────────────────────────────────────────────


def test_recommended_plugins_tuple_pins_coding_harness_first() -> None:
    """The first entry is the harness — the WARN message references it
    by exact name. Pinning the order protects against silent reordering
    that would skew the existing wizard's 'Enabled N tool categories'
    message + setup audit."""
    assert RECOMMENDED_PLUGINS[0] == "coding-harness"
    assert "memory-honcho" in RECOMMENDED_PLUGINS
    assert "dev-tools" in RECOMMENDED_PLUGINS
    assert isinstance(RECOMMENDED_PLUGINS, tuple)


def test_setup_wizard_reexports_from_canonical_location() -> None:
    """The existing wizard helper still works — but its constant now
    points at the canonical tuple. Back-compat for any external import
    that already references ``cli_setup.section_handlers.tools._RECOMMENDED_PLUGINS``.
    """
    from opencomputer.cli_setup.section_handlers.tools import _RECOMMENDED_PLUGINS

    assert _RECOMMENDED_PLUGINS is RECOMMENDED_PLUGINS


# ──────────────────────────────────────────────────────────────────────
# is_harness_dark — classification helper
# ──────────────────────────────────────────────────────────────────────


def test_is_harness_dark_true_when_installed_but_excluded() -> None:
    """The exact symptom Recipe A targets — harness is on disk but
    explicit enabled list excludes it."""
    assert is_harness_dark(
        enabled_ids=frozenset({"telegram", "browser-control"}),
        installed_plugin_ids=frozenset({"telegram", "browser-control", "coding-harness"}),
    )


def test_is_harness_dark_false_when_wildcard() -> None:
    """``enabled_ids=None`` is the malformed-config / missing-preset
    fallback — load everything. The harness loads on its own; stay
    silent."""
    assert not is_harness_dark(
        enabled_ids=None,
        installed_plugin_ids=frozenset({"coding-harness", "telegram"}),
    )


def test_is_harness_dark_false_when_star_literal() -> None:
    """``cli._resolve_plugin_filter`` can return the ``"*"`` string
    (explicit wildcard) — distinct from ``None`` but equally load-all.
    Both must classify as not-dark."""
    assert not is_harness_dark(
        enabled_ids="*",
        installed_plugin_ids=frozenset({"coding-harness", "telegram"}),
    )


def test_is_harness_dark_false_when_not_installed() -> None:
    """If the harness isn't on disk, there's nothing to enable — WARN
    would point at a non-existent fix. Stay silent."""
    assert not is_harness_dark(
        enabled_ids=frozenset({"telegram"}),
        installed_plugin_ids=frozenset({"telegram"}),
    )


def test_is_harness_dark_false_when_harness_in_enabled() -> None:
    """The user is already opted in — no nag."""
    assert not is_harness_dark(
        enabled_ids=frozenset({"coding-harness", "telegram"}),
        installed_plugin_ids=frozenset({"coding-harness", "telegram"}),
    )


# ──────────────────────────────────────────────────────────────────────
# maybe_warn_harness_dark — suppression + emission
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def captured_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=120, force_terminal=False, no_color=True), buf


def test_warn_emits_when_harness_dark(
    captured_console: tuple[Console, io.StringIO], tmp_path
) -> None:
    console, buf = captured_console
    profile_yaml = tmp_path / "profile.yaml"
    profile_yaml.write_text("plugins:\n  enabled: [telegram]\n")
    fired = maybe_warn_harness_dark(
        enabled_ids=frozenset({"telegram"}),
        installed_plugin_ids=frozenset({"telegram", "coding-harness"}),
        profile_yaml=profile_yaml,
        console=console,
        env={},
    )
    out = buf.getvalue()
    assert fired is True, "harness dark + no suppression → must warn"
    assert "coding-harness" in out
    assert "oc plugin enable coding-harness" in out


def test_warn_silent_when_env_var_set(
    captured_console: tuple[Console, io.StringIO], tmp_path
) -> None:
    console, buf = captured_console
    profile_yaml = tmp_path / "profile.yaml"
    profile_yaml.write_text("plugins:\n  enabled: [telegram]\n")
    fired = maybe_warn_harness_dark(
        enabled_ids=frozenset({"telegram"}),
        installed_plugin_ids=frozenset({"telegram", "coding-harness"}),
        profile_yaml=profile_yaml,
        console=console,
        env={"OPENCOMPUTER_NO_HARNESS_WARN": "1"},
    )
    assert fired is False
    assert buf.getvalue() == ""


def test_warn_silent_when_profile_suppression_set(
    captured_console: tuple[Console, io.StringIO], tmp_path
) -> None:
    console, buf = captured_console
    profile_yaml = tmp_path / "profile.yaml"
    profile_yaml.write_text(
        "plugins:\n  enabled: [telegram]\n  suppress_harness_warning: true\n"
    )
    fired = maybe_warn_harness_dark(
        enabled_ids=frozenset({"telegram"}),
        installed_plugin_ids=frozenset({"telegram", "coding-harness"}),
        profile_yaml=profile_yaml,
        console=console,
        env={},
    )
    assert fired is False
    assert buf.getvalue() == ""


def test_warn_silent_when_harness_loaded(
    captured_console: tuple[Console, io.StringIO], tmp_path
) -> None:
    console, buf = captured_console
    profile_yaml = tmp_path / "profile.yaml"
    profile_yaml.write_text("plugins:\n  enabled: [telegram, coding-harness]\n")
    fired = maybe_warn_harness_dark(
        enabled_ids=frozenset({"telegram", "coding-harness"}),
        installed_plugin_ids=frozenset({"telegram", "coding-harness"}),
        profile_yaml=profile_yaml,
        console=console,
        env={},
    )
    assert fired is False
    assert buf.getvalue() == ""


def test_warn_silent_when_wildcard_filter(
    captured_console: tuple[Console, io.StringIO], tmp_path
) -> None:
    """No filter = load everything = harness will load = no nag."""
    console, buf = captured_console
    profile_yaml = tmp_path / "profile.yaml"
    profile_yaml.write_text('plugins:\n  enabled: "*"\n')
    fired = maybe_warn_harness_dark(
        enabled_ids=None,
        installed_plugin_ids=frozenset({"telegram", "coding-harness"}),
        profile_yaml=profile_yaml,
        console=console,
        env={},
    )
    assert fired is False
    assert buf.getvalue() == ""


def test_warn_silent_when_profile_yaml_missing(
    captured_console: tuple[Console, io.StringIO], tmp_path
) -> None:
    """Missing profile.yaml means the resolver returned the safe
    'load everything' default — same as wildcard. Stay silent."""
    console, buf = captured_console
    fired = maybe_warn_harness_dark(
        enabled_ids=None,
        installed_plugin_ids=frozenset({"coding-harness"}),
        profile_yaml=tmp_path / "absent.yaml",
        console=console,
        env={},
    )
    assert fired is False
    assert buf.getvalue() == ""


def test_warn_handles_malformed_profile_yaml_silently(
    captured_console: tuple[Console, io.StringIO], tmp_path
) -> None:
    """A malformed profile.yaml must NEVER crash the chat startup —
    treat it as 'no suppression configured' and let the WARN fire if
    other conditions match. Mirrors the existing tolerant pattern in
    ``cli_profile._read_enabled_plugin_ids``.
    """
    console, buf = captured_console
    profile_yaml = tmp_path / "profile.yaml"
    profile_yaml.write_text("plugins: [this is not a mapping]\n")
    fired = maybe_warn_harness_dark(
        enabled_ids=frozenset({"telegram"}),
        installed_plugin_ids=frozenset({"telegram", "coding-harness"}),
        profile_yaml=profile_yaml,
        console=console,
        env={},
    )
    assert fired is True  # falls through to WARN because we couldn't read suppression
    assert "oc plugin enable coding-harness" in buf.getvalue()


def test_warn_fires_when_env_var_empty_string(
    captured_console: tuple[Console, io.StringIO], tmp_path
) -> None:
    """Only a non-empty ``OPENCOMPUTER_NO_HARNESS_WARN`` value suppresses;
    the var present-but-empty does NOT suppress (parity with how the rest
    of the project reads boolean-ish env vars)."""
    console, buf = captured_console
    profile_yaml = tmp_path / "profile.yaml"
    profile_yaml.write_text("plugins:\n  enabled: [telegram]\n")
    fired = maybe_warn_harness_dark(
        enabled_ids=frozenset({"telegram"}),
        installed_plugin_ids=frozenset({"telegram", "coding-harness"}),
        profile_yaml=profile_yaml,
        console=console,
        env={"OPENCOMPUTER_NO_HARNESS_WARN": ""},
    )
    assert fired is True
    assert "oc plugin enable coding-harness" in buf.getvalue()


def test_warn_message_is_single_line(
    captured_console: tuple[Console, io.StringIO], tmp_path
) -> None:
    """The doc explicitly specifies 'one-line WARN with the fix command'.
    Multi-line output is annoying noise at chat startup. The message body
    (excluding trailing newline) must be a single line."""
    console, buf = captured_console
    profile_yaml = tmp_path / "profile.yaml"
    profile_yaml.write_text("plugins:\n  enabled: []\n")
    maybe_warn_harness_dark(
        enabled_ids=frozenset(),
        installed_plugin_ids=frozenset({"coding-harness"}),
        profile_yaml=profile_yaml,
        console=console,
        env={},
    )
    body = buf.getvalue().rstrip("\n")
    assert "\n" not in body, f"WARN must be single line; got {body!r}"


# ──────────────────────────────────────────────────────────────────────
# Integration — the cli.py glue wires real discovery + resolver + helper
# ──────────────────────────────────────────────────────────────────────


def test_chat_glue_warns_for_dark_profile(monkeypatch, tmp_path, capsys) -> None:
    """End-to-end: ``_maybe_warn_coding_harness_dark`` walks the REAL
    bundled ``extensions/`` (which ships coding-harness) and the REAL
    profile resolver. A profile.yaml enabling only ``telegram`` excludes
    the bundled harness → the glue must print the WARN to stdout."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "profile.yaml").write_text("plugins:\n  enabled: [telegram]\n")

    from opencomputer.cli import _maybe_warn_coding_harness_dark

    _maybe_warn_coding_harness_dark()
    assert "oc plugin enable coding-harness" in capsys.readouterr().out


def test_chat_glue_silent_for_wildcard_profile(monkeypatch, tmp_path, capsys) -> None:
    """The inverse: no profile.yaml at all → the resolver returns the
    load-everything default → the bundled harness loads → no WARN."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.cli import _maybe_warn_coding_harness_dark

    _maybe_warn_coding_harness_dark()
    assert "coding-harness" not in capsys.readouterr().out
