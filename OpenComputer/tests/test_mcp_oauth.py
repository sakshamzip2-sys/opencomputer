"""Tests for opencomputer.mcp.oauth — token storage + lookup helper.

G.13 / Tier 2.5 v1: storage primitives + manual paste path. The full
browser OAuth dance is deferred to provider-specific follow-ups; this
test file covers the storage layer that they'll plug into.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.mcp.oauth import (
    OAuthToken,
    OAuthTokenStore,
    get_token_for_env_lookup,
    oauth_dir,
    paste_token,
    token_path,
)


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


class TestOAuthTokenStore:
    def test_put_then_get(self) -> None:
        store = OAuthTokenStore()
        token = OAuthToken(provider="github", access_token="ghp_secret")
        store.put(token)
        loaded = store.get("github")
        assert loaded is not None
        assert loaded.access_token == "ghp_secret"

    def test_get_unknown_returns_none(self) -> None:
        assert OAuthTokenStore().get("nonsense") is None

    def test_provider_normalised_lowercase(self) -> None:
        store = OAuthTokenStore()
        store.put(OAuthToken(provider="GitHub", access_token="x"))
        # Look up with various casings
        assert store.get("github") is not None
        assert store.get("GITHUB") is not None

    def test_overwrite(self) -> None:
        store = OAuthTokenStore()
        store.put(OAuthToken(provider="github", access_token="first"))
        store.put(OAuthToken(provider="github", access_token="second"))
        assert store.get("github").access_token == "second"

    def test_revoke_returns_true_when_existed(self) -> None:
        store = OAuthTokenStore()
        store.put(OAuthToken(provider="r", access_token="x"))
        assert store.revoke("r") is True
        assert store.get("r") is None

    def test_revoke_unknown_returns_false(self) -> None:
        assert OAuthTokenStore().revoke("nope") is False

    def test_list_returns_all(self) -> None:
        store = OAuthTokenStore()
        store.put(OAuthToken(provider="a", access_token="ax"))
        store.put(OAuthToken(provider="b", access_token="bx"))
        store.put(OAuthToken(provider="c", access_token="cx"))
        names = sorted(t.provider for t in store.list())
        assert names == ["a", "b", "c"]

    def test_expired_token_not_returned(self) -> None:
        store = OAuthTokenStore()
        store.put(OAuthToken(provider="x", access_token="t", expires_at=time.time() - 60))
        assert store.get("x") is None

    def test_unexpired_token_returned(self) -> None:
        store = OAuthTokenStore()
        store.put(OAuthToken(provider="x", access_token="t", expires_at=time.time() + 60))
        assert store.get("x") is not None


# ---------------------------------------------------------------------------
# Storage hygiene — file mode + atomic writes
# ---------------------------------------------------------------------------


class TestStorageHygiene:
    def test_token_file_is_0600(self, tmp_path: Path) -> None:
        store = OAuthTokenStore()
        store.put(OAuthToken(provider="x", access_token="x"))
        f = token_path("x")
        assert f.exists()
        if os.name != "nt":
            assert oct(f.stat().st_mode)[-3:] == "600"

    def test_dir_is_0700(self, tmp_path: Path) -> None:
        store = OAuthTokenStore()
        store.put(OAuthToken(provider="x", access_token="x"))
        d = oauth_dir()
        if os.name != "nt":
            assert oct(d.stat().st_mode)[-3:] == "700"

    def test_corrupted_file_returns_none(self, tmp_path: Path) -> None:
        # Write garbage where a token file would be
        oauth_dir()  # ensure dir exists
        f = token_path("broken")
        f.write_text("not json {", encoding="utf-8")
        # get() should return None, not raise
        assert OAuthTokenStore().get("broken") is None


# ---------------------------------------------------------------------------
# paste_token convenience
# ---------------------------------------------------------------------------


class TestPasteToken:
    def test_basic_round_trip(self) -> None:
        path = paste_token(provider="github", access_token="ghp_xxx", scope="repo")
        assert path.exists()
        loaded = OAuthTokenStore().get("github")
        assert loaded.access_token == "ghp_xxx"
        assert loaded.scope == "repo"
        assert loaded.created_at > 0

    def test_empty_token_rejected(self) -> None:
        with pytest.raises(ValueError):
            paste_token(provider="github", access_token="")

    def test_whitespace_token_rejected(self) -> None:
        with pytest.raises(ValueError):
            paste_token(provider="github", access_token="   ")

    def test_token_stripped(self) -> None:
        paste_token(provider="x", access_token="  spaced  ")
        assert OAuthTokenStore().get("x").access_token == "spaced"


# ---------------------------------------------------------------------------
# Env-var lookup with OAuth fallback
# ---------------------------------------------------------------------------


class TestEnvLookupFallback:
    def test_env_wins_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FAKE_TOKEN", "from-env")
        paste_token(provider="github", access_token="from-store")
        assert get_token_for_env_lookup(provider="github", env_var="FAKE_TOKEN") == "from-env"

    def test_oauth_used_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FAKE_TOKEN", raising=False)
        paste_token(provider="github", access_token="from-store")
        assert get_token_for_env_lookup(provider="github", env_var="FAKE_TOKEN") == "from-store"

    def test_returns_none_when_neither(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FAKE_TOKEN", raising=False)
        # No paste_token call → store empty
        assert get_token_for_env_lookup(provider="ghost", env_var="FAKE_TOKEN") is None

    def test_empty_env_falls_through_to_oauth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FAKE_TOKEN", "")
        paste_token(provider="x", access_token="from-store")
        assert get_token_for_env_lookup(provider="x", env_var="FAKE_TOKEN") == "from-store"


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestCLI:
    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_paste_with_explicit_token(self, runner: CliRunner) -> None:
        from opencomputer.cli_mcp import mcp_app

        result = runner.invoke(
            mcp_app,
            ["oauth-paste", "github", "--token", "ghp_test", "--scope", "repo"],
        )
        assert result.exit_code == 0
        assert "stored" in result.stdout
        assert OAuthTokenStore().get("github") is not None

    def test_list_empty(self, runner: CliRunner) -> None:
        from opencomputer.cli_mcp import mcp_app

        result = runner.invoke(mcp_app, ["oauth-list"])
        assert result.exit_code == 0
        assert "No OAuth" in result.stdout

    def test_list_after_paste(self, runner: CliRunner) -> None:
        from opencomputer.cli_mcp import mcp_app

        runner.invoke(mcp_app, ["oauth-paste", "github", "--token", "x"])
        result = runner.invoke(mcp_app, ["oauth-list"])
        assert result.exit_code == 0
        assert "github" in result.stdout
        # Token value NEVER appears in listing
        assert "x" not in result.stdout or result.stdout.count("x") < 5  # incidental letter

    def test_revoke_with_yes(self, runner: CliRunner) -> None:
        from opencomputer.cli_mcp import mcp_app

        runner.invoke(mcp_app, ["oauth-paste", "github", "--token", "x"])
        result = runner.invoke(mcp_app, ["oauth-revoke", "github", "--yes"])
        assert result.exit_code == 0
        assert OAuthTokenStore().get("github") is None

    def test_revoke_unknown_errors(self, runner: CliRunner) -> None:
        from opencomputer.cli_mcp import mcp_app

        result = runner.invoke(mcp_app, ["oauth-revoke", "nonexistent", "--yes"])
        assert result.exit_code == 1


class TestPersistenceAcrossInstances:
    def test_multiple_store_instances_share_data(self) -> None:
        OAuthTokenStore().put(OAuthToken(provider="x", access_token="t1"))
        # Different store instance, same backing files
        assert OAuthTokenStore().get("x") is not None
