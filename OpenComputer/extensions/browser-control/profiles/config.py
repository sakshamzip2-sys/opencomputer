"""Data structures + constants for the browser port's profile config layer.

Mirrors OpenClaw's config.ts / profile-capabilities.ts type system, idiomized
to Python dataclasses. See docs/refs/openclaw/browser/01-chrome-and-profiles.md
"Data structure field reference" for the original TS shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ─── type aliases ──────────────────────────────────────────────────────

BrowserDriver = Literal["managed", "existing-session", "control-extension"]
BrowserProfileMode = Literal[
    "local-managed",
    "local-existing-session",
    "local-control-extension",
    "remote-cdp",
]


# ─── defaults / palette / regex / port range ──────────────────────────

DEFAULT_OPENCLAW_BROWSER_ENABLED = True
DEFAULT_BROWSER_EVALUATE_ENABLED = True
DEFAULT_OPENCLAW_BROWSER_COLOR = "#FF4500"
DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME = "opencomputer"
DEFAULT_BROWSER_DEFAULT_PROFILE_NAME = "opencomputer"
DEFAULT_USER_BROWSER_PROFILE_NAME = "user"
DEFAULT_USER_BROWSER_PROFILE_COLOR = "#00AA00"

DEFAULT_AI_SNAPSHOT_MAX_CHARS = 80_000
DEFAULT_AI_SNAPSHOT_EFFICIENT_MAX_CHARS = 10_000
DEFAULT_AI_SNAPSHOT_EFFICIENT_DEPTH = 6

DEFAULT_BROWSER_CONTROL_PORT = 18792  # gateway+1 default; see browser-bridge at 18791

CDP_PORT_RANGE_START = 18800
CDP_PORT_RANGE_END = 18899
CDP_PORT_RANGE_SPAN = CDP_PORT_RANGE_END - CDP_PORT_RANGE_START  # exclusive end semantic

DEFAULT_REMOTE_CDP_TIMEOUT_MS = 1500
DEFAULT_REMOTE_CDP_HANDSHAKE_TIMEOUT_MS = 3000

# Names accepted by the create-profile API. Resolver tolerates anything in the
# raw dict (so hand-edited configs with capitals still work) but the API
# rejects names not matching this regex. See deep-dive gotcha #12.
PROFILE_NAME_REGEX = r"^[a-z0-9][a-z0-9-]*$"
MAX_PROFILE_NAME_LENGTH = 64

# 10-color palette for auto-allocation. Starts with the default OpenClaw
# orange, then nine others picked for visual contrast.
PROFILE_COLORS: tuple[str, ...] = (
    "#FF4500",
    "#1F77B4",
    "#2CA02C",
    "#9467BD",
    "#8C564B",
    "#E377C2",
    "#17BECF",
    "#BCBD22",
    "#D62728",
    "#7F7F7F",
)


# ─── dataclasses ───────────────────────────────────────────────────────


@dataclass(slots=True)
class SsrfPolicy:
    dangerously_allow_private_network: bool = False
    allowed_hostnames: list[str] | None = None
    hostname_allowlist: list[str] | None = None


@dataclass(slots=True)
class BrowserProfileConfig:
    """Raw entry from `~/.opencomputer/<profile>/config.yaml` — pre-resolution."""

    cdp_port: int | None = None
    cdp_url: str | None = None
    color: str = DEFAULT_OPENCLAW_BROWSER_COLOR
    driver: BrowserDriver | None = None
    attach_only: bool | None = None
    user_data_dir: str | None = None


@dataclass(slots=True)
class ResolvedBrowserConfig:
    """Top-level browser config after resolution."""

    enabled: bool = DEFAULT_OPENCLAW_BROWSER_ENABLED
    evaluate_enabled: bool = DEFAULT_BROWSER_EVALUATE_ENABLED
    control_port: int = DEFAULT_BROWSER_CONTROL_PORT
    cdp_port_range_start: int = CDP_PORT_RANGE_START
    cdp_port_range_end: int = CDP_PORT_RANGE_END
    cdp_protocol: Literal["http", "https"] = "http"
    cdp_host: str = "127.0.0.1"
    cdp_is_loopback: bool = True
    remote_cdp_timeout_ms: int = DEFAULT_REMOTE_CDP_TIMEOUT_MS
    remote_cdp_handshake_timeout_ms: int = DEFAULT_REMOTE_CDP_HANDSHAKE_TIMEOUT_MS
    color: str = DEFAULT_OPENCLAW_BROWSER_COLOR
    executable_path: str | None = None
    headless: bool = False
    no_sandbox: bool = False
    attach_only: bool = False
    default_profile: str = DEFAULT_BROWSER_DEFAULT_PROFILE_NAME
    profiles: dict[str, BrowserProfileConfig] = field(default_factory=dict)
    ssrf_policy: SsrfPolicy | None = None
    extra_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ResolvedBrowserProfile:
    """One profile after resolution. CDP fields are zero/empty for existing-session."""

    name: str
    cdp_port: int
    cdp_url: str
    cdp_host: str
    cdp_is_loopback: bool
    color: str
    driver: BrowserDriver
    attach_only: bool
    user_data_dir: str | None = None
