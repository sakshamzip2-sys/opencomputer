"""Named plugin marketplaces (best-of-three Recipe 5).

Covers the registry module (``plugins/marketplaces.py``) and the
``oc plugin marketplace`` / ``oc plugin search`` CLI. Network fetching
is not exercised here — only the registry CRUD, validation, and the
pure catalog-entry normaliser.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_plugin import _catalog_plugin_entries, plugin_app
from opencomputer.plugins.marketplaces import (
    MarketplaceError,
    add_marketplace,
    get_marketplace,
    load_marketplaces,
    remove_marketplace,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch) -> None:
    """marketplaces_path() resolves via _home() → OPENCOMPUTER_HOME."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))


# ── registry CRUD ────────────────────────────────────────────────────


def test_add_then_load_round_trip() -> None:
    add_marketplace("official", "https://example.com/catalog.json")
    items = load_marketplaces()
    assert [m.name for m in items] == ["official"]
    assert items[0].url == "https://example.com/catalog.json"
    assert items[0].added_at > 0


def test_add_records_trust_key() -> None:
    add_marketplace("foo", "https://foo.dev/c.json", trust_key="ab12cd")
    assert get_marketplace("foo").trust_key == "ab12cd"  # type: ignore[union-attr]


def test_add_rejects_invalid_name() -> None:
    with pytest.raises(MarketplaceError):
        add_marketplace("Bad Name", "https://x.dev/c.json")


def test_add_rejects_non_http_url() -> None:
    with pytest.raises(MarketplaceError):
        add_marketplace("foo", "ftp://x.dev/c.json")


def test_add_rejects_duplicate() -> None:
    add_marketplace("foo", "https://a.dev/c.json")
    with pytest.raises(MarketplaceError):
        add_marketplace("foo", "https://b.dev/c.json")


def test_remove_existing_and_missing() -> None:
    add_marketplace("foo", "https://a.dev/c.json")
    assert remove_marketplace("foo") is True
    assert remove_marketplace("foo") is False
    assert load_marketplaces() == []


def test_get_marketplace_case_insensitive() -> None:
    add_marketplace("foo", "https://a.dev/c.json")
    assert get_marketplace("FOO") is not None
    assert get_marketplace("nope") is None


def test_load_on_missing_file_is_empty() -> None:
    assert load_marketplaces() == []


def test_load_tolerates_malformed_file() -> None:
    from opencomputer.plugins.marketplaces import marketplaces_path

    marketplaces_path().write_text(": : not yaml : :", encoding="utf-8")
    assert load_marketplaces() == []


# ── catalog-entry normaliser ─────────────────────────────────────────


def test_catalog_entries_dict_shape() -> None:
    catalog = {"plugins": {"alpha": {"description": "A"}, "beta": {}}}
    entries = dict(_catalog_plugin_entries(catalog))
    assert set(entries) == {"alpha", "beta"}


def test_catalog_entries_list_shape() -> None:
    catalog = {"plugins": [{"id": "alpha", "description": "A"}]}
    entries = dict(_catalog_plugin_entries(catalog))
    assert "alpha" in entries


def test_catalog_entries_missing_plugins_block() -> None:
    assert _catalog_plugin_entries({}) == []


# ── CLI ──────────────────────────────────────────────────────────────


def test_cli_add_then_list() -> None:
    add = runner.invoke(
        plugin_app, ["marketplace", "add", "off", "https://o.dev/c.json"]
    )
    assert add.exit_code == 0
    listing = runner.invoke(plugin_app, ["marketplace", "list"])
    assert listing.exit_code == 0
    assert "off" in listing.stdout


def test_cli_add_bad_url_exits_nonzero() -> None:
    result = runner.invoke(
        plugin_app, ["marketplace", "add", "off", "not-a-url"]
    )
    assert result.exit_code == 1


def test_cli_remove_missing_exits_nonzero() -> None:
    result = runner.invoke(plugin_app, ["marketplace", "remove", "ghost"])
    assert result.exit_code == 1


def test_cli_list_empty_message() -> None:
    result = runner.invoke(plugin_app, ["marketplace", "list"])
    assert result.exit_code == 0
    assert "no marketplaces configured" in result.stdout


def test_cli_search_without_marketplaces_exits_nonzero() -> None:
    result = runner.invoke(plugin_app, ["search", "anything"])
    assert result.exit_code == 1


# ── install <marketplace>/<plugin> routing ───────────────────────────


def test_install_marketplace_prefix_routes_to_that_catalog(
    monkeypatch,
) -> None:
    """`oc plugin install mp1/widget` must resolve `widget` against the
    `mp1` marketplace's catalog URL — not the github/source-policy path."""
    import opencomputer.cli_plugin as cli_plugin

    add_marketplace("mp1", "https://mp1.dev/catalog.json")
    captured: dict = {}

    def _fake_remote(*, slug, profile, is_global, force, refresh, catalog_url):  # noqa: ANN001, ANN003
        captured["slug"] = slug
        captured["catalog_url"] = catalog_url

    monkeypatch.setattr(cli_plugin, "_install_from_remote", _fake_remote)
    result = runner.invoke(plugin_app, ["install", "mp1/widget"])
    assert result.exit_code == 0
    assert captured == {
        "slug": "widget",
        "catalog_url": "https://mp1.dev/catalog.json",
    }


def test_install_unknown_prefix_does_not_route_to_marketplace(
    monkeypatch,
) -> None:
    """A `foo/bar` arg where `foo` is not a registered marketplace must
    NOT hit the marketplace path — it falls through to normal install."""
    import opencomputer.cli_plugin as cli_plugin

    called = {"remote": False}

    def _fake_remote(**_kw):  # noqa: ANN003
        called["remote"] = True

    monkeypatch.setattr(cli_plugin, "_install_from_remote", _fake_remote)
    # no marketplace registered → "ghost/bar" must fall through
    runner.invoke(plugin_app, ["install", "ghost/bar"])
    assert called["remote"] is False
