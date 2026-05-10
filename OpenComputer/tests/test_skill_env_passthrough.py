"""Tests for the skill-scoped env + credential-file passthrough registry.

P3.4 + P3.5 — closes the dead-code gap where skill frontmatter parsing
existed but no production callsite consulted skill-declared
``required_environment_variables`` or ``required_credential_files``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from opencomputer.agent.memory import (
    RequiredCredentialFile,
    RequiredEnvVar,
    _parse_required_credential_files,
    _parse_required_env_vars,
)
from opencomputer.security import env_passthrough as ep


@pytest.fixture(autouse=True)
def _wipe_registry():
    """Each test starts with an empty registry."""
    ep.clear_registry_for_tests()
    yield
    ep.clear_registry_for_tests()


# ── frontmatter parser ──────────────────────────────────────────────


def test_parse_env_vars_dict_form():
    out = _parse_required_env_vars(
        [{"name": "FOO", "prompt": "Foo key", "help": "https://example/foo"}]
    )
    assert out == (RequiredEnvVar(name="FOO", prompt="Foo key", help="https://example/foo"),)


def test_parse_env_vars_string_form():
    out = _parse_required_env_vars(["FOO", "BAR"])
    assert out == (RequiredEnvVar(name="FOO"), RequiredEnvVar(name="BAR"))


def test_parse_env_vars_drops_empty_names():
    out = _parse_required_env_vars(["FOO", "", {"name": ""}, {"prompt": "no name"}])
    assert tuple(v.name for v in out) == ("FOO",)


def test_parse_env_vars_non_list_returns_empty():
    assert _parse_required_env_vars("FOO") == ()
    assert _parse_required_env_vars(None) == ()
    assert _parse_required_env_vars({"FOO": "X"}) == ()


def test_parse_credential_files_dict_form():
    out = _parse_required_credential_files(
        [{"path": "tok.json", "description": "Google OAuth"}]
    )
    assert out == (RequiredCredentialFile(path="tok.json", description="Google OAuth"),)


def test_parse_credential_files_string_form():
    out = _parse_required_credential_files(["a.json", "b.json"])
    assert tuple(f.path for f in out) == ("a.json", "b.json")


# ── registry: register / unregister ─────────────────────────────────


def test_register_then_get_passthrough():
    ep.register_skill_requirements(
        "skill-a",
        env_vars=(RequiredEnvVar(name="API_X"),),
        credential_files=(),
    )
    assert ep.get_passthrough_env_keys() == ("API_X",)


def test_register_dedupes_across_skills():
    ep.register_skill_requirements(
        "skill-a",
        env_vars=(RequiredEnvVar(name="SHARED"),),
        credential_files=(),
    )
    ep.register_skill_requirements(
        "skill-b",
        env_vars=(RequiredEnvVar(name="SHARED"), RequiredEnvVar(name="OTHER")),
        credential_files=(),
    )
    keys = ep.get_passthrough_env_keys()
    assert sorted(keys) == ["OTHER", "SHARED"]


def test_register_with_empty_unregisters():
    ep.register_skill_requirements(
        "skill-a",
        env_vars=(RequiredEnvVar(name="X"),),
        credential_files=(),
    )
    # Re-register with empty → drops the entry.
    ep.register_skill_requirements(
        "skill-a", env_vars=(), credential_files=(),
    )
    assert ep.get_passthrough_env_keys() == ()


def test_unregister_drops_entry():
    ep.register_skill_requirements(
        "skill-a",
        env_vars=(RequiredEnvVar(name="X"),),
        credential_files=(),
    )
    ep.unregister_skill_requirements("skill-a")
    assert ep.get_passthrough_env_keys() == ()


# ── missing required env vars ──────────────────────────────────────


def test_missing_required_env_vars(monkeypatch):
    ep.register_skill_requirements(
        "skill-a",
        env_vars=(
            RequiredEnvVar(name="MISSING_VAR_001", prompt="My key", help="here"),
            RequiredEnvVar(name="SET_VAR_002"),
        ),
        credential_files=(),
    )
    monkeypatch.setenv("SET_VAR_002", "value")
    monkeypatch.delenv("MISSING_VAR_001", raising=False)
    missing = ep.get_missing_required_env_vars()
    assert tuple(v.name for v in missing) == ("MISSING_VAR_001",)
    # Help and prompt preserved through the registry.
    assert missing[0].prompt == "My key"
    assert missing[0].help == "here"


def test_missing_treats_empty_string_as_unset(monkeypatch):
    ep.register_skill_requirements(
        "skill-a",
        env_vars=(RequiredEnvVar(name="EMPTY_STR_VAR"),),
        credential_files=(),
    )
    monkeypatch.setenv("EMPTY_STR_VAR", "   ")  # whitespace = unset
    missing = ep.get_missing_required_env_vars()
    assert any(v.name == "EMPTY_STR_VAR" for v in missing)


# ── credential file resolution ────────────────────────────────────


def test_resolve_credential_files_returns_existing(tmp_path: Path):
    # Layout: profile_home/google_token.json exists; missing.json doesn't.
    (tmp_path / "google_token.json").write_text("{}")
    ep.register_skill_requirements(
        "skill-a",
        env_vars=(),
        credential_files=(
            RequiredCredentialFile(path="google_token.json"),
            RequiredCredentialFile(path="missing.json"),
        ),
    )
    pairs = ep.resolve_credential_file_paths(tmp_path)
    # Only the existing file is returned.
    assert len(pairs) == 1
    host, container = pairs[0]
    assert host.name == "google_token.json"
    assert container == "/root/.opencomputer/google_token.json"


def test_resolve_credential_files_blocks_path_traversal(tmp_path: Path, caplog):
    """A skill that declares ``../../etc/passwd`` must not be honoured."""
    ep.register_skill_requirements(
        "skill-evil",
        env_vars=(),
        credential_files=(RequiredCredentialFile(path="../../etc/passwd"),),
    )
    pairs = ep.resolve_credential_file_paths(tmp_path)
    assert pairs == ()


def test_get_required_env_var_declarations():
    """Public listing surface for setup wizard / oc skills env."""
    ep.register_skill_requirements(
        "skill-a",
        env_vars=(RequiredEnvVar(name="A", prompt="A key", help="A help"),),
        credential_files=(),
    )
    out = ep.get_required_env_var_declarations()
    assert len(out) == 1
    assert out[0].name == "A"
    assert out[0].prompt == "A key"
