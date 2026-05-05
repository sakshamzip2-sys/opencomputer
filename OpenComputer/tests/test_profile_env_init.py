"""Tests for opencomputer.profile_env_init (D.4 T2 / Phase 14.G follow-up)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from opencomputer.profile_env_init import (
    EnvVarSpec,
    atomic_write,
    collect_env_var_specs,
    parse_env_file,
    render_env_file,
    run_init,
)


@dataclass(frozen=True)
class _FakeProvider:
    id: str = ""
    env_vars: tuple[str, ...] = ()
    label: str = ""
    signup_url: str = ""


@dataclass(frozen=True)
class _FakeChannel:
    id: str = ""
    env_vars: tuple[str, ...] = ()
    label: str = ""
    signup_url: str = ""


@dataclass(frozen=True)
class _FakeSetup:
    providers: tuple[_FakeProvider, ...] = ()
    channels: tuple[_FakeChannel, ...] = ()


@dataclass(frozen=True)
class _FakeManifest:
    id: str = ""
    description: str = ""
    setup: _FakeSetup = field(default_factory=_FakeSetup)


@dataclass(frozen=True)
class _FakeCandidate:
    manifest: _FakeManifest


def _cand(plugin_id: str, **kwargs):
    return _FakeCandidate(manifest=_FakeManifest(id=plugin_id, **kwargs))


# ─── collect_env_var_specs ────────────────────────────────────────────


def test_collect_specs_combines_providers_and_channels():
    cands = [
        _cand(
            "p1",
            setup=_FakeSetup(
                providers=(_FakeProvider(env_vars=("X_KEY",), label="P1"),),
                channels=(_FakeChannel(env_vars=("Y_TOK",), label="P1-chan"),),
            ),
        ),
    ]
    specs = collect_env_var_specs(cands)
    assert [s.var_name for s in specs] == ["X_KEY", "Y_TOK"]
    assert specs[0].plugin_id == "p1"
    assert specs[1].label == "P1-chan"


def test_collect_specs_dedupes_across_plugins():
    cands = [
        _cand("a", setup=_FakeSetup(providers=(_FakeProvider(env_vars=("SHARED",)),))),
        _cand("b", setup=_FakeSetup(providers=(_FakeProvider(env_vars=("SHARED",)),))),
    ]
    specs = collect_env_var_specs(cands)
    assert [s.var_name for s in specs] == ["SHARED"]
    assert specs[0].plugin_id == "a"  # first wins


def test_collect_specs_filters_disabled():
    cands = [
        _cand("on", setup=_FakeSetup(providers=(_FakeProvider(env_vars=("A",)),))),
        _cand("off", setup=_FakeSetup(providers=(_FakeProvider(env_vars=("B",)),))),
    ]
    specs = collect_env_var_specs(cands, enabled_ids={"on"})
    assert [s.var_name for s in specs] == ["A"]


# ─── parse_env_file / render_env_file ─────────────────────────────────


def test_parse_env_file_handles_missing_file(tmp_path: Path):
    assert parse_env_file(tmp_path / "missing.env") == {}


def test_parse_env_file_skips_comments_and_blanks(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment\n"
        "\n"
        "FOO=bar\n"
        '  BAZ="qux quux"\n'
        "BAD_LINE_NO_EQUALS\n"
        "ANOTHER=value\n"
    )
    assert parse_env_file(p) == {"FOO": "bar", "BAZ": "qux quux", "ANOTHER": "value"}


def test_render_env_file_quotes_special_values():
    out = render_env_file({"PLAIN": "abc", "WITH_SPACE": "a b c"})
    assert "PLAIN=abc" in out
    assert 'WITH_SPACE="a b c"' in out


def test_render_env_file_sorts_keys_for_stable_output():
    out1 = render_env_file({"B": "2", "A": "1"})
    out2 = render_env_file({"A": "1", "B": "2"})
    assert out1 == out2


# ─── atomic_write ─────────────────────────────────────────────────────


def test_atomic_write_creates_with_mode_0600(tmp_path: Path):
    target = tmp_path / "subdir" / ".env"
    atomic_write(target, "key=value\n")
    assert target.exists()
    assert target.read_text() == "key=value\n"
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600


def test_atomic_write_overwrites_existing(tmp_path: Path):
    target = tmp_path / ".env"
    target.write_text("old=content\n")
    atomic_write(target, "new=content\n")
    assert target.read_text() == "new=content\n"


# ─── run_init flow ────────────────────────────────────────────────────


def _sample_specs():
    return [
        EnvVarSpec("FOO_KEY", "foo", "Foo provider", "https://foo/", "Foo desc"),
        EnvVarSpec("BAR_TOKEN", "bar", "Bar channel", "", "Bar desc"),
    ]


def test_run_init_writes_entered_values(tmp_path: Path):
    target = tmp_path / ".env"
    answers = {"FOO_KEY": "f00", "BAR_TOKEN": "b4r"}

    def prompter(spec, current):
        return answers[spec.var_name]

    result = run_init(
        _sample_specs(),
        target_path=target,
        profile_name="default",
        prompter=prompter,
    )

    assert result.written == 2
    assert result.skipped_existing == 0
    parsed = parse_env_file(target)
    assert parsed == {"FOO_KEY": "f00", "BAR_TOKEN": "b4r"}


def test_run_init_skips_already_set_unless_overwrite(tmp_path: Path):
    target = tmp_path / ".env"
    target.write_text("FOO_KEY=preset\n")

    # Prompter would explode if called for FOO_KEY (already set).
    prompts: list[str] = []

    def prompter(spec, current):
        prompts.append(spec.var_name)
        return "newval" if spec.var_name == "BAR_TOKEN" else None  # type: ignore[return-value]

    result = run_init(
        _sample_specs(),
        target_path=target,
        profile_name="default",
        prompter=prompter,
        overwrite=False,
    )

    assert prompts == ["BAR_TOKEN"]
    assert result.skipped_existing == 1
    parsed = parse_env_file(target)
    assert parsed["FOO_KEY"] == "preset"
    assert parsed["BAR_TOKEN"] == "newval"


def test_run_init_empty_input_skips(tmp_path: Path):
    target = tmp_path / ".env"
    answers = {"FOO_KEY": "", "BAR_TOKEN": "tok"}

    def prompter(spec, current):
        return answers[spec.var_name]

    result = run_init(
        _sample_specs(),
        target_path=target,
        profile_name="default",
        prompter=prompter,
    )

    assert result.written == 1
    assert result.skipped_empty == 1
    parsed = parse_env_file(target)
    assert "FOO_KEY" not in parsed
    assert parsed["BAR_TOKEN"] == "tok"


def test_run_init_keyboardinterrupt_aborts_without_partial_write(tmp_path: Path):
    target = tmp_path / ".env"
    target.write_text("PRESET=existing\n")
    original = target.read_text()

    def prompter(spec, current):
        if spec.var_name == "BAR_TOKEN":
            return None  # signal abort
        return "first-value"

    with pytest.raises(KeyboardInterrupt):
        run_init(
            _sample_specs(),
            target_path=target,
            profile_name="default",
            prompter=prompter,
        )

    # The .env file is untouched.
    assert target.read_text() == original
