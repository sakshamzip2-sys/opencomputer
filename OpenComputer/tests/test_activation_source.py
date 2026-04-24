"""Task I.7 — Activation-source tracking on PluginAPI.

Verifies:

* ``PluginActivationSource`` Literal is exported from ``plugin_sdk.core``
  and re-exported from ``plugin_sdk`` with the seven documented values
  (``bundled``, ``global_install``, ``profile_local``,
  ``workspace_overlay``, ``user_enable``, ``auto_enable_default``,
  ``auto_enable_demand``).
* ``PluginAPI.activation_source`` defaults to ``"bundled"`` — the
  loader's current baseline for bundled ``extensions/*`` plugins.
* A custom ``activation_source`` passed at construction time is read
  back verbatim through the property.
* Plugin ``register(api)`` code can read ``api.activation_source`` and
  branch on it, matching OpenClaw's ``createPluginActivationSource``
  pattern at ``sources/openclaw/src/plugins/config-state.ts``.
* The loader (``opencomputer.plugins.registry.PluginRegistry.api``)
  continues to produce a ``PluginAPI`` with ``activation_source ==
  "bundled"`` for bundled extensions — backwards compatible for
  existing plugins that never read the new property.
"""

from __future__ import annotations

from typing import get_args
from unittest.mock import MagicMock

import pytest


def test_activation_source_literal_exports_seven_values() -> None:
    """The Literal type must expose exactly the seven documented sources."""
    from plugin_sdk.core import PluginActivationSource

    args = set(get_args(PluginActivationSource))
    assert args == {
        "bundled",
        "global_install",
        "profile_local",
        "workspace_overlay",
        "user_enable",
        "auto_enable_default",
        "auto_enable_demand",
    }


def test_activation_source_is_reexported_from_plugin_sdk() -> None:
    """Public re-export so third-party plugins can ``from plugin_sdk import ...``."""
    import plugin_sdk

    assert hasattr(plugin_sdk, "PluginActivationSource")
    assert "PluginActivationSource" in plugin_sdk.__all__


def _make_api(**overrides):
    from opencomputer.plugins.loader import PluginAPI

    kwargs = {
        "tool_registry": MagicMock(),
        "hook_engine": MagicMock(),
        "provider_registry": {},
        "channel_registry": {},
        "injection_engine": MagicMock(),
    }
    kwargs.update(overrides)
    return PluginAPI(**kwargs)


def test_plugin_api_default_source_is_bundled() -> None:
    """The loader baseline: bundled ``extensions/*`` plugins → "bundled"."""
    api = _make_api()
    assert api.activation_source == "bundled"


@pytest.mark.parametrize(
    "source",
    [
        "bundled",
        "global_install",
        "profile_local",
        "workspace_overlay",
        "user_enable",
        "auto_enable_default",
        "auto_enable_demand",
    ],
)
def test_plugin_api_accepts_each_documented_source(source: str) -> None:
    """Every Literal value must round-trip through the constructor."""
    api = _make_api(activation_source=source)
    assert api.activation_source == source


def test_plugin_register_can_branch_on_activation_source() -> None:
    """A plugin's ``register(api)`` reads ``api.activation_source`` and adapts.

    This is the whole point of I.7 — plugins can log differently,
    skip heavy init, or warn the user based on WHY they were enabled.
    """
    observed: dict[str, str] = {}

    def fake_register(api) -> None:
        # Plugin code branches on the source, exactly like OpenClaw's
        # createPluginActivationSource-driven logging in
        # sources/openclaw/src/plugins/config-state.ts.
        observed["source"] = api.activation_source
        if api.activation_source == "user_enable":
            observed["log"] = "user requested activation"
        elif api.activation_source == "bundled":
            observed["log"] = "loaded from extensions/"
        else:
            observed["log"] = f"loaded via {api.activation_source}"

    api_user = _make_api(activation_source="user_enable")
    fake_register(api_user)
    assert observed == {"source": "user_enable", "log": "user requested activation"}

    api_bundled = _make_api(activation_source="bundled")
    fake_register(api_bundled)
    assert observed == {"source": "bundled", "log": "loaded from extensions/"}

    api_workspace = _make_api(activation_source="workspace_overlay")
    fake_register(api_workspace)
    assert observed == {
        "source": "workspace_overlay",
        "log": "loaded via workspace_overlay",
    }


def test_plugin_api_invalid_source_rejected() -> None:
    """Only documented Literal values are accepted — guard against typos."""
    with pytest.raises(ValueError, match="activation_source"):
        _make_api(activation_source="totally_bogus_source")


def test_registry_api_factory_defaults_to_bundled() -> None:
    """``PluginRegistry.api()`` produces bundled-sourced APIs by default.

    This is the MVP wiring: every call into ``load_all`` today is for
    bundled ``extensions/*`` plugins, so the factory hands out
    ``"bundled"``. Future install-CLI work will pass an explicit source.
    """
    from opencomputer.plugins.registry import PluginRegistry

    reg = PluginRegistry()
    api = reg.api()
    assert api.activation_source == "bundled"


def test_load_plugin_threads_activation_source_into_register(tmp_path) -> None:
    """``load_plugin(cand, api, activation_source=...)`` exposes the source to register().

    This is the wiring point the CLI will use when upgrading calls —
    e.g. ``opencomputer plugin enable <id>`` will pass
    ``"user_enable"``. Uses a synthetic on-disk plugin so the loader's
    real import path is exercised.
    """
    import json as _json

    from opencomputer.plugins.discovery import PluginCandidate
    from opencomputer.plugins.loader import load_plugin
    from plugin_sdk.core import PluginManifest

    root = tmp_path / "fake-plugin"
    root.mkdir()
    (root / "plugin.json").write_text(
        _json.dumps(
            {
                "id": "fake-activation-plugin",
                "name": "Fake",
                "version": "0.0.1",
                "entry": "entry_mod",
            }
        )
    )
    (root / "entry_mod.py").write_text(
        "OBSERVED = {}\n"
        "def register(api):\n"
        "    OBSERVED['source'] = api.activation_source\n"
    )
    manifest = PluginManifest(
        id="fake-activation-plugin",
        name="Fake",
        version="0.0.1",
        entry="entry_mod",
    )
    cand = PluginCandidate(
        manifest=manifest,
        root_dir=root,
        manifest_path=root / "plugin.json",
    )

    api = _make_api()  # baseline: "bundled"
    loaded = load_plugin(cand, api, activation_source="user_enable")
    assert loaded is not None
    assert loaded.module.OBSERVED == {"source": "user_enable"}
    # The override is scoped to the register() call; the shared api
    # reverts so the next plugin on the same api sees its own source.
    assert api.activation_source == "bundled"


def test_load_plugin_rejects_invalid_activation_source(tmp_path) -> None:
    """Typos in ``activation_source`` raise ValueError rather than silently passing."""
    from opencomputer.plugins.discovery import PluginCandidate
    from opencomputer.plugins.loader import load_plugin
    from plugin_sdk.core import PluginManifest

    # Minimal on-disk plugin — we never get far enough to run register().
    root = tmp_path / "fake-plugin-bad-source"
    root.mkdir()
    (root / "plugin.json").write_text("{}")
    (root / "entry_mod.py").write_text("def register(api): pass\n")
    manifest = PluginManifest(
        id="fake-plugin-bad-source",
        name="Fake",
        version="0.0.1",
        entry="entry_mod",
    )
    cand = PluginCandidate(
        manifest=manifest,
        root_dir=root,
        manifest_path=root / "plugin.json",
    )

    api = _make_api()
    with pytest.raises(ValueError, match="activation_source"):
        load_plugin(cand, api, activation_source="definitely_not_valid")
