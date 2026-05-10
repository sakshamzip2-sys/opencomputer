"""SecretRef provider chain — env/exec providers + SecretRegistry.

Sits on top of the existing ``plugin_sdk.wire_primitives.SecretRef``
primitive (which is a wire-safe reference shape only — no resolution).
The chain here adds:

* :class:`~opencomputer.security.secrets.EnvSecretProvider` — resolves
  ``${VAR}`` references against ``os.environ``.
* :class:`~opencomputer.security.secrets.ExecSecretProvider` — invokes
  a validated CLI (``op``, ``vault``, ``sops``) with no shell, a
  configurable timeout, and an output-byte cap.
* :class:`~opencomputer.security.secrets.SecretRegistry` — eager
  resolve at startup; atomic swap on reload (full success or keep
  last-known-good).

These tests pin the contract: provider precedence, error propagation,
exec safety (no shell, timeout, output cap), and audit signals.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from opencomputer.security.secrets import (
    AuditFinding,
    EnvSecretProvider,
    ExecSecretProvider,
    SecretProviderError,
    SecretRegistry,
    SecretSpec,
    audit_paths,
)

# ─── EnvSecretProvider ────────────────────────────────────────────────


def test_env_provider_resolves_existing_var(monkeypatch):
    monkeypatch.setenv("OC_TEST_X", "hello")
    p = EnvSecretProvider()
    assert p.resolve("OC_TEST_X") == "hello"


def test_env_provider_unknown_var_raises(monkeypatch):
    monkeypatch.delenv("OC_TEST_NOPE", raising=False)
    p = EnvSecretProvider()
    with pytest.raises(SecretProviderError) as exc:
        p.resolve("OC_TEST_NOPE")
    assert "OC_TEST_NOPE" in str(exc.value)


def test_env_provider_blank_var_treated_as_unset(monkeypatch):
    monkeypatch.setenv("OC_TEST_BLANK", "")
    p = EnvSecretProvider()
    with pytest.raises(SecretProviderError):
        p.resolve("OC_TEST_BLANK")


# ─── ExecSecretProvider ───────────────────────────────────────────────


@pytest.fixture
def fake_op(tmp_path: Path):
    """Fake ``op`` CLI that prints whatever is passed via ``--print``.

    Used to exercise the exec provider without requiring a real 1Password
    install. Marked executable so ``shutil.which`` resolves it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    op = bin_dir / "fake-op"
    op.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    --print=*) echo \"${arg#--print=}\";;\n"
        "    --slow) sleep 5;;\n"
        "    --fail) exit 7;;\n"
        "    --huge) yes 'A' | head -c 200000;;\n"
        "  esac\n"
        "done\n"
    )
    op.chmod(op.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return op


def test_exec_provider_runs_validated_binary(fake_op):
    p = ExecSecretProvider(
        command=str(fake_op),
        args_template=["--print={id}"],
        timeout_s=5.0,
    )
    assert p.resolve("hello-world") == "hello-world"


def test_exec_provider_rejects_relative_command(tmp_path):
    with pytest.raises(SecretProviderError) as exc:
        ExecSecretProvider(command="op", args_template=["--print={id}"])
    assert "absolute" in str(exc.value).lower()


def test_exec_provider_rejects_missing_binary(tmp_path):
    with pytest.raises(SecretProviderError) as exc:
        ExecSecretProvider(
            command=str(tmp_path / "does-not-exist"),
            args_template=["--print={id}"],
        )
    assert "not found" in str(exc.value).lower()


def test_exec_provider_propagates_nonzero_exit(fake_op):
    p = ExecSecretProvider(
        command=str(fake_op),
        args_template=["--fail"],
        timeout_s=5.0,
    )
    with pytest.raises(SecretProviderError) as exc:
        p.resolve("anything")
    assert "exit" in str(exc.value).lower()


def test_exec_provider_timeout(fake_op):
    p = ExecSecretProvider(
        command=str(fake_op),
        args_template=["--slow"],
        timeout_s=0.5,
    )
    with pytest.raises(SecretProviderError) as exc:
        p.resolve("anything")
    assert "timeout" in str(exc.value).lower()


def test_exec_provider_caps_output(fake_op):
    p = ExecSecretProvider(
        command=str(fake_op),
        args_template=["--huge"],
        timeout_s=5.0,
        max_output_bytes=4096,
    )
    with pytest.raises(SecretProviderError) as exc:
        p.resolve("anything")
    assert "output" in str(exc.value).lower()


def test_exec_provider_uses_shell_false(fake_op, tmp_path):
    """Shell metacharacters in the secret id must NOT be evaluated.

    We pass an id containing ``;`` and verify the fake binary echoes it
    back verbatim (no command substitution).
    """
    p = ExecSecretProvider(
        command=str(fake_op),
        args_template=["--print={id}"],
        timeout_s=5.0,
    )
    weird = "abc; echo PWNED"
    assert p.resolve(weird) == weird


# ─── SecretRegistry — eager resolve + atomic swap ─────────────────────


def test_registry_resolves_env_specs_eagerly(monkeypatch):
    monkeypatch.setenv("OC_REG_A", "value-a")
    monkeypatch.setenv("OC_REG_B", "value-b")
    reg = SecretRegistry()
    reg.load([
        SecretSpec(id="a", source="env", lookup="OC_REG_A"),
        SecretSpec(id="b", source="env", lookup="OC_REG_B"),
    ])
    assert reg.get("a") == "value-a"
    assert reg.get("b") == "value-b"


def test_registry_eager_resolve_failure_raises(monkeypatch):
    monkeypatch.delenv("OC_REG_MISSING", raising=False)
    reg = SecretRegistry()
    with pytest.raises(SecretProviderError):
        reg.load([
            SecretSpec(id="x", source="env", lookup="OC_REG_MISSING"),
        ])


def test_registry_atomic_swap_keeps_last_known_good(monkeypatch):
    """If reload fails, the previous resolved set must still be live."""
    monkeypatch.setenv("OC_REG_A", "v1")
    reg = SecretRegistry()
    reg.load([SecretSpec(id="a", source="env", lookup="OC_REG_A")])
    assert reg.get("a") == "v1"

    # Now attempt a reload that includes a missing var — should NOT
    # clobber the existing 'a'.
    monkeypatch.delenv("OC_REG_BROKEN", raising=False)
    with pytest.raises(SecretProviderError):
        reg.load([
            SecretSpec(id="a", source="env", lookup="OC_REG_A"),
            SecretSpec(id="broken", source="env", lookup="OC_REG_BROKEN"),
        ])
    # Original value preserved.
    assert reg.get("a") == "v1"
    # Failed-load id is NOT visible.
    assert reg.get("broken") is None


def test_registry_unknown_id_returns_none():
    reg = SecretRegistry()
    assert reg.get("nope") is None


def test_registry_resolve_secret_ref_method(monkeypatch):
    """The registry knows how to resolve a SecretRef-shaped wire dict."""
    monkeypatch.setenv("OC_REG_X", "secret-value")
    reg = SecretRegistry()
    reg.load([SecretSpec(id="x", source="env", lookup="OC_REG_X")])
    payload = {"$secret_ref": "x", "hint": "x"}
    assert reg.resolve_wire(payload) == "secret-value"


def test_registry_resolve_wire_passes_through_non_ref():
    reg = SecretRegistry()
    # Non-secret-ref dict is returned untouched.
    assert reg.resolve_wire({"foo": "bar"}) == {"foo": "bar"}
    assert reg.resolve_wire("plain-string") == "plain-string"


# ─── audit_paths ──────────────────────────────────────────────────────


def test_audit_finds_plaintext_token_field(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "anthropic:\n"
        "  api_key: sk-ant-totally-real-key-shhh\n"
        "telegram:\n"
        "  token: 8123456789:AAAAAAAAA\n"
    )
    findings = audit_paths([cfg])
    kinds = {f.kind for f in findings}
    # Plaintext-looking tokens flagged.
    assert "plaintext_secret" in kinds
    assert any(f.path == cfg for f in findings)


def test_audit_recognises_secret_ref_shape(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "anthropic": {"api_key": {"$secret_ref": "anthropic", "hint": "x"}},
    }))
    findings = audit_paths([cfg])
    kinds = {f.kind for f in findings}
    # Should report the ref but not flag it as plaintext.
    assert "secret_ref_present" in kinds
    assert "plaintext_secret" not in kinds


def test_audit_skips_safe_files(tmp_path):
    cfg = tmp_path / "notes.md"
    cfg.write_text("# Notes\n\nNothing secret here.\n")
    findings = audit_paths([cfg])
    assert findings == []


def test_audit_finding_dataclass_has_meaningful_fields():
    f = AuditFinding(
        kind="plaintext_secret",
        path=Path("/x"),
        detail="api_key looks like a plaintext token",
    )
    assert f.kind == "plaintext_secret"
    assert f.path == Path("/x")
    assert "api_key" in f.detail


def test_audit_skips_missing_paths(tmp_path):
    """Don't crash on a file that doesn't exist; just skip it."""
    findings = audit_paths([tmp_path / "missing.yaml"])
    assert findings == []
