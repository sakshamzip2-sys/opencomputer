"""Track 1 wiring: --load-extension is auto-added to managed Chrome args.

Wave 6 — verifies that ``build_chrome_launch_args`` includes the
``--load-extension`` flag (and the companion ``DisableLoadExtension-
CommandLineSwitch`` feature suppression) when:

  - ``profile.driver == "managed"`` AND
  - ``extensions/browser-control/extension/dist/background.js`` exists

Otherwise the flag MUST NOT appear (no-op fallback for fresh checkouts
where the extension hasn't been built yet).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from extensions.browser_control.chrome.launch import build_chrome_launch_args
from extensions.browser_control.profiles import resolve_browser_config, resolve_profile

EXT_DIST = (
    Path(__file__).resolve().parent.parent
    / "extensions"
    / "browser-control"
    / "extension"
    / "dist"
)


@pytest.fixture
def built_extension(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Materialize a fake background.js so build_chrome_launch_args sees the
    extension as 'built'. Restores cleanly via tmp_path."""
    fake = tmp_path / "extension" / "dist"
    fake.mkdir(parents=True)
    (fake / "background.js").write_text("// stub for tests\n", encoding="utf-8")
    # Monkeypatch the resolver to return our fake path.
    real_resolve = Path.resolve

    def fake_resolve(self: Path) -> Path:
        # The launch.py code walks .parent.parent — return a synthetic
        # path that resolves to the fake extension dir.
        if "browser-control/chrome/launch.py" in str(self):
            # Simulate the file living at <fake_root>/chrome/launch.py
            # so .parent.parent → <fake_root> and /<fake_root>/extension/dist exists.
            return tmp_path / "chrome" / "launch.py"
        return real_resolve(self)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    return fake


def _opencomputer_profile():
    cfg = resolve_browser_config({})
    return resolve_profile(cfg, "opencomputer")


def _user_profile():
    cfg = resolve_browser_config({})
    return resolve_profile(cfg, "user")


def test_managed_profile_with_built_extension_loads_it(built_extension: Path) -> None:
    """managed profile + built dist/ → --load-extension in args."""
    cfg = resolve_browser_config({})
    profile = _opencomputer_profile()
    assert profile is not None and profile.driver == "managed"

    args = build_chrome_launch_args(cfg, profile, user_data_dir="/tmp/test-user-data")

    # Find the --load-extension arg
    load_ext_args = [a for a in args if a.startswith("--load-extension=")]
    assert len(load_ext_args) == 1, f"expected exactly one --load-extension, got: {load_ext_args}"
    assert str(built_extension) in load_ext_args[0]

    # The DisableLoadExtensionCommandLineSwitch feature should be suppressed.
    disable_features = [a for a in args if a.startswith("--disable-features=")]
    assert len(disable_features) == 1, "exactly one --disable-features expected"
    assert "DisableLoadExtensionCommandLineSwitch" in disable_features[0]


def test_managed_profile_without_built_extension_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """managed profile + no dist/ → no --load-extension flag (silent fallback)."""
    cfg = resolve_browser_config({})
    profile = _opencomputer_profile()
    assert profile is not None

    # Force the dist-exists check to fail.
    real_is_file = Path.is_file

    def fake_is_file(self: Path) -> bool:
        if str(self).endswith("/extension/dist/background.js"):
            return False
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", fake_is_file)

    args = build_chrome_launch_args(cfg, profile, user_data_dir="/tmp/test-user-data")
    assert not any(a.startswith("--load-extension=") for a in args), (
        "--load-extension should NOT appear when dist/background.js is missing"
    )

    # The disable-features flag should still exist with just Translate/MediaRouter.
    disable_features = [a for a in args if a.startswith("--disable-features=")]
    assert len(disable_features) == 1
    assert "DisableLoadExtensionCommandLineSwitch" not in disable_features[0]
    assert "Translate" in disable_features[0]
    assert "MediaRouter" in disable_features[0]


def test_existing_session_profile_does_not_load_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """user profile (driver=existing-session) → never gets --load-extension.

    The user profile attaches to the user's real Chrome (or chrome-mcp's
    spawned Chrome) — we don't control its launch args. The flag would
    be inert anyway, but better to be explicit and skip.

    Note: existing-session profiles have cdp_port=0, so we can't actually
    invoke build_chrome_launch_args on them in production (managed Chrome
    only). This test confirms that even if we did, the load-extension
    branch is gated on ``driver == "managed"``.
    """
    # Simulate a managed-shape profile but with driver overridden to
    # existing-session, just to exercise the gate.
    from dataclasses import replace

    cfg = resolve_browser_config({})
    managed = _opencomputer_profile()
    assert managed is not None
    forced_user = replace(managed, driver="existing-session")

    # Materialize a fake dist so the gate's only barrier is the driver
    # check.
    fake_root = Path(__file__).resolve().parent / ".tmp_extension_check"
    fake_dist = fake_root / "extension" / "dist"
    fake_dist.mkdir(parents=True, exist_ok=True)
    (fake_dist / "background.js").write_text("// stub\n", encoding="utf-8")
    real_resolve = Path.resolve

    def fake_resolve(self: Path) -> Path:
        if "browser-control/chrome/launch.py" in str(self):
            return fake_root / "chrome" / "launch.py"
        return real_resolve(self)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    try:
        args = build_chrome_launch_args(cfg, forced_user, user_data_dir="/tmp/x")
        assert not any(a.startswith("--load-extension=") for a in args), (
            "existing-session profile must not get --load-extension"
        )
    finally:
        # Cleanup
        import shutil

        shutil.rmtree(fake_root, ignore_errors=True)


def test_disable_features_is_a_single_flag() -> None:
    """Chrome only honors the LAST --disable-features; we must emit one flag."""
    cfg = resolve_browser_config({})
    profile = _opencomputer_profile()
    assert profile is not None

    args = build_chrome_launch_args(cfg, profile, user_data_dir="/tmp/x")
    disable_features = [a for a in args if a.startswith("--disable-features=")]
    assert len(disable_features) == 1, (
        f"exactly one --disable-features expected, got: {disable_features}"
    )
