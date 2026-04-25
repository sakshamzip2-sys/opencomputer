"""PR-2: integration tests verifying OpenCLI tools require consent + publish to bus.

Tests cover:
    1. Every tool class declares at least one CapabilityClaim.
    2. A successful scrape causes default_bus to receive a WebObservationEvent.
    3. Bus publish failure never breaks the scrape result.
    4. plugin.json still has enabled_by_default=false.
    5. plugin.py::register() no longer returns early — calls api.register_tool() 3×.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── resolve plugin dir and load tool classes without polluting sys.modules ───────

_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "extensions" / "opencli-scraper"
_PLUGIN_DIR_STR = str(_PLUGIN_DIR)


def _load_plugin_dir_module(rel_name: str, qualified_name: str):
    """Load a module from _PLUGIN_DIR using a qualified sys.modules key.

    Using a qualified name (e.g. ``extensions.opencli_scraper.tools``) instead
    of the bare file stem (``tools``) prevents the module from shadowing other
    packages that use the same short name (e.g. the scaffold smoke-check's
    temporary ``tools/`` sub-package). Both the extension package hierarchy
    and the short-name alias are registered in sys.modules so intra-plugin
    relative imports work correctly.
    """
    # Ensure parent package namespace exists
    _ensure_namespace("extensions", _PLUGIN_DIR.parent)
    _ensure_namespace("extensions.opencli_scraper", _PLUGIN_DIR)

    if qualified_name in sys.modules:
        return sys.modules[qualified_name]

    # The module itself needs the plugin dir in sys.path so its own sibling
    # imports (field_whitelist, rate_limiter, etc.) resolve. We temporarily
    # add it only while exec-ing the module.
    added = _PLUGIN_DIR_STR not in sys.path
    if added:
        sys.path.insert(0, _PLUGIN_DIR_STR)
    try:
        spec = importlib.util.spec_from_file_location(
            qualified_name, str(_PLUGIN_DIR / f"{rel_name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "extensions.opencli_scraper"
        sys.modules[qualified_name] = mod
        spec.loader.exec_module(mod)
    finally:
        if added and _PLUGIN_DIR_STR in sys.path:
            sys.path.remove(_PLUGIN_DIR_STR)
    return mod


def _ensure_namespace(name: str, path: Path) -> None:
    if name not in sys.modules:
        ns = types.ModuleType(name)
        ns.__path__ = [str(path)]
        ns.__package__ = name
        sys.modules[name] = ns


# Load tool classes under qualified names to avoid bare-name sys.modules pollution
_tools_mod = _load_plugin_dir_module("tools", "extensions.opencli_scraper.tools")
ScrapeRawTool = _tools_mod.ScrapeRawTool
FetchProfileTool = _tools_mod.FetchProfileTool
MonitorPageTool = _tools_mod.MonitorPageTool

from plugin_sdk.consent import CapabilityClaim  # noqa: E402
from plugin_sdk.core import ToolCall  # noqa: E402

# ── helpers ──────────────────────────────────────────────────────────────────────


def _make_mocks(wrapper_data=None, robots_allowed: bool = True):
    wrapper = MagicMock()
    wrapper.run = AsyncMock(return_value=wrapper_data or {"login": "octocat", "name": "Mona"})
    rate_limiter = MagicMock()
    rate_limiter.acquire = AsyncMock(return_value=None)
    robots = MagicMock()
    robots.allowed = AsyncMock(return_value=robots_allowed)
    return wrapper, rate_limiter, robots


# ── 1. Capability claims ─────────────────────────────────────────────────────────


def test_each_tool_declares_capability_claim():
    """Every OpenCLI tool exposes a capability_claims tuple with at least one claim."""
    for cls in (ScrapeRawTool, FetchProfileTool, MonitorPageTool):
        claims = getattr(cls, "capability_claims", ())
        assert len(claims) >= 1, f"{cls.__name__} missing capability_claims"
        # Claims must be CapabilityClaim instances
        assert isinstance(claims[0], CapabilityClaim), (
            f"{cls.__name__} claims[0] is not a CapabilityClaim"
        )
        # Each claim has a stable id that namespaces under opencli_scraper.*
        assert claims[0].capability_id.startswith("opencli_scraper."), (
            f"{cls.__name__} claim capability_id should namespace under opencli_scraper.*"
        )


def test_capability_claim_fields_are_populated():
    """CapabilityClaim fields (tier_required, human_description) are non-empty."""
    from plugin_sdk.consent import ConsentTier

    for cls in (ScrapeRawTool, FetchProfileTool, MonitorPageTool):
        claim = cls.capability_claims[0]
        assert claim.tier_required is not None, f"{cls.__name__} claim missing tier_required"
        assert isinstance(claim.tier_required, ConsentTier)
        assert claim.human_description, f"{cls.__name__} claim missing human_description"


def test_scrape_raw_capability_id():
    assert ScrapeRawTool.capability_claims[0].capability_id == "opencli_scraper.scrape_raw"


def test_fetch_profile_capability_id():
    assert FetchProfileTool.capability_claims[0].capability_id == "opencli_scraper.fetch_profile"


def test_monitor_page_capability_id():
    assert MonitorPageTool.capability_claims[0].capability_id == "opencli_scraper.monitor_page"


# ── 2. Successful scrape publishes WebObservationEvent ──────────────────────────


@pytest.fixture()
def isolated_bus():
    """Provide a fresh bus, then restore the module singleton afterwards.

    Using reset_default_bus() without restoration mutates the module-level
    ``default_bus`` attribute permanently, which breaks the singleton identity
    assertion in test_typed_event_bus.py when tests run together.
    """
    import opencomputer.ingestion.bus as bus_mod
    from opencomputer.ingestion.bus import TypedEventBus

    original = bus_mod.default_bus
    fresh = TypedEventBus()
    bus_mod.default_bus = fresh
    yield fresh
    bus_mod.default_bus = original


@pytest.mark.asyncio
async def test_successful_scrape_publishes_web_observation_event(isolated_bus):
    """A successful scrape causes default_bus to receive a WebObservationEvent."""
    from plugin_sdk.ingestion import WebObservationEvent

    bus = isolated_bus
    received: list[WebObservationEvent] = []
    bus.subscribe("web_observation", lambda ev: received.append(ev))

    wrapper, rate_limiter, robots = _make_mocks()
    tool = FetchProfileTool(wrapper=wrapper, rate_limiter=rate_limiter, robots_cache=robots)

    result = await tool.execute(ToolCall(
        id="c1", name="FetchProfile", arguments={"platform": "github", "user": "octocat"},
    ))
    assert not result.is_error, f"Tool returned error: {result.content}"

    assert len(received) == 1, f"expected 1 web_observation event, got {len(received)}"
    ev = received[0]
    assert isinstance(ev, WebObservationEvent)
    assert ev.event_type == "web_observation"
    assert ev.source == "opencli-scraper"
    assert ev.url.startswith("https://")
    assert ev.payload_size_bytes >= 0


@pytest.mark.asyncio
async def test_scrape_raw_publishes_event(isolated_bus):
    """ScrapeRawTool also publishes a WebObservationEvent on success."""
    bus = isolated_bus
    received = []
    bus.subscribe("web_observation", lambda ev: received.append(ev))

    wrapper, rate_limiter, robots = _make_mocks(wrapper_data={"data": {"login": "octocat"}})
    tool = ScrapeRawTool(wrapper=wrapper, rate_limiter=rate_limiter, robots_cache=robots)

    result = await tool.execute(ToolCall(
        id="c2", name="ScrapeRaw", arguments={"adapter": "github/user", "args": ["octocat"]},
    ))
    assert not result.is_error, f"Tool returned error: {result.content}"

    assert len(received) == 1
    assert received[0].source == "opencli-scraper"


# ── 3. Bus failure does not break the tool ───────────────────────────────────────


@pytest.mark.asyncio
async def test_bus_publish_failure_does_not_break_tool():
    """If the bus publish raises, the scrape result is still returned."""
    from unittest.mock import patch

    wrapper, rate_limiter, robots = _make_mocks()
    tool = FetchProfileTool(wrapper=wrapper, rate_limiter=rate_limiter, robots_cache=robots)

    with patch("opencomputer.ingestion.bus.get_default_bus") as mock_get_bus:
        mock_bus = MagicMock()
        mock_bus.publish = MagicMock(side_effect=RuntimeError("bus boom"))
        mock_get_bus.return_value = mock_bus

        result = await tool.execute(ToolCall(
            id="c3", name="FetchProfile", arguments={"platform": "github", "user": "octocat"},
        ))
    # Tool execute did NOT crash; result returned despite bus failure
    assert not result.is_error, f"Tool crashed when bus failed: {result.content}"


# ── 4. plugin.json still disabled by default ─────────────────────────────────────


def test_plugin_manifest_still_disabled_by_default():
    """enabled_by_default MUST stay false until user's legal review."""
    manifest_path = _PLUGIN_DIR / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest.get("enabled_by_default") is False, (
        "PR-2 explicitly leaves enabled_by_default=false until user's legal review"
    )


# ── 5. plugin.py register() calls api.register_tool() 3 times ───────────────────


def _load_plugin_module():
    """Load extensions/opencli-scraper/plugin.py fresh from its absolute path.

    Avoids relying on the bare name 'plugin' in sys.modules, which other test
    modules may have populated with a different plugin's module object.
    """
    import importlib.util

    plugin_path = _PLUGIN_DIR / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "extensions.opencli_scraper.plugin", str(plugin_path)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plugin_register_actually_registers_tools():
    """plugin.py::register no longer returns early — it calls api.register_tool() 3 times."""
    plugin = _load_plugin_module()
    api = MagicMock()
    plugin.register(api)

    assert api.register_tool.call_count == 3, (
        f"Expected 3 register_tool calls, got {api.register_tool.call_count}"
    )
    # Each call passes a BaseTool instance whose .schema.name is one of the expected
    names = sorted(call.args[0].schema.name for call in api.register_tool.call_args_list)
    assert names == ["FetchProfile", "MonitorPage", "ScrapeRaw"], (
        f"Unexpected tool names registered: {names}"
    )


def test_plugin_register_tools_have_capability_claims():
    """Tools registered by plugin.register() all carry capability_claims."""
    plugin = _load_plugin_module()
    api = MagicMock()
    plugin.register(api)

    for call in api.register_tool.call_args_list:
        tool = call.args[0]
        claims = getattr(type(tool), "capability_claims", ())
        assert len(claims) >= 1, f"{type(tool).__name__} registered without capability_claims"
