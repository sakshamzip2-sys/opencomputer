"""Phase 10f.K/L/M/N — memory-honcho plugin tests.

10f.K (this file — skeleton tests only):
  - Manifest is valid and has the required fields.
  - IMAGE_VERSION file exists and is a non-empty single-line tag.
  - Stub plugin.py register() imports + runs without error.

Later sub-phases (10f.L/M/N) will append tests to this file for the
provider implementation, docker bootstrap, and first-run wizard flow.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

_EXT_DIR = Path(__file__).resolve().parent.parent / "extensions" / "memory-honcho"


class TestHonchoSkeleton:
    def test_plugin_json_exists_and_parses(self):
        manifest_path = _EXT_DIR / "plugin.json"
        assert manifest_path.exists(), f"missing: {manifest_path}"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Required fields
        for key in ("id", "name", "version", "kind", "entry"):
            assert key in data, f"plugin.json missing required field: {key!r}"
        # Sanity on values
        assert data["id"] == "memory-honcho"
        assert data["kind"] == "provider"
        assert data["entry"] == "plugin"

    def test_plugin_json_has_profiles_wildcard(self):
        """Honcho overlay should be available in every profile by default."""
        data = json.loads((_EXT_DIR / "plugin.json").read_text(encoding="utf-8"))
        # The manifest schema added in 14.C will make this field required; for
        # now it's additive — but the plugin's own manifest should declare it
        # explicitly so it passes the future validator unchanged.
        profiles = data.get("profiles")
        assert profiles == ["*"], (
            f"memory-honcho plugin.json profiles should be ['*'] "
            f"(any profile can opt in); got {profiles!r}"
        )

    def test_image_version_file_exists_and_is_a_tag(self):
        tag_path = _EXT_DIR / "IMAGE_VERSION"
        assert tag_path.exists(), f"missing: {tag_path}"
        content = tag_path.read_text(encoding="utf-8").strip()
        assert content, "IMAGE_VERSION file must not be empty"
        # One line, no internal whitespace
        assert "\n" not in content, "IMAGE_VERSION must be a single line"
        assert " " not in content, "IMAGE_VERSION must not contain spaces"

    def test_readme_exists_and_mentions_agpl(self):
        readme = _EXT_DIR / "README.md"
        assert readme.exists(), f"missing: {readme}"
        content = readme.read_text(encoding="utf-8")
        assert "AGPL" in content, "README must acknowledge Honcho's AGPL license"
        assert "Docker" in content, "README must mention Docker prerequisite"

    def _load_plugin_module(self):
        """Load extensions/memory-honcho/plugin.py as a Python module.

        The plugin dir has a hyphen (memory-honcho), so it's not an
        importable package — use importlib.util like the real loader does.
        Requires the parent dir on sys.path for the relative ``.provider``
        import.
        """
        import sys

        parent = str(_EXT_DIR)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        # The relative import `from .provider import ...` inside plugin.py
        # needs a package context — set submodule_search_locations on the
        # synthetic package.
        pkg_name = "_honcho_test_pkg"
        pkg_spec = importlib.machinery.ModuleSpec(
            pkg_name,
            loader=None,
            origin=str(_EXT_DIR),
            is_package=True,
        )
        pkg_spec.submodule_search_locations = [str(_EXT_DIR)]
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys.modules[pkg_name] = pkg

        # Load provider.py first so plugin.py's `.provider` import resolves
        prov_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.provider", _EXT_DIR / "provider.py"
        )
        prov_mod = importlib.util.module_from_spec(prov_spec)
        sys.modules[f"{pkg_name}.provider"] = prov_mod
        prov_spec.loader.exec_module(prov_mod)

        # Now load plugin.py under the package so its relative import works
        plug_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.plugin", _EXT_DIR / "plugin.py"
        )
        plug_mod = importlib.util.module_from_spec(plug_spec)
        sys.modules[f"{pkg_name}.plugin"] = plug_mod
        plug_spec.loader.exec_module(plug_mod)
        return plug_mod

    def test_register_calls_register_memory_provider(self):
        """Phase 10f.L: register() must call api.register_memory_provider with our provider."""
        mod = self._load_plugin_module()
        captured: list = []

        class _FakeAPI:
            def register_memory_provider(self, provider):
                captured.append(provider)

        mod.register(_FakeAPI())
        assert len(captured) == 1
        from plugin_sdk.memory import MemoryProvider

        assert isinstance(captured[0], MemoryProvider)
        assert captured[0].provider_id == "memory-honcho:self-hosted"

    def test_register_gracefully_skips_on_old_core(self):
        """Pre-Phase-10f.G core has no register_memory_provider — must NOT raise."""
        mod = self._load_plugin_module()

        class _OldAPI:
            pass  # no register_memory_provider attribute

        mod.register(_OldAPI())  # must not raise


# ─── Phase 10f.L — HonchoSelfHostedProvider unit tests ─────────────────


def _provider_with_mock(responses: dict):
    """Build a provider backed by a mocked httpx.MockTransport.

    ``responses`` maps ``(method, path)`` tuples to either a dict (JSON
    body, 200 status) or a callable (request) -> httpx.Response.
    """
    import sys

    import httpx

    # Ensure extensions/memory-honcho/ is importable as a package for the
    # test — same pattern as TestHonchoSkeleton._load_plugin_module.
    sys.path.insert(0, str(_EXT_DIR))
    pkg_name = "_honcho_provider_test_pkg"
    pkg_spec = importlib.machinery.ModuleSpec(
        pkg_name, loader=None, origin=str(_EXT_DIR), is_package=True
    )
    pkg_spec.submodule_search_locations = [str(_EXT_DIR)]
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules[pkg_name] = pkg
    prov_spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.provider", _EXT_DIR / "provider.py"
    )
    prov_mod = importlib.util.module_from_spec(prov_spec)
    sys.modules[f"{pkg_name}.provider"] = prov_mod
    prov_spec.loader.exec_module(prov_mod)

    def _handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        entry = responses.get(key)
        if entry is None:
            return httpx.Response(404, json={"error": f"no mock for {key}"})
        if callable(entry):
            return entry(request)
        if isinstance(entry, int):
            return httpx.Response(entry)
        return httpx.Response(200, json=entry)

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(base_url="http://test", transport=transport)
    config = prov_mod.HonchoConfig(
        base_url="http://test",
        workspace="opencomputer",
        host_key="opencomputer",
        context_cadence=1,
        dialectic_cadence=1,  # every turn for simpler tests
    )
    return prov_mod.HonchoSelfHostedProvider(config, http_client=client)


class TestHonchoProvider:
    def test_provider_id(self):
        prov = _provider_with_mock({})
        assert prov.provider_id == "memory-honcho:self-hosted"

    def test_tool_schemas_returns_five_namespaced_tools(self):
        prov = _provider_with_mock({})
        names = {s.name for s in prov.tool_schemas()}
        assert names == {
            "honcho_profile",
            "honcho_search",
            "honcho_context",
            "honcho_reasoning",
            "honcho_conclude",
        }

    def test_health_check_ok(self):
        import asyncio

        prov = _provider_with_mock({("GET", "/health"): {"status": "ok"}})
        assert asyncio.run(prov.health_check()) is True

    def test_health_check_non_200_returns_false(self):
        import asyncio

        import httpx

        def _fail(request):
            return httpx.Response(500)

        prov = _provider_with_mock({("GET", "/health"): _fail})
        assert asyncio.run(prov.health_check()) is False

    def test_prefetch_returns_context_string(self):
        import asyncio

        prov = _provider_with_mock(
            {("POST", "/v1/context"): {"context": "User prefers terse replies."}}
        )
        result = asyncio.run(prov.prefetch("hello", turn_index=0))
        assert result == "User prefers terse replies."

    def test_prefetch_returns_none_off_cycle(self):
        """With context_cadence=2, turn_index=1 should skip."""
        import asyncio

        import httpx

        sys_mod = __import__("sys")
        sys_mod.path.insert(0, str(_EXT_DIR))
        pkg_name = "_honcho_cadence_test_pkg"
        pkg_spec = importlib.machinery.ModuleSpec(
            pkg_name, loader=None, origin=str(_EXT_DIR), is_package=True
        )
        pkg_spec.submodule_search_locations = [str(_EXT_DIR)]
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys_mod.modules[pkg_name] = pkg
        prov_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.provider", _EXT_DIR / "provider.py"
        )
        prov_mod = importlib.util.module_from_spec(prov_spec)
        sys_mod.modules[f"{pkg_name}.provider"] = prov_mod
        prov_spec.loader.exec_module(prov_mod)

        called = []

        def _handler(request):
            called.append(request.url.path)
            return httpx.Response(200, json={"context": "x"})

        client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(_handler))
        config = prov_mod.HonchoConfig(
            base_url="http://test", context_cadence=2, dialectic_cadence=2
        )
        prov = prov_mod.HonchoSelfHostedProvider(config, http_client=client)

        # turn_index=0 → 0%2==0 → runs
        r = asyncio.run(prov.prefetch("q", turn_index=0))
        assert r == "x"
        # turn_index=1 → 1%2==1 → skips (no HTTP call)
        r = asyncio.run(prov.prefetch("q", turn_index=1))
        assert r is None
        assert len(called) == 1  # only the first call hit the server

    def test_sync_turn_posts_to_messages_endpoint(self):
        import asyncio

        captured = []

        import httpx

        def _handler(request):
            import json as _json

            captured.append((request.url.path, _json.loads(request.content)))
            return httpx.Response(200, json={})

        sys_mod = __import__("sys")
        sys_mod.path.insert(0, str(_EXT_DIR))
        pkg_name = "_honcho_sync_test_pkg"
        pkg_spec = importlib.machinery.ModuleSpec(
            pkg_name, loader=None, origin=str(_EXT_DIR), is_package=True
        )
        pkg_spec.submodule_search_locations = [str(_EXT_DIR)]
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys_mod.modules[pkg_name] = pkg
        prov_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.provider", _EXT_DIR / "provider.py"
        )
        prov_mod = importlib.util.module_from_spec(prov_spec)
        sys_mod.modules[f"{pkg_name}.provider"] = prov_mod
        prov_spec.loader.exec_module(prov_mod)

        client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(_handler))
        config = prov_mod.HonchoConfig(base_url="http://test", dialectic_cadence=1)
        prov = prov_mod.HonchoSelfHostedProvider(config, http_client=client)

        asyncio.run(prov.sync_turn("hello", "hi back", turn_index=0))
        assert len(captured) == 1
        path, body = captured[0]
        assert path == "/v1/messages"
        assert body["user"] == "hello"
        assert body["assistant"] == "hi back"
        assert body["turn_index"] == 0

    def test_handle_tool_call_routes_by_name(self):
        import asyncio

        from plugin_sdk.core import ToolCall

        prov = _provider_with_mock(
            {
                ("GET", "/v1/profile"): {"summary": "Saksham, India timezone"},
                (
                    "POST",
                    "/v1/search",
                ): {"text": "three matches about python"},
            }
        )

        result = asyncio.run(
            prov.handle_tool_call(
                ToolCall(id="1", name="honcho_profile", arguments={"peer": "user"})
            )
        )
        assert result.is_error is False
        assert "Saksham" in result.content

        result = asyncio.run(
            prov.handle_tool_call(
                ToolCall(
                    id="2",
                    name="honcho_search",
                    arguments={"query": "python"},
                )
            )
        )
        assert result.is_error is False
        assert "python" in result.content

    def test_handle_tool_call_unknown_name_returns_error(self):
        import asyncio

        from plugin_sdk.core import ToolCall

        prov = _provider_with_mock({})
        result = asyncio.run(
            prov.handle_tool_call(ToolCall(id="x", name="not_a_tool", arguments={}))
        )
        assert result.is_error is True
        assert "unknown" in result.content.lower()

    def test_all_errors_return_tool_result_not_raise(self):
        """Network failure must NOT raise — returns is_error=True."""
        import asyncio

        import httpx

        def _boom(request):
            raise httpx.ConnectError("nothing listening")

        sys_mod = __import__("sys")
        sys_mod.path.insert(0, str(_EXT_DIR))
        pkg_name = "_honcho_err_test_pkg"
        pkg_spec = importlib.machinery.ModuleSpec(
            pkg_name, loader=None, origin=str(_EXT_DIR), is_package=True
        )
        pkg_spec.submodule_search_locations = [str(_EXT_DIR)]
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys_mod.modules[pkg_name] = pkg
        prov_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.provider", _EXT_DIR / "provider.py"
        )
        prov_mod = importlib.util.module_from_spec(prov_spec)
        sys_mod.modules[f"{pkg_name}.provider"] = prov_mod
        prov_spec.loader.exec_module(prov_mod)

        client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(_boom))
        config = prov_mod.HonchoConfig(base_url="http://test")
        prov = prov_mod.HonchoSelfHostedProvider(config, http_client=client)

        from plugin_sdk.core import ToolCall

        result = asyncio.run(
            prov.handle_tool_call(ToolCall(id="1", name="honcho_profile", arguments={}))
        )
        assert result.is_error is True
        assert "nothing listening" in result.content or "Honcho" in result.content

    def test_sync_turn_swallows_network_errors(self):
        """sync_turn is fire-and-forget — must never raise."""
        import asyncio

        import httpx

        def _boom(request):
            raise httpx.ConnectError("down")

        sys_mod = __import__("sys")
        sys_mod.path.insert(0, str(_EXT_DIR))
        pkg_name = "_honcho_syncerr_test_pkg"
        pkg_spec = importlib.machinery.ModuleSpec(
            pkg_name, loader=None, origin=str(_EXT_DIR), is_package=True
        )
        pkg_spec.submodule_search_locations = [str(_EXT_DIR)]
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys_mod.modules[pkg_name] = pkg
        prov_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.provider", _EXT_DIR / "provider.py"
        )
        prov_mod = importlib.util.module_from_spec(prov_spec)
        sys_mod.modules[f"{pkg_name}.provider"] = prov_mod
        prov_spec.loader.exec_module(prov_mod)

        client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(_boom))
        config = prov_mod.HonchoConfig(base_url="http://test", dialectic_cadence=1)
        prov = prov_mod.HonchoSelfHostedProvider(config, http_client=client)

        # Must not raise
        asyncio.run(prov.sync_turn("u", "a", turn_index=0))
