"""Unit tests for browser-port `chrome/` (Wave 0c).

Covers:
  - resolve_chrome_executable: per-OS strategy (mocked subprocess + os hooks)
  - read_browser_version + parse_browser_major_version
  - decoration: atomic JSON writes + correct keys + ARGB conversion
  - is_profile_decorated idempotency check
  - build_chrome_launch_args: argv shape + headless/sandbox toggles
  - launch_openclaw_chrome: happy path + readiness timeout (mocked spawn + probe)
  - is_chrome_reachable: HTTP probe with mocked httpx
"""

from __future__ import annotations

import pytest

# `extensions/browser-control/_utils/trash.py` does `from send2trash
# import send2trash` at module top — the import chain triggered by
# `from extensions.browser_control.chrome import ...` below pulls in
# the _utils package and hence trash.py. send2trash lives in the
# optional `[browser]` extras (per pyproject.toml line 142), so on
# minimal CI installs without the extras this whole test file would
# fail to collect with ModuleNotFoundError. Skip cleanly instead.
pytest.importorskip(
    "send2trash",
    reason="install with `pip install opencomputer[browser]` to run",
)

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from extensions.browser_control.chrome import (
    RunningChrome,
    build_chrome_launch_args,
    decorate_openclaw_profile,
    ensure_profile_clean_exit,
    is_chrome_reachable,
    is_profile_decorated,
    parse_browser_major_version,
    parse_hex_rgb_to_signed_argb_int,
    read_browser_version,
    resolve_chrome_executable,
    resolve_openclaw_user_data_dir,
)
from extensions.browser_control.chrome import decoration as decoration_mod
from extensions.browser_control.chrome import executables as executables_mod
from extensions.browser_control.chrome import launch as launch_mod
from extensions.browser_control.chrome.launch import (
    CHROME_LAUNCH_READY_POLL_MS,
    CHROME_LAUNCH_READY_WINDOW_MS,
    ChromeLaunchError,
    launch_openclaw_chrome,
)
from extensions.browser_control.profiles import (
    ResolvedBrowserConfig,
    ResolvedBrowserProfile,
    resolve_browser_config,
    resolve_profile,
)

# ─── helpers ───────────────────────────────────────────────────────────


def _profile() -> ResolvedBrowserProfile:
    cfg = resolve_browser_config({})
    p = resolve_profile(cfg, "opencomputer")
    assert p is not None
    return p


# ─── parse_browser_major_version ───────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Google Chrome 124.0.6367.119", 124),
        ("Chromium 144.0.7136.0", 144),
        ("Brave 1.65.123 (123.0.6312.105)", 123),  # picks LAST version
        ("Chromium 3.0/1.2.3", 1),  # OpenClaw test fixture
        ("no version here", None),
        ("", None),
    ],
)
def test_parse_browser_major_version(raw, expected):
    assert parse_browser_major_version(raw) == expected


def test_read_browser_version_returns_none_on_missing_binary(tmp_path):
    fake = tmp_path / "no-such-chrome"
    assert read_browser_version(fake) is None


def test_read_browser_version_parses_subprocess_output():
    completed = MagicMock(stdout="Google Chrome 124.0.6367.119\n", returncode=0)
    with patch.object(executables_mod.subprocess, "run", return_value=completed):
        assert read_browser_version("/fake/chrome") == "Google Chrome 124.0.6367.119"


# ─── resolve_chrome_executable ─────────────────────────────────────────


def test_resolve_chrome_executable_mac_via_plist_then_osascript():
    with (
        patch.object(executables_mod, "_read_default_http_bundle_id_mac", return_value="com.google.Chrome"),
        patch.object(
            executables_mod, "_resolve_app_path_via_osascript",
            return_value="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ),
    ):
        result = resolve_chrome_executable("darwin")
    assert result == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def test_resolve_chrome_executable_mac_falls_back_to_hardcoded():
    with (
        patch.object(executables_mod, "_read_default_http_bundle_id_mac", return_value=None),
        patch.object(executables_mod, "_scan_hardcoded_paths_mac", return_value="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    ):
        assert resolve_chrome_executable("darwin") == "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"


def test_resolve_chrome_executable_linux_uses_xdg_when_present():
    with (
        patch.object(executables_mod, "_query_xdg_default_browser_linux", return_value="google-chrome.desktop"),
        patch.object(executables_mod, "_parse_desktop_exec_linux", return_value="/usr/bin/google-chrome"),
    ):
        assert resolve_chrome_executable("linux") == "/usr/bin/google-chrome"


def test_resolve_chrome_executable_linux_no_xdg_falls_back():
    with (
        patch.object(executables_mod, "_query_xdg_default_browser_linux", return_value=None),
        patch.object(executables_mod, "_scan_hardcoded_paths_linux", return_value="/usr/bin/chromium"),
    ):
        assert resolve_chrome_executable("linux") == "/usr/bin/chromium"


def test_resolve_chrome_executable_returns_none_when_no_strategy_succeeds():
    with (
        patch.object(executables_mod, "_query_xdg_default_browser_linux", return_value=None),
        patch.object(executables_mod, "_scan_hardcoded_paths_linux", return_value=None),
    ):
        assert resolve_chrome_executable("linux") is None


def test_resolve_chrome_executable_windows_via_registry():
    with (
        patch.object(executables_mod, "_detect_default_chrome_windows", return_value=r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ):
        assert resolve_chrome_executable("win32") == r"C:\Program Files\Google\Chrome\Application\chrome.exe"


# ─── decoration ────────────────────────────────────────────────────────


def test_parse_hex_rgb_to_signed_argb_int_default_orange():
    # #FF4500 with 0xFF alpha = 0xFFFF4500 (signed) = 0xFFFF4500 - 0x100000000 = -45824
    assert parse_hex_rgb_to_signed_argb_int("#FF4500") == 0xFFFF4500 - 0x1_0000_0000


def test_parse_hex_rgb_to_signed_argb_int_handles_no_hash():
    assert parse_hex_rgb_to_signed_argb_int("FF4500") == 0xFFFF4500 - 0x1_0000_0000


@pytest.mark.parametrize("bad", ["", "ZZZZZZ", "#FFF", "#1234567"])
def test_parse_hex_rgb_to_signed_argb_int_rejects_garbage(bad):
    assert parse_hex_rgb_to_signed_argb_int(bad) is None


def test_decoration_writes_via_atomic_json(tmp_path):
    """No direct open('w') — every JSON write must go through atomic_write_json."""
    user_data_dir = tmp_path
    with patch.object(decoration_mod, "atomic_write_json") as mock_atomic:
        decorate_openclaw_profile(str(user_data_dir), name="opencomputer", color="#FF4500")
    # Two writes: Local State + Default/Preferences.
    assert mock_atomic.call_count == 2
    paths_written = {str(call.args[0]) for call in mock_atomic.call_args_list}
    assert any(p.endswith("Local State") for p in paths_written)
    assert any(p.endswith("Default/Preferences") for p in paths_written)


def test_decoration_sets_load_bearing_keys(tmp_path):
    user_data_dir = tmp_path
    decorate_openclaw_profile(str(user_data_dir), name="agent", color="#1F77B4")

    local_state = json.loads((tmp_path / "Local State").read_text())
    info = local_state["profile"]["info_cache"]["Default"]
    assert info["name"] == "agent"
    assert info["shortcut_name"] == "agent"
    assert info["user_name"] == "agent"
    assert info["profile_color"] == "#1F77B4"
    assert info["user_color"] == "#1F77B4"
    assert isinstance(info["profile_color_seed"], int)
    assert isinstance(info["profile_highlight_color"], int)

    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text())
    assert prefs["profile"]["name"] == "agent"
    assert prefs["profile"]["profile_color"] == "#1F77B4"
    assert prefs["profile"]["user_color"] == "#1F77B4"
    assert isinstance(prefs["autogenerated"]["theme"]["color"], int)
    assert isinstance(prefs["browser"]["theme"]["user_color2"], int)


def test_decoration_preserves_unrelated_keys(tmp_path):
    """Decoration must not wipe pre-existing prefs the user / Chrome wrote."""
    user_data_dir = tmp_path
    (tmp_path / "Local State").write_text(json.dumps({"unrelated": {"keep": "me"}}))
    (tmp_path / "Default").mkdir()
    (tmp_path / "Default" / "Preferences").write_text(
        json.dumps({"safebrowsing": {"enabled": True}})
    )

    decorate_openclaw_profile(str(user_data_dir), name="agent", color="#FF4500")

    local_state = json.loads((tmp_path / "Local State").read_text())
    assert local_state["unrelated"] == {"keep": "me"}

    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text())
    assert prefs["safebrowsing"] == {"enabled": True}


def test_is_profile_decorated_idempotent(tmp_path):
    decorate_openclaw_profile(str(tmp_path), name="agent", color="#FF4500")
    assert is_profile_decorated(str(tmp_path), "agent", "#FF4500") is True
    assert is_profile_decorated(str(tmp_path), "different", "#FF4500") is False
    assert is_profile_decorated(str(tmp_path), "agent", "#1F77B4") is False


def test_ensure_profile_clean_exit_writes_normal_keys(tmp_path):
    ensure_profile_clean_exit(str(tmp_path))
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text())
    assert prefs["profile"]["exit_type"] == "Normal"
    assert prefs["profile"]["exited_cleanly"] is True


# ─── build_chrome_launch_args ─────────────────────────────────────────


def test_build_chrome_launch_args_includes_required_flags():
    cfg = resolve_browser_config({})
    profile = resolve_profile(cfg, "opencomputer")
    assert profile is not None
    args = build_chrome_launch_args(cfg, profile, "/tmp/x", headless=False)
    assert f"--remote-debugging-port={profile.cdp_port}" in args
    assert "--user-data-dir=/tmp/x" in args
    assert "--no-first-run" in args
    assert "--password-store=basic" in args


def test_build_chrome_launch_args_headless_toggle():
    cfg = resolve_browser_config({"headless": True})
    profile = resolve_profile(cfg, "opencomputer")
    assert profile is not None
    args = build_chrome_launch_args(cfg, profile, "/tmp/x")
    assert "--headless=new" in args
    assert "--disable-gpu" in args


def test_build_chrome_launch_args_no_sandbox_toggle():
    cfg = resolve_browser_config({"no_sandbox": True})
    profile = resolve_profile(cfg, "opencomputer")
    assert profile is not None
    args = build_chrome_launch_args(cfg, profile, "/tmp/x")
    assert "--no-sandbox" in args
    assert "--disable-setuid-sandbox" in args


def test_build_chrome_launch_args_appends_extra_args_last():
    cfg = resolve_browser_config({"extra_args": ["--enable-logging"]})
    profile = resolve_profile(cfg, "opencomputer")
    assert profile is not None
    args = build_chrome_launch_args(cfg, profile, "/tmp/x")
    assert args[-1] == "--enable-logging"


# ─── resolve_openclaw_user_data_dir ───────────────────────────────────


def test_resolve_openclaw_user_data_dir_uses_profile_name():
    path = resolve_openclaw_user_data_dir("work", base_dir="/tmp/oc-test")
    assert path.endswith("/work/user-data")


# ─── is_chrome_reachable ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_chrome_reachable_true_on_200_json():
    fake_resp = MagicMock(status_code=200)
    fake_resp.json = MagicMock(return_value={"Browser": "Chrome/124"})
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_resp)
    with patch(
        "extensions.browser_control.chrome.lifecycle.httpx.AsyncClient",
        return_value=fake_client,
    ):
        assert await is_chrome_reachable("http://127.0.0.1:18800") is True


@pytest.mark.asyncio
async def test_is_chrome_reachable_false_on_connection_error():
    import httpx

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch(
        "extensions.browser_control.chrome.lifecycle.httpx.AsyncClient",
        return_value=fake_client,
    ):
        assert await is_chrome_reachable("http://127.0.0.1:18800") is False


# ─── launch_openclaw_chrome ──────────────────────────────────────────


def _fake_proc(pid: int = 12345) -> MagicMock:
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.pid = pid
    proc.returncode = None
    proc.stderr = None
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


@pytest.mark.asyncio
async def test_launch_openclaw_chrome_happy_path(tmp_path, monkeypatch):
    cfg = resolve_browser_config({})
    profile = resolve_profile(cfg, "opencomputer")
    assert profile is not None

    udir = tmp_path / "user-data"
    udir.mkdir()
    (udir / "Local State").write_text("{}")
    (udir / "Default").mkdir()
    (udir / "Default" / "Preferences").write_text("{}")

    proc = _fake_proc()

    async def fake_spawn(executable, *args, **kwargs):
        return proc

    monkeypatch.setattr(launch_mod, "resolve_chrome_executable", lambda *a, **k: "/fake/chrome")
    # is_profile_decorated already True after we call decorate_openclaw_profile in setup.
    monkeypatch.setattr(launch_mod, "is_profile_decorated", lambda *a, **k: True)
    monkeypatch.setattr(launch_mod, "ensure_profile_clean_exit", lambda *a, **k: None)

    async def fake_reachable(*args, **kwargs):
        return True

    monkeypatch.setattr(launch_mod, "is_chrome_reachable", fake_reachable)

    running = await launch_openclaw_chrome(
        cfg, profile, user_data_dir=str(udir), spawn=fake_spawn
    )
    assert running.pid == proc.pid
    assert running.user_data_dir == str(udir)
    assert running.cdp_url == profile.cdp_url


@pytest.mark.asyncio
async def test_launch_openclaw_chrome_raises_on_readiness_timeout(tmp_path, monkeypatch):
    cfg = resolve_browser_config({})
    profile = resolve_profile(cfg, "opencomputer")
    assert profile is not None

    udir = tmp_path / "user-data"
    udir.mkdir()
    (udir / "Local State").write_text("{}")
    (udir / "Default").mkdir()
    (udir / "Default" / "Preferences").write_text("{}")

    proc = _fake_proc()

    async def fake_spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr(launch_mod, "resolve_chrome_executable", lambda *a, **k: "/fake/chrome")
    monkeypatch.setattr(launch_mod, "is_profile_decorated", lambda *a, **k: True)
    monkeypatch.setattr(launch_mod, "ensure_profile_clean_exit", lambda *a, **k: None)

    async def never_reachable(*args, **kwargs):
        return False

    monkeypatch.setattr(launch_mod, "is_chrome_reachable", never_reachable)
    # Tiny window so the test is fast.
    monkeypatch.setattr(launch_mod, "CHROME_LAUNCH_READY_WINDOW_MS", 100, raising=True)
    monkeypatch.setattr(launch_mod, "CHROME_LAUNCH_READY_POLL_MS", 50, raising=True)

    with pytest.raises(ChromeLaunchError, match="did not become reachable"):
        await launch_openclaw_chrome(
            cfg, profile, user_data_dir=str(udir), spawn=fake_spawn
        )
    proc.terminate.assert_called()


@pytest.mark.asyncio
async def test_launch_openclaw_chrome_rejects_existing_session(tmp_path):
    cfg = resolve_browser_config({})
    user_profile = resolve_profile(cfg, "user")
    assert user_profile is not None
    with pytest.raises(ChromeLaunchError, match="existing-session"):
        await launch_openclaw_chrome(cfg, user_profile)


@pytest.mark.asyncio
async def test_launch_openclaw_chrome_rejects_non_loopback(monkeypatch):
    cfg = resolve_browser_config(
        {
            "profiles": {
                "remote": {
                    "driver": "managed",
                    "cdp_url": "https://browser.example.com:9222",
                    "color": "#1F77B4",
                }
            }
        }
    )
    profile = resolve_profile(cfg, "remote")
    assert profile is not None
    with pytest.raises(ChromeLaunchError, match="non-loopback"):
        await launch_openclaw_chrome(cfg, profile)


# Confirm imports stayed exported.
def test_running_chrome_dataclass_fields():
    rc = RunningChrome(
        pid=1, executable="/x", user_data_dir="/y", cdp_port=18800,
        cdp_url="http://127.0.0.1:18800", started_at=0.0, proc=None,
    )
    assert rc.pid == 1
    assert rc.cdp_url == "http://127.0.0.1:18800"


def test_constants_present():
    assert CHROME_LAUNCH_READY_WINDOW_MS > 0
    assert CHROME_LAUNCH_READY_POLL_MS > 0


def test_resolved_browser_config_passes_through():
    cfg = ResolvedBrowserConfig()
    assert cfg.headless is False
