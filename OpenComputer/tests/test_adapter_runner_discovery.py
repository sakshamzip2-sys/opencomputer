"""Tests for adapter discovery — walks adapters/ dirs + imports each .py."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_registry():
    from extensions.adapter_runner import clear_registry_for_tests

    clear_registry_for_tests()
    yield
    clear_registry_for_tests()


def _write_adapter(path: Path, site: str, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''"""adapter: {site}/{name}"""
from extensions.adapter_runner import adapter, Strategy

@adapter(
    site="{site}",
    name="{name}",
    description="test",
    domain="example.com",
    strategy=Strategy.PUBLIC,
    columns=["id"],
)
async def run(args, ctx):
    return [{{"id": 1}}]
'''
    )


def test_discover_user_adapters(tmp_path: Path):
    from extensions.adapter_runner._discovery import discover_adapters

    user_root = tmp_path / "user"
    _write_adapter(user_root / "adapters" / "test_site" / "thing.py", "test_site", "thing")
    result = discover_adapters(profile_home=user_root, extensions_root=tmp_path / "no-such")
    names = {(s.site, s.name) for s in result.specs}
    assert ("test_site", "thing") in names


def test_discover_extensions_pack(tmp_path: Path):
    from extensions.adapter_runner._discovery import discover_adapters

    # Mimic an installed adapter-pack plugin: extensions/<plugin>/adapters/<site>/x.py
    ext_root = tmp_path / "extensions"
    plugin_dir = ext_root / "my-pack"
    _write_adapter(plugin_dir / "adapters" / "site_a" / "cmd.py", "site_a", "cmd")

    # Add an empty (no adapters) sibling to ensure the walk skips it.
    (ext_root / "other-plugin").mkdir(parents=True)
    (ext_root / "other-plugin" / "plugin.py").write_text("# no adapters\n")

    result = discover_adapters(profile_home=None, extensions_root=ext_root)
    names = {(s.site, s.name) for s in result.specs}
    assert ("site_a", "cmd") in names


def test_discover_records_import_errors(tmp_path: Path):
    from extensions.adapter_runner._discovery import discover_adapters

    user_root = tmp_path / "user"
    bad = user_root / "adapters" / "broken" / "syntax.py"
    bad.parent.mkdir(parents=True)
    bad.write_text("def def def def\n")  # syntax error

    result = discover_adapters(profile_home=user_root, extensions_root=tmp_path / "noop")
    assert result.errors  # at least one error captured
    assert any("syntax.py" in e for e in result.errors)


def test_bundled_pack_discovered_via_real_extensions_root():
    """The 8 bundled adapters under extensions/browser-control/adapters
    should all import cleanly from the real on-disk path."""
    from pathlib import Path

    from extensions.adapter_runner._discovery import discover_adapters

    # Resolve the actual extensions root so the bundled pack is exercised.
    ext_root = Path(__file__).resolve().parent.parent / "extensions"
    result = discover_adapters(profile_home=None, extensions_root=ext_root)
    sites = {s.site for s in result.specs}
    # All 8 bundled sites should appear.
    expected = {
        "hackernews",
        "arxiv",
        "reddit",
        "github",
        "apple_podcasts",
        "amazon",
        "cursor_app",
        "chatgpt_app",
    }
    assert expected.issubset(sites), (
        f"missing bundled adapters: {expected - sites}; errors: {result.errors}"
    )
