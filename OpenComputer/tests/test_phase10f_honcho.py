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


# ─── Phase 10f.M — docker-compose + setup/status/reset CLI ──────────────


def _load_bootstrap():
    """Load extensions/memory-honcho/bootstrap.py directly.

    Must register in sys.modules BEFORE exec_module so that dataclasses
    with slots=True can find their module via cls.__module__ lookup.
    """
    import importlib.util
    import sys

    mod_name = "_honcho_bootstrap_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _EXT_DIR / "bootstrap.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestDockerComposeFile:
    def test_compose_file_exists_and_binds_to_localhost(self):
        """Honcho MUST NOT be exposed to 0.0.0.0 — personal agent only."""
        compose = _EXT_DIR / "docker-compose.yml"
        assert compose.exists(), f"missing: {compose}"
        content = compose.read_text(encoding="utf-8")
        # Must bind API port to 127.0.0.1 only
        assert "127.0.0.1:8000:8000" in content
        # Postgres and Redis must NOT expose ports at all (the API talks to
        # them inside the compose network)
        assert "5432:5432" not in content, "postgres should not expose its port"
        assert "6379:6379" not in content, "redis should not expose its port"
        # Must include mem_limit
        assert "mem_limit" in content


class TestBootstrapHelpers:
    def test_detect_docker_when_missing(self, monkeypatch):
        bootstrap = _load_bootstrap()

        # shutil.which returns None for missing binary
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        docker, compose_v2 = bootstrap.detect_docker()
        assert docker is False
        assert compose_v2 is False

    def test_detect_docker_when_present(self, monkeypatch):
        bootstrap = _load_bootstrap()
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")

        def _fake_run(args, **kwargs):
            class _R:
                returncode = 0

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        docker, compose_v2 = bootstrap.detect_docker()
        assert docker is True
        assert compose_v2 is True

    def test_honcho_up_refuses_without_docker(self, monkeypatch):
        bootstrap = _load_bootstrap()
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        ok, msg = bootstrap.honcho_up()
        assert ok is False
        assert "not installed" in msg.lower()

    def test_honcho_up_invokes_docker_compose(self, monkeypatch):
        bootstrap = _load_bootstrap()
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")

        called = []

        def _fake_run(args, **kwargs):
            called.append(args)

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)

        ok, msg = bootstrap.honcho_up()
        assert ok is True
        # First call was `docker compose version`, second was `docker compose -f ... up -d`
        compose_ups = [c for c in called if "up" in c and "-d" in c]
        assert len(compose_ups) >= 1
        # Must target our docker-compose.yml
        assert any(str(bootstrap.COMPOSE_FILE) in c for c in compose_ups)

    def test_honcho_reset_uses_down_v(self, monkeypatch):
        bootstrap = _load_bootstrap()
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")

        called = []

        def _fake_run(args, **kwargs):
            called.append(args)

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)

        ok, _ = bootstrap.honcho_reset()
        assert ok is True
        # Must include -v to wipe volumes
        resets = [c for c in called if "down" in c and "-v" in c]
        assert len(resets) == 1


class TestMemorySetupStatusResetCLI:
    def _invoke(self, subcommand, *args, monkeypatch=None):
        """Run `opencomputer memory <subcommand> <args>` via CliRunner."""
        from typer.testing import CliRunner

        from opencomputer.cli_memory import memory_app

        runner = CliRunner()
        return runner.invoke(memory_app, [subcommand, *args])

    def test_setup_docker_missing_exits_cleanly(self, monkeypatch):
        """Docker missing → setup prints hint, returns 0 (does NOT crash)."""
        # Patch the bootstrap's detect_docker to simulate Docker missing.
        # We do this by stubbing shutil.which in the imported bootstrap.
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = self._invoke("setup")
        # Exit 0 because we printed a hint and bailed — did NOT crash.
        assert result.exit_code == 0
        assert "docker" in result.stdout.lower()

    def test_status_reports_docker_state(self, monkeypatch):
        import shutil

        # No docker → should clearly say so
        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = self._invoke("status")
        assert result.exit_code == 0
        assert "docker" in result.stdout.lower()

    def test_reset_asks_confirmation(self, monkeypatch):
        """Reset without --yes prompts for confirmation; declining aborts."""
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
        import subprocess

        def _fake_run(args, **kwargs):
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)

        # Respond "n" to the confirmation prompt
        from typer.testing import CliRunner

        from opencomputer.cli_memory import memory_app

        runner = CliRunner()
        result = runner.invoke(memory_app, ["reset"], input="n\n")
        assert "abort" in result.stdout.lower()


# ─── Phase 10f.N — first-run wizard Honcho step ────────────────────────


class TestWizardHonchoStep:
    """The setup wizard's optional Honcho step (10f.N)."""

    def test_optional_honcho_skipped_by_user_returns_cleanly(self, monkeypatch):
        """If the user answers 'no' to Honcho, the wizard proceeds without error."""
        import opencomputer.setup_wizard as wizard

        # Confirm.ask is used to prompt — stub to return False ("skipped")
        class _FakeConfirm:
            @staticmethod
            def ask(question, default=True):
                return False

        monkeypatch.setattr(wizard, "Confirm", _FakeConfirm)

        # Should not raise
        wizard._optional_honcho()

    def test_optional_honcho_docker_missing_prints_hint(self, monkeypatch):
        """If the user says yes but Docker is missing, wizard prints install hint."""
        import opencomputer.setup_wizard as wizard

        class _FakeConfirm:
            @staticmethod
            def ask(question, default=True):
                return True

        monkeypatch.setattr(wizard, "Confirm", _FakeConfirm)

        # Patch the bootstrap loader to return a stub whose detect_docker
        # returns (False, False).
        from types import SimpleNamespace

        stub_bootstrap = SimpleNamespace(
            detect_docker=lambda: (False, False),
            honcho_up=lambda: (True, "ignored"),
        )
        import opencomputer.cli_memory as cli_memory

        monkeypatch.setattr(
            cli_memory, "_load_honcho_bootstrap", lambda: stub_bootstrap
        )

        # Should not raise (it prints a hint and returns)
        wizard._optional_honcho()

    def test_optional_honcho_happy_path_calls_honcho_up(self, monkeypatch):
        """If Docker is available and user agrees, wizard calls honcho_up()."""
        import opencomputer.setup_wizard as wizard

        class _FakeConfirm:
            @staticmethod
            def ask(question, default=True):
                return True

        monkeypatch.setattr(wizard, "Confirm", _FakeConfirm)

        up_called = []
        from types import SimpleNamespace

        def _up():
            up_called.append(True)
            return (True, "Honcho stack started.")

        stub_bootstrap = SimpleNamespace(
            detect_docker=lambda: (True, True),
            honcho_up=_up,
        )
        import opencomputer.cli_memory as cli_memory

        monkeypatch.setattr(
            cli_memory, "_load_honcho_bootstrap", lambda: stub_bootstrap
        )

        wizard._optional_honcho()
        assert len(up_called) == 1


class TestCLAUDEmdUpdated:
    """CLAUDE.md should no longer list 'Honcho memory' as Won't Do."""

    def test_honcho_moved_out_of_wont_do(self):
        from pathlib import Path as _P

        claude_md = _P(__file__).resolve().parent.parent / "CLAUDE.md"
        content = claude_md.read_text(encoding="utf-8")
        # The Won't Do bullet list should not mention Honcho any more
        # (it was removed from that line in 10f.N).
        wont_do_section = content.split("### WON'T DO", 1)[-1].split("\n\n", 2)
        # First paragraph after the header — the bullet list.
        bullet_para = wont_do_section[1] if len(wont_do_section) > 1 else ""
        assert "Honcho memory" not in bullet_para, (
            "Honcho memory was moved to the Built list in 10f.N; "
            "it should no longer appear in WON'T DO."
        )
        # The follow-up paragraph should document the move.
        assert "Honcho" in content
        assert "Phase 10f.K–N" in content or "memory-honcho" in content


# ─── Phase 14.J — Honcho host key per active profile ───────────────────


def _load_plugin_entry_module():
    """Load extensions/memory-honcho/plugin.py with its .provider sibling
    resolvable — same trick as _load_plugin_module in TestHonchoSkeleton.
    """
    import sys

    parent = str(_EXT_DIR)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    pkg_name = "_honcho_14j_pkg"
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
    plug_spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.plugin", _EXT_DIR / "plugin.py"
    )
    plug_mod = importlib.util.module_from_spec(plug_spec)
    sys.modules[f"{pkg_name}.plugin"] = plug_mod
    plug_spec.loader.exec_module(plug_mod)
    return plug_mod


class TestHonchoHostKey:
    """14.J — host_key derived from active profile."""

    def test_default_profile_uses_bare_host_key(self, tmp_path, monkeypatch):
        """With no sticky profile, host_key is 'opencomputer'."""
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("HONCHO_HOST_KEY", raising=False)
        mod = _load_plugin_entry_module()
        cfg = mod._config_from_env()
        assert cfg.host_key == "opencomputer"

    def test_named_profile_uses_suffixed_host_key(self, tmp_path, monkeypatch):
        """Sticky 'coder' profile → host_key 'opencomputer.coder'."""
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("HONCHO_HOST_KEY", raising=False)
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "active_profile").write_text("coder\n")
        mod = _load_plugin_entry_module()
        cfg = mod._config_from_env()
        assert cfg.host_key == "opencomputer.coder"

    def test_explicit_env_var_wins(self, tmp_path, monkeypatch):
        """HONCHO_HOST_KEY overrides any profile derivation."""
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HONCHO_HOST_KEY", "custom-key")
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "active_profile").write_text("coder\n")
        mod = _load_plugin_entry_module()
        cfg = mod._config_from_env()
        assert cfg.host_key == "custom-key"

    def test_corrupt_active_profile_falls_back_to_default(self, tmp_path, monkeypatch):
        """Invalid name in active_profile file → fall back to 'opencomputer'."""
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("HONCHO_HOST_KEY", raising=False)
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "active_profile").write_text("BAD NAME WITH SPACES\n")
        mod = _load_plugin_entry_module()
        cfg = mod._config_from_env()
        assert cfg.host_key == "opencomputer"
