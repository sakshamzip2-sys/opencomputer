"""Tests for M11.3 production wiring: pypi installer, policy in cli_plugin,
sigstore wrapper.
"""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from opencomputer.plugins.remote_install import (
    PypiDownloadError,
    PypiNotFoundError,
    extract_tarball,
    install_from_pypi,
)
from opencomputer.plugins.sigstore_verify import (
    SigstoreUnavailableError,
    SigstoreVerification,
    SigstoreVerificationFailedError,
    cosign_path,
    is_required_by_env,
    require_cosign,
    verify_blob,
    verify_or_warn,
)
from opencomputer.plugins.source_policy import (
    PluginSourcePolicy,
    PolicyDeniedError,
    load_policy_from_active_profile,
    parse_source,
)

# ─── extract_tarball strip_top_level ────────────────────────────────


def _make_tarball(entries: dict[str, bytes]) -> bytes:
    """Build an in-memory .tar.gz with the given path → content map."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_extract_tarball_strip_top_level_flattens_wrapper(
    tmp_path: Path,
) -> None:
    raw = _make_tarball(
        {
            "foo-1.0.0/plugin.json": b'{"id":"foo","version":"1.0.0"}',
            "foo-1.0.0/foo.py": b"# plugin",
        }
    )
    dest = tmp_path / "foo"
    extract_tarball(raw, dest=dest, strip_top_level=True)
    assert (dest / "plugin.json").exists()
    assert (dest / "foo.py").exists()
    assert not (dest / "foo-1.0.0").exists()


def test_extract_tarball_strip_top_level_handles_disagreement(
    tmp_path: Path,
) -> None:
    """When members disagree on the wrapper, falls back to flat extract."""
    raw = _make_tarball(
        {
            "foo-1.0.0/plugin.json": b"{}",
            "stray.py": b"# unexpected top-level file",
        }
    )
    dest = tmp_path / "out"
    extract_tarball(raw, dest=dest, strip_top_level=True)
    # Standard tarfile.extractall path activated → wrapper preserved
    assert (dest / "foo-1.0.0").exists() or (dest / "stray.py").exists()


def test_extract_tarball_no_strip_preserves_wrapper(tmp_path: Path) -> None:
    raw = _make_tarball(
        {"wrap/plugin.json": b'{"id":"wrap","version":"1.0.0"}'}
    )
    dest = tmp_path / "out"
    extract_tarball(raw, dest=dest, strip_top_level=False)
    assert (dest / "wrap" / "plugin.json").exists()


# ─── install_from_pypi (dependency-injected) ────────────────────────


def _build_fake_sdist(
    *, plugin_id: str = "test-plugin", version: str = "0.1.0"
) -> bytes:
    """Build a sdist that mirrors PEP-643 layout."""
    manifest = json.dumps(
        {
            "id": plugin_id,
            "name": plugin_id,
            "version": version,
            "kind": "tool",
            "entry": "plugin.py",
        }
    ).encode("utf-8")
    return _make_tarball(
        {
            f"{plugin_id}-{version}/plugin.json": manifest,
            f"{plugin_id}-{version}/plugin.py": b"def register(api): pass\n",
        }
    )


def test_install_from_pypi_extracts_strips_wrapper(tmp_path: Path) -> None:
    raw = _build_fake_sdist(plugin_id="oc-fake", version="1.2.3")
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    def _fake_pip_download(spec: str, dest_dir: Path) -> Path:
        sdist = dest_dir / "oc-fake-1.2.3.tar.gz"
        sdist.write_bytes(raw)
        return sdist

    result = install_from_pypi(
        "oc-fake==1.2.3",
        dest_root=dest_root,
        plugin_id_hint="oc-fake",
        pip_download_fn=_fake_pip_download,
        skip_scan=True,
    )
    assert result.plugin_id == "oc-fake"
    assert result.version == "1.2.3"
    assert result.install_path == dest_root / "oc-fake"
    assert (result.install_path / "plugin.json").exists()


def test_install_from_pypi_records_in_index(tmp_path: Path) -> None:
    raw = _build_fake_sdist(plugin_id="oc-fake")
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    def _fake(spec: str, dest_dir: Path) -> Path:
        p = dest_dir / "oc-fake-0.1.0.tar.gz"
        p.write_bytes(raw)
        return p

    install_from_pypi(
        "oc-fake",
        dest_root=dest_root,
        plugin_id_hint="oc-fake",
        pip_download_fn=_fake,
        skip_scan=True,
    )
    index_path = dest_root / ".installed_index.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text())
    rows = index.get("plugins") if isinstance(index, dict) else index
    assert any(
        r.get("plugin_id") == "oc-fake" and r.get("source") == "pypi"
        for r in (rows or [])
    )


def test_install_from_pypi_id_mismatch_rejected(tmp_path: Path) -> None:
    raw = _build_fake_sdist(plugin_id="oc-real")
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    def _fake(spec: str, dest_dir: Path) -> Path:
        p = dest_dir / "oc-real-0.1.0.tar.gz"
        p.write_bytes(raw)
        return p

    with pytest.raises(Exception, match="oc-real"):
        install_from_pypi(
            "oc-real",
            dest_root=dest_root,
            plugin_id_hint="oc-impostor",  # mismatch
            pip_download_fn=_fake,
            skip_scan=True,
        )
    # Dest dir must NOT exist after rejection
    assert not (dest_root / "oc-impostor").exists()


def test_install_from_pypi_existing_without_force_refused(
    tmp_path: Path,
) -> None:
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()
    (dest_root / "oc-fake").mkdir()  # pre-existing

    def _fake(spec: str, dest_dir: Path) -> Path:
        raise AssertionError("download_fn must not be called")

    with pytest.raises(Exception, match="already installed"):
        install_from_pypi(
            "oc-fake",
            dest_root=dest_root,
            plugin_id_hint="oc-fake",
            force=False,
            pip_download_fn=_fake,
        )


def test_install_from_pypi_pip_download_failure_surfaces(
    tmp_path: Path,
) -> None:
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    def _failing(spec: str, dest_dir: Path) -> Path:
        raise subprocess.CalledProcessError(
            returncode=1, cmd=["pip"], stderr="No matching distribution found"
        )

    with pytest.raises(PypiDownloadError, match="No matching"):
        install_from_pypi(
            "non-existent-plugin",
            dest_root=dest_root,
            plugin_id_hint="non-existent-plugin",
            pip_download_fn=_failing,
        )


def test_install_from_pypi_pip_missing_raises(tmp_path: Path) -> None:
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    def _missing(spec: str, dest_dir: Path) -> Path:
        raise FileNotFoundError("python3 not on PATH")

    with pytest.raises(PypiNotFoundError):
        install_from_pypi(
            "anything",
            dest_root=dest_root,
            plugin_id_hint="anything",
            pip_download_fn=_missing,
        )


# ─── policy in cli_plugin (load_policy_from_active_profile) ─────────


def test_load_policy_from_active_profile_missing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config.yaml → returns default policy (no rules)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    policy = load_policy_from_active_profile()
    assert isinstance(policy, PluginSourcePolicy)
    assert policy.rules == {}


def test_load_policy_from_active_profile_reads_plugins_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  sources:\n    pypi:\n      allow:\n        - oc-*\n",
        encoding="utf-8",
    )
    policy = load_policy_from_active_profile()
    assert policy.is_allowed(parse_source("pypi:oc-thing"))
    assert not policy.is_allowed(parse_source("pypi:other-thing"))


def test_load_policy_from_active_profile_invalid_yaml_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("plugins: [unclosed", encoding="utf-8")
    with pytest.raises(ValueError, match="could not parse"):
        load_policy_from_active_profile()


# ─── cli_plugin.install — policy gate end-to-end ────────────────────


def test_cli_install_pypi_denied_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default deny-by-default policy refuses pypi without explicit allow."""
    from opencomputer.cli_plugin import plugin_app

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(
        plugin_app, ["install", "pypi:some-thing", "--id", "some-thing"]
    )
    assert result.exit_code == 2
    assert "denied" in result.output or "not allowed" in result.output


def test_cli_install_pypi_allowed_when_policy_permits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer import cli_plugin

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  sources:\n    pypi:\n      allow:\n        - oc-fake\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    def _fake_install(spec: str, **kwargs: Any) -> Any:
        captured["spec"] = spec
        captured["kwargs"] = kwargs

        class _Result:
            plugin_id = "oc-fake"
            version = "1.2.3"
            install_path = tmp_path / "plugins" / "oc-fake"

        return _Result()

    monkeypatch.setattr(cli_plugin, "_install_from_pypi", _fake_install)
    runner = CliRunner()
    result = runner.invoke(
        cli_plugin.plugin_app,
        ["install", "pypi:oc-fake", "--id", "oc-fake"],
    )
    assert result.exit_code == 0, result.output
    assert "oc-fake" in result.output
    assert captured["spec"] == "oc-fake"


def test_cli_install_git_denied_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.cli_plugin import plugin_app

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(
        plugin_app,
        ["install", "git+https://example.com/foo/bar.git", "--id", "bar"],
    )
    assert result.exit_code == 2


def test_cli_install_directory_allowed_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory installs are NOT subject to deny-by-default; the CLI
    proceeds (and fails downstream for unrelated reasons in this test)."""
    from opencomputer.cli_plugin import plugin_app

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    src = tmp_path / "myplugin"
    src.mkdir()
    runner = CliRunner()
    result = runner.invoke(plugin_app, ["install", str(src)])
    # exit_code != 2 means the policy gate did NOT block the directory
    # install; downstream plugin.json validation is what fails this test.
    assert result.exit_code != 2 or "denied" not in result.output


def test_cli_install_pypi_missing_id_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pypi:<spec>`` without ``--id`` fails fast at exit-code 2."""
    from opencomputer.cli_plugin import plugin_app

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  sources:\n    pypi:\n      allow:\n        - '*'\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(plugin_app, ["install", "pypi:foo"])
    assert result.exit_code == 2
    assert "--id" in result.output


# ─── sigstore wrapper ──────────────────────────────────────────────


def test_is_required_by_env_default_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OC_PLUGIN_REQUIRE_SIGSTORE", raising=False)
    assert is_required_by_env() is False


def test_is_required_by_env_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for v in ("1", "true", "True", "yes", "ON"):
        monkeypatch.setenv("OC_PLUGIN_REQUIRE_SIGSTORE", v)
        assert is_required_by_env() is True


def test_require_cosign_raises_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opencomputer.plugins.sigstore_verify.cosign_path",
        lambda: None,
    )
    with pytest.raises(SigstoreUnavailableError, match="cosign"):
        require_cosign()


def test_verify_blob_raises_when_artifact_missing(tmp_path: Path) -> None:
    sig = tmp_path / "fake.sig"
    sig.write_bytes(b"")
    with pytest.raises(SigstoreVerificationFailedError, match="artifact"):
        verify_blob(
            tmp_path / "missing-artifact",
            signature_path=sig,
        )


def test_verify_blob_raises_when_signature_missing(tmp_path: Path) -> None:
    artifact = tmp_path / "blob"
    artifact.write_bytes(b"hello")
    with pytest.raises(SigstoreVerificationFailedError, match="signature"):
        verify_blob(artifact, signature_path=tmp_path / "missing.sig")


def test_verify_blob_calls_cosign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful path: cosign returns 0 → SigstoreVerification."""
    artifact = tmp_path / "blob"
    artifact.write_bytes(b"x")
    sig = tmp_path / "blob.sig"
    sig.write_bytes(b"sig-bytes")

    monkeypatch.setattr(
        "opencomputer.plugins.sigstore_verify.cosign_path",
        lambda: "/usr/local/bin/cosign",
    )

    cmds: list[list[str]] = []

    class _OK:
        stdout = "Verified OK"
        stderr = ""

    def _runner(cmd: list[str], **_kw: Any) -> Any:
        cmds.append(cmd)
        return _OK()

    out = verify_blob(
        artifact,
        signature_path=sig,
        cert_identity="https://github.com/owner/repo",
        cert_oidc_issuer="https://token.actions.githubusercontent.com",
        cosign_runner=_runner,
    )
    assert isinstance(out, SigstoreVerification)
    assert out.artifact == str(artifact)
    assert out.signature == str(sig)
    # Command structure
    assert cmds[0][0] == "/usr/local/bin/cosign"
    assert "verify-blob" in cmds[0]
    assert str(sig) in cmds[0]


def test_verify_blob_cosign_nonzero_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "blob"
    artifact.write_bytes(b"x")
    sig = tmp_path / "blob.sig"
    sig.write_bytes(b"sig")
    monkeypatch.setattr(
        "opencomputer.plugins.sigstore_verify.cosign_path",
        lambda: "/cosign",
    )

    def _runner(cmd: list[str], **_kw: Any) -> Any:
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, stderr="bad signature"
        )

    with pytest.raises(SigstoreVerificationFailedError, match="bad signature"):
        verify_blob(artifact, signature_path=sig, cosign_runner=_runner)


def test_verify_or_warn_no_signature_no_require(tmp_path: Path) -> None:
    """No signature provided + not required → returns None silently."""
    out = verify_or_warn(
        tmp_path / "doesnt-matter",
        signature_path=None,
        require=False,
    )
    assert out is None


def test_verify_or_warn_no_signature_required_raises(tmp_path: Path) -> None:
    with pytest.raises(SigstoreVerificationFailedError, match="no signature"):
        verify_or_warn(
            tmp_path / "blob",
            signature_path=None,
            require=True,
        )


def test_verify_or_warn_cosign_missing_not_required_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "blob"
    artifact.write_bytes(b"x")
    sig = tmp_path / "blob.sig"
    sig.write_bytes(b"x")
    monkeypatch.setattr(
        "opencomputer.plugins.sigstore_verify.cosign_path", lambda: None
    )
    out = verify_or_warn(artifact, signature_path=sig, require=False)
    assert out is None


def test_verify_or_warn_cosign_missing_required_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "blob"
    artifact.write_bytes(b"x")
    sig = tmp_path / "blob.sig"
    sig.write_bytes(b"x")
    monkeypatch.setattr(
        "opencomputer.plugins.sigstore_verify.cosign_path", lambda: None
    )
    with pytest.raises(SigstoreUnavailableError):
        verify_or_warn(artifact, signature_path=sig, require=True)


def test_cosign_path_is_cached() -> None:
    """``cosign_path`` is decorated with lru_cache so callers don't pay
    PATH lookups per install."""
    cosign_path.cache_clear()
    a = cosign_path()
    b = cosign_path()
    assert a == b
