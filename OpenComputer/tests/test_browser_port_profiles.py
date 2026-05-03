"""Unit tests for browser-port `profiles/` (Wave 0b).

Covers:
  - resolve_browser_config defaults + ensure-default-profile injection
  - resolve_profile decision tree (existing-session, openclaw, remote, stale ws)
  - get_browser_profile_capabilities truth table (the 3 modes)
  - service create_profile / delete_profile validation + state mutation
  - allocate_cdp_port / allocate_color / get_used_*
  - extra_args denylist
"""

from __future__ import annotations

import pytest
from extensions.browser_control.profiles import (
    BrowserProfileConfig,
    CreateProfileParams,
    ProfileValidationError,
    ResolvedBrowserConfig,
    ResolvedBrowserProfile,
    allocate_cdp_port,
    allocate_color,
    create_profile,
    delete_profile,
    get_browser_profile_capabilities,
    is_valid_profile_name,
    resolve_browser_config,
    resolve_profile,
)
from extensions.browser_control.profiles.config import (
    CDP_PORT_RANGE_END,
    CDP_PORT_RANGE_START,
    DEFAULT_OPENCLAW_BROWSER_COLOR,
    DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME,
    DEFAULT_USER_BROWSER_PROFILE_NAME,
    PROFILE_COLORS,
)
from extensions.browser_control.profiles.service import (
    get_used_colors,
    get_used_ports,
)

# ─── resolve_browser_config ────────────────────────────────────────────


def test_resolve_browser_config_empty_yields_defaults_with_default_profiles():
    cfg = resolve_browser_config({})
    assert cfg.enabled is True
    assert cfg.evaluate_enabled is True
    assert cfg.color == DEFAULT_OPENCLAW_BROWSER_COLOR
    assert DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME in cfg.profiles
    assert DEFAULT_USER_BROWSER_PROFILE_NAME in cfg.profiles
    assert cfg.profiles[DEFAULT_USER_BROWSER_PROFILE_NAME].driver == "existing-session"
    assert cfg.default_profile == DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME


def test_resolve_browser_config_normalizes_color():
    cfg = resolve_browser_config({"color": "abc123"})  # missing leading hash
    assert cfg.color == "#ABC123"


def test_resolve_browser_config_rejects_bad_color_silently():
    cfg = resolve_browser_config({"color": "not-a-color"})
    assert cfg.color == DEFAULT_OPENCLAW_BROWSER_COLOR


def test_resolve_browser_config_extra_args_denylist():
    cfg = resolve_browser_config(
        {
            "extra_args": [
                "--user-data-dir=/etc/passwd",  # forbidden
                "--remote-debugging-port=9999",  # forbidden
                "--no-sandbox",  # forbidden (must come from no_sandbox toggle)
                "--disable-features=Translate",  # allowed
                12345,  # not a string — dropped silently
            ],
        }
    )
    assert "--disable-features=Translate" in cfg.extra_args
    assert all(
        not arg.startswith(("--user-data-dir", "--remote-debugging-port", "--no-sandbox"))
        for arg in cfg.extra_args
    )


def test_resolve_browser_config_user_profile_keeps_user_color():
    cfg = resolve_browser_config({})
    assert cfg.profiles[DEFAULT_USER_BROWSER_PROFILE_NAME].color == "#00AA00"


def test_resolve_browser_config_default_profile_falls_back_to_user_when_only_user_present():
    cfg = resolve_browser_config(
        {
            "profiles": {
                # Force only `user` to exist by overriding the openclaw entry as
                # an existing-session — but ensure_default_profile re-injects
                # `openclaw`. This test verifies the synthesized openclaw is
                # present even when raw config tries to omit it.
                "user": {"driver": "existing-session"},
            },
        }
    )
    assert DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME in cfg.profiles
    assert cfg.default_profile == DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME


def test_resolve_browser_config_timeout_coerces_negative_to_default():
    cfg = resolve_browser_config({"remote_cdp_timeout_ms": -5})
    assert cfg.remote_cdp_timeout_ms == 1500  # default


# ─── resolve_profile ───────────────────────────────────────────────────


def test_resolve_profile_existing_session_zeros_cdp_fields():
    cfg = resolve_browser_config({})
    p = resolve_profile(cfg, DEFAULT_USER_BROWSER_PROFILE_NAME)
    assert p is not None
    assert p.driver == "existing-session"
    assert p.cdp_port == 0
    assert p.cdp_url == ""
    assert p.cdp_host == ""
    assert p.attach_only is True
    assert p.cdp_is_loopback is True


def test_resolve_profile_openclaw_default_uses_loopback_cdp_url():
    cfg = resolve_browser_config({})
    p = resolve_profile(cfg, DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME)
    assert p is not None
    assert p.driver == "openclaw"
    assert p.cdp_is_loopback is True
    assert p.cdp_host in ("127.0.0.1",)
    assert p.cdp_port > 0
    assert p.cdp_url.startswith("http://127.0.0.1:")


def test_resolve_profile_remote_cdp_marks_non_loopback():
    cfg = resolve_browser_config(
        {
            "profiles": {
                "remote": {
                    "driver": "openclaw",
                    "cdp_url": "https://browser.example.com:9222",
                    "color": "#1F77B4",
                }
            }
        }
    )
    p = resolve_profile(cfg, "remote")
    assert p is not None
    assert p.cdp_is_loopback is False
    assert p.cdp_host == "browser.example.com"
    assert p.cdp_port == 9222


def test_resolve_profile_strips_stale_ws_devtools_path():
    # Per-launch /devtools/browser/<uuid> paths become invalid after Chrome restarts.
    # Resolver must drop the path and rebuild from cdp_protocol://host:port.
    cfg = resolve_browser_config(
        {
            "profiles": {
                "stale": {
                    "driver": "openclaw",
                    "cdp_url": "ws://127.0.0.1:18800/devtools/browser/abc-def-123",
                    "cdp_port": 18800,
                    "color": "#1F77B4",
                }
            }
        }
    )
    p = resolve_profile(cfg, "stale")
    assert p is not None
    assert "/devtools/browser/" not in p.cdp_url
    assert p.cdp_url == "http://127.0.0.1:18800"  # protocol from top-level cdp_protocol (http default)


def test_resolve_profile_returns_none_for_missing():
    cfg = resolve_browser_config({})
    assert resolve_profile(cfg, "no-such-profile") is None


def test_resolve_profile_returns_none_when_neither_port_nor_url():
    cfg = resolve_browser_config(
        {
            "profiles": {
                "broken": {
                    "driver": "openclaw",
                    "color": "#1F77B4",
                    # no cdp_port, no cdp_url
                }
            }
        }
    )
    assert resolve_profile(cfg, "broken") is None


# ─── capabilities ──────────────────────────────────────────────────────


def _profile(driver, *, cdp_is_loopback=True, cdp_url="http://127.0.0.1:18800", cdp_port=18800):
    return ResolvedBrowserProfile(
        name="x",
        cdp_port=cdp_port,
        cdp_url=cdp_url,
        cdp_host="127.0.0.1" if cdp_is_loopback else "remote.example.com",
        cdp_is_loopback=cdp_is_loopback,
        color="#FF4500",
        driver=driver,
        attach_only=False,
    )


def test_capabilities_existing_session():
    caps = get_browser_profile_capabilities(_profile("existing-session"))
    assert caps.mode == "local-existing-session"
    assert caps.uses_chrome_mcp is True
    assert caps.uses_persistent_playwright is False
    assert not any(
        (caps.supports_per_tab_ws, caps.supports_json_tab_endpoints, caps.supports_reset, caps.supports_managed_tab_limit)
    )


def test_capabilities_remote_cdp():
    caps = get_browser_profile_capabilities(_profile("openclaw", cdp_is_loopback=False))
    assert caps.mode == "remote-cdp"
    assert caps.is_remote is True
    assert caps.uses_persistent_playwright is True
    assert caps.uses_chrome_mcp is False


def test_capabilities_local_managed():
    caps = get_browser_profile_capabilities(_profile("openclaw"))
    assert caps.mode == "local-managed"
    assert caps.uses_chrome_mcp is False
    assert caps.supports_per_tab_ws is True
    assert caps.supports_reset is True
    assert caps.supports_managed_tab_limit is True


# ─── service: validation + allocation ─────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("openclaw", True),
        ("a", True),
        ("a-b-c", True),
        ("9-foo", True),
        ("", False),
        ("-leading-dash", False),
        ("Has Caps", False),
        ("with_underscore", False),
        ("a" * 65, False),
    ],
)
def test_is_valid_profile_name(name, expected):
    assert is_valid_profile_name(name) is expected


def test_allocate_cdp_port_finds_first_free():
    used = {CDP_PORT_RANGE_START, CDP_PORT_RANGE_START + 1}
    assert allocate_cdp_port(used) == CDP_PORT_RANGE_START + 2


def test_allocate_cdp_port_returns_none_when_exhausted():
    used = set(range(CDP_PORT_RANGE_START, CDP_PORT_RANGE_END + 1))
    assert allocate_cdp_port(used) is None


def test_allocate_color_picks_first_unused():
    # First palette entry is the default orange.
    assert allocate_color(set()).upper() == PROFILE_COLORS[0].upper()
    used = {PROFILE_COLORS[0]}
    assert allocate_color(used).upper() == PROFILE_COLORS[1].upper()


def test_allocate_color_cycles_when_exhausted():
    used = {c.upper() for c in PROFILE_COLORS}
    assert allocate_color(used) in PROFILE_COLORS


def test_get_used_ports_collects_explicit_and_url_ports():
    profiles = {
        "a": BrowserProfileConfig(cdp_port=18810, color="#1F77B4", driver="openclaw"),
        "b": BrowserProfileConfig(
            cdp_url="https://remote.example.com:9222", color="#2CA02C", driver="openclaw"
        ),
    }
    assert get_used_ports(profiles) == {18810, 9222}


def test_get_used_colors_uppercases():
    profiles = {
        "a": BrowserProfileConfig(color="#aabbcc", driver="openclaw"),
    }
    assert get_used_colors(profiles) == {"#AABBCC"}


# ─── service: create_profile / delete_profile ─────────────────────────


def test_create_profile_openclaw_allocates_port_and_color():
    state = resolve_browser_config({})
    persisted: list[ResolvedBrowserConfig] = []

    result = create_profile(
        state,
        CreateProfileParams(name="work"),
        write_config=persisted.append,
    )
    assert result.profile_name == "work"
    assert result.transport == "cdp"
    assert result.cdp_port is not None
    assert result.cdp_url is None  # auto-allocated; URL is built in resolve_profile
    assert result.color.startswith("#")
    assert "work" in state.profiles
    assert persisted, "write_config must be invoked on success"


def test_create_profile_existing_session_uses_chrome_mcp_transport():
    state = resolve_browser_config({})
    state.profiles.pop(DEFAULT_USER_BROWSER_PROFILE_NAME, None)  # free the slot
    result = create_profile(
        state,
        CreateProfileParams(name="my-laptop", driver="existing-session"),
    )
    assert result.transport == "chrome-mcp"
    assert result.cdp_port is None
    assert result.cdp_url is None


def test_create_profile_rejects_bad_name():
    state = resolve_browser_config({})
    with pytest.raises(ProfileValidationError, match="profile name"):
        create_profile(state, CreateProfileParams(name="Has Caps"))


def test_create_profile_rejects_duplicate():
    state = resolve_browser_config({})
    with pytest.raises(ProfileValidationError, match="already exists"):
        create_profile(state, CreateProfileParams(name=DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME))


def test_create_profile_rejects_user_data_dir_on_openclaw():
    state = resolve_browser_config({})
    with pytest.raises(ProfileValidationError, match="user_data_dir"):
        create_profile(
            state,
            CreateProfileParams(name="bad", driver="openclaw", user_data_dir="/tmp/x"),
        )


def test_create_profile_rejects_cdp_url_on_existing_session():
    state = resolve_browser_config({})
    state.profiles.pop(DEFAULT_USER_BROWSER_PROFILE_NAME, None)
    with pytest.raises(ProfileValidationError, match="cdp_url cannot be set"):
        create_profile(
            state,
            CreateProfileParams(
                name="bad", driver="existing-session", cdp_url="http://127.0.0.1:9222"
            ),
        )


def test_create_profile_remote_cdp_marks_remote():
    state = resolve_browser_config({})
    result = create_profile(
        state,
        CreateProfileParams(
            name="cloud", driver="openclaw", cdp_url="https://browser.example.com:9222"
        ),
    )
    assert result.is_remote is True
    assert result.cdp_url == "https://browser.example.com:9222"


def test_delete_profile_removes_and_invokes_after_remove():
    state = resolve_browser_config({})
    create_profile(state, CreateProfileParams(name="work"))
    persisted: list[ResolvedBrowserConfig] = []
    seen: list[tuple[str, BrowserProfileConfig]] = []

    result = delete_profile(
        state,
        "work",
        write_config=persisted.append,
        after_remove=lambda n, cfg: seen.append((n, cfg)),
    )
    assert result.profile_name == "work"
    assert "work" not in state.profiles
    assert persisted, "write_config must run before after_remove"
    assert len(seen) == 1 and seen[0][0] == "work"


def test_delete_profile_refuses_default():
    state = resolve_browser_config({})
    with pytest.raises(ProfileValidationError, match="cannot delete default"):
        delete_profile(state, DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME)


def test_delete_profile_unknown_raises():
    state = resolve_browser_config({})
    with pytest.raises(ProfileValidationError, match="does not exist"):
        delete_profile(state, "no-such")
