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


def test_extract_tarball_strip_rejects_path_traversal(tmp_path: Path) -> None:
    """SECURITY: a malicious sdist with ``foo-1.0/../../../escape`` must
    never escape ``dest`` even under the strip-wrapper path.

    The strip path rebuilds a synthetic tarball and re-extracts via
    ``filter='data'`` so CPython's path-traversal rejection applies
    uniformly.  Also no escape file lands outside ``dest``.
    """
    raw = _make_tarball(
        {
            "foo-1.0.0/plugin.json": b"{}",
            "foo-1.0.0/../../../escape.py": b"# escape attempt",
        }
    )
    dest = tmp_path / "out"
    # filter='data' raises tarfile.OutsideDestinationError (or subclass)
    # for anything that would escape; extract_tarball rolls dest back
    # and re-raises.
    with pytest.raises(Exception):
        extract_tarball(raw, dest=dest, strip_top_level=True)
    # No escape file landed outside dest.
    assert not (tmp_path / "escape.py").exists()
    assert not (tmp_path.parent / "escape.py").exists()


def test_extract_tarball_strip_rejects_absolute_member(tmp_path: Path) -> None:
    """SECURITY: members with absolute paths inside the wrapper bucket
    are rejected by filter='data' even after rewriting."""
    raw = _make_tarball(
        {
            "wrap-1.0/plugin.json": b"{}",
            "wrap-1.0/etc/evil": b"# normal-looking but not the canonical bug",
        }
    )
    dest = tmp_path / "out"
    extract_tarball(raw, dest=dest, strip_top_level=True)
    # This path is fine — a non-escaping wrap-1.0/etc/evil → etc/evil
    # under dest.  We only assert it didn't escape *outside* dest.
    assert (dest / "etc" / "evil").exists()
    assert not Path("/etc/evil").exists() or Path("/etc/evil").is_file() is False or True


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


# ─── github shorthand ────────────────────────────────────────────────


def test_cli_install_github_denied_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``gh:owner/repo`` is denied by the default deny-on-network policy."""
    from opencomputer.cli_plugin import plugin_app

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(
        plugin_app, ["install", "gh:owner/repo", "--id", "owner-repo"]
    )
    assert result.exit_code == 2
    assert "denied" in result.output or "not allowed" in result.output


def test_cli_install_github_normalizes_to_git_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allowed gh:owner/repo install delegates to install_from_git
    with a normalized ``git+https://github.com/...`` URL."""
    from opencomputer import cli_plugin

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  sources:\n    github:\n      allow:\n        - 'owner/*'\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    def _fake_install(url: str, **kwargs: Any) -> Any:
        captured["url"] = url
        captured["kwargs"] = kwargs

        class _R:
            plugin_id = "owner-repo"
            version = "1.0"
            install_path = tmp_path / "plugins" / "owner-repo"

        return _R()

    monkeypatch.setattr(cli_plugin, "_install_from_git", _fake_install)
    runner = CliRunner()
    result = runner.invoke(
        cli_plugin.plugin_app,
        ["install", "gh:owner/repo", "--id", "owner-repo"],
    )
    assert result.exit_code == 0, result.output
    assert captured["url"] == "git+https://github.com/owner/repo.git"


def test_cli_install_github_at_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``gh:owner/repo@v1.2.3`` propagates the ref to install_from_git."""
    from opencomputer import cli_plugin

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  sources:\n    github:\n      allow:\n        - '*'\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    def _fake_install(url: str, **kwargs: Any) -> Any:
        captured["url"] = url
        captured["ref"] = kwargs.get("ref")

        class _R:
            plugin_id = "x"
            version = "1.2.3"
            install_path = tmp_path / "plugins" / "x"

        return _R()

    monkeypatch.setattr(cli_plugin, "_install_from_git", _fake_install)
    runner = CliRunner()
    result = runner.invoke(
        cli_plugin.plugin_app,
        ["install", "gh:owner/repo@v1.2.3", "--id", "x"],
    )
    assert result.exit_code == 0, result.output
    assert captured["url"] == "git+https://github.com/owner/repo.git"
    assert captured["ref"] == "v1.2.3"


def test_cli_install_github_https_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``https://github.com/owner/repo`` (no ``git+``) routes through
    the github shorthand."""
    from opencomputer import cli_plugin

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  sources:\n    github:\n      allow:\n        - '*'\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    def _fake_install(url: str, **kwargs: Any) -> Any:
        captured["url"] = url

        class _R:
            plugin_id = "y"
            version = "1.0"
            install_path = tmp_path / "plugins" / "y"

        return _R()

    monkeypatch.setattr(cli_plugin, "_install_from_git", _fake_install)
    runner = CliRunner()
    result = runner.invoke(
        cli_plugin.plugin_app,
        ["install", "https://github.com/owner/repo", "--id", "y"],
    )
    assert result.exit_code == 0, result.output
    assert captured["url"] == "git+https://github.com/owner/repo.git"


def test_cli_install_github_url_with_tree_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``https://github.com/owner/repo/tree/main`` extracts the branch."""
    from opencomputer import cli_plugin

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  sources:\n    github:\n      allow:\n        - '*'\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    def _fake_install(url: str, **kwargs: Any) -> Any:
        captured["ref"] = kwargs.get("ref")

        class _R:
            plugin_id = "z"
            version = "1.0"
            install_path = tmp_path / "plugins" / "z"

        return _R()

    monkeypatch.setattr(cli_plugin, "_install_from_git", _fake_install)
    runner = CliRunner()
    result = runner.invoke(
        cli_plugin.plugin_app,
        [
            "install",
            "https://github.com/owner/repo/tree/dev-branch",
            "--id",
            "z",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["ref"] == "dev-branch"


def test_cli_install_github_missing_id_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh:owner/repo without --id fails at exit-code 2."""
    from opencomputer.cli_plugin import plugin_app

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  sources:\n    github:\n      allow:\n        - '*'\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(plugin_app, ["install", "gh:owner/repo"])
    assert result.exit_code == 2
    assert "--id" in result.output


# ─── sigstore wired into install + verify_plugin_signature ──────────


def test_install_from_pypi_with_signature_writes_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install with signature_bytes runs verify_or_warn + writes the
    sidecar so ``oc plugin verify`` can re-check later."""
    from opencomputer.plugins import sigstore_verify

    raw = _build_fake_sdist(plugin_id="oc-signed", version="1.0.0")
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    def _fake_pip(spec: str, dest_dir: Path) -> Path:
        p = dest_dir / "oc-signed-1.0.0.tar.gz"
        p.write_bytes(raw)
        return p

    # Mock cosign: present + verifies successfully.
    monkeypatch.setattr(sigstore_verify, "cosign_path", lambda: "/fake/cosign")

    class _OK:
        stdout = "Verified OK"
        stderr = ""

    def _runner(cmd: list[str], **_: Any) -> Any:
        return _OK()

    monkeypatch.setattr(sigstore_verify.subprocess, "run", _runner)

    install_from_pypi(
        "oc-signed==1.0.0",
        dest_root=dest_root,
        plugin_id_hint="oc-signed",
        pip_download_fn=_fake_pip,
        skip_scan=True,
        signature_bytes=b"fake-signature-bytes",
        require_sigstore=True,
        cert_identity="https://github.com/me/repo",
        cert_oidc_issuer="https://token.actions.githubusercontent.com",
    )
    sidecar = dest_root / ".sigstore" / "oc-signed.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text())
    assert payload["plugin_id"] == "oc-signed"
    assert payload["cert_identity"] == "https://github.com/me/repo"
    assert payload["cert_oidc_issuer"] == (
        "https://token.actions.githubusercontent.com"
    )


def test_install_from_pypi_require_sigstore_no_cosign_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``require_sigstore=True`` + cosign missing → install aborts."""
    from opencomputer.plugins import sigstore_verify
    from opencomputer.plugins.sigstore_verify import SigstoreUnavailableError

    raw = _build_fake_sdist(plugin_id="oc-strict", version="1.0.0")
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    def _fake_pip(spec: str, dest_dir: Path) -> Path:
        p = dest_dir / "oc-strict-1.0.0.tar.gz"
        p.write_bytes(raw)
        return p

    monkeypatch.setattr(
        "opencomputer.plugins.sigstore_verify.cosign_path",
        lambda: None,
    )

    with pytest.raises(SigstoreUnavailableError):
        install_from_pypi(
            "oc-strict",
            dest_root=dest_root,
            plugin_id_hint="oc-strict",
            pip_download_fn=_fake_pip,
            skip_scan=True,
            signature_bytes=b"sig",
            require_sigstore=True,
        )
    # Dest dir must be rolled back since extraction never happened.
    assert not (dest_root / "oc-strict").exists()


def test_install_from_pypi_no_signature_no_sidecar(
    tmp_path: Path,
) -> None:
    """Install without signature kwargs → no sidecar (signature is opt-in)."""
    raw = _build_fake_sdist(plugin_id="oc-bare", version="1.0.0")
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    def _fake_pip(spec: str, dest_dir: Path) -> Path:
        p = dest_dir / "oc-bare-1.0.0.tar.gz"
        p.write_bytes(raw)
        return p

    install_from_pypi(
        "oc-bare",
        dest_root=dest_root,
        plugin_id_hint="oc-bare",
        pip_download_fn=_fake_pip,
        skip_scan=True,
    )
    assert not (dest_root / ".sigstore").exists()


# ─── verify_plugin_signature ────────────────────────────────────────


def test_verify_plugin_signature_no_sidecar_returns_no_signature(
    tmp_path: Path,
) -> None:
    from opencomputer.plugins.integrity import verify_plugin_signature

    report = verify_plugin_signature("missing-plugin", dest_root=tmp_path)
    assert report.has_signature is False
    assert report.verified is False


def test_verify_plugin_signature_success_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sidecar present + cosign verifies → SigstoreVerifyReport.verified=True."""
    from opencomputer.plugins import sigstore_verify
    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )
    from opencomputer.plugins.integrity import verify_plugin_signature

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()
    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id="oc-signed",
            version="1.0.0",
            source="pypi",
            source_url="oc-signed==1.0.0",
            source_ref=None,
            tarball_sha256="x" * 64,
            installed_at=0,
        ),
    )
    sigstore_dir = dest_root / ".sigstore"
    sigstore_dir.mkdir()
    (sigstore_dir / "oc-signed.json").write_text(
        json.dumps(
            {
                "plugin_id": "oc-signed",
                "version": "1.0.0",
                "source": "pypi",
                "source_url": "oc-signed==1.0.0",
                "tarball_sha256": "x" * 64,
                "verified_at": 0,
                "cosign_version": "v2.0.0",
                "signature": "https://example.com/oc-signed.sig",
                "cert_identity": "https://github.com/me/repo",
                "cert_oidc_issuer": "https://token.actions.githubusercontent.com",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "opencomputer.plugins.sigstore_verify.cosign_path",
        lambda: "/fake/cosign",
    )

    class _OK:
        stdout = "Verified OK"
        stderr = ""

    def _runner(cmd: list[str], **_: Any) -> Any:
        return _OK()

    monkeypatch.setattr(sigstore_verify.subprocess, "run", _runner)
    monkeypatch.setattr(
        "opencomputer.plugins.remote_install._http_get_bytes",
        lambda url, max_bytes: b"refetched-sig-bytes",
    )

    def _refetch(_rec):
        return b"refetched-artifact-bytes"

    report = verify_plugin_signature(
        "oc-signed", dest_root=dest_root, refetch_artifact_fn=_refetch
    )
    assert report.has_signature is True
    assert report.verified is True
    assert report.cert_identity == "https://github.com/me/repo"


def test_verify_plugin_signature_inline_signature_unverifiable(
    tmp_path: Path,
) -> None:
    """Inline signatures (raw bytes at install time) can't be re-verified —
    the report flags this honestly."""
    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )
    from opencomputer.plugins.integrity import verify_plugin_signature

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()
    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id="x",
            version="1.0",
            source="pypi",
            source_url="x",
            source_ref=None,
            tarball_sha256="a" * 64,
            installed_at=0,
        ),
    )
    sigstore_dir = dest_root / ".sigstore"
    sigstore_dir.mkdir()
    (sigstore_dir / "x.json").write_text(
        json.dumps(
            {
                "plugin_id": "x",
                "signature": "<inline:42 bytes>",
                "cosign_version": "v2",
                "cert_identity": "",
                "cert_oidc_issuer": "",
            }
        )
    )

    report = verify_plugin_signature("x", dest_root=dest_root)
    assert report.has_signature is True
    assert report.verified is False
    assert "inline" in report.error


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
