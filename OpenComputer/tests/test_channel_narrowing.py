"""Channel-narrowing for the activation planner (best-of-three R3 follow-up).

``channel_narrowed_ids`` drops pure channel-adapter plugins — those
declaring ``activation.on_channels`` — because they bridge a messaging
platform into the gateway daemon and are dead weight on ``oc chat``,
which serves no channels. Every other plugin is kept, including
channel-*kind* plugins that also register tools (e.g. homeassistant):
those do not declare ``on_channels`` and so survive.
"""
from __future__ import annotations

from opencomputer.plugins.activation_planner import channel_narrowed_ids


class _Activation:
    def __init__(self, on_channels=()) -> None:  # noqa: ANN001
        self.on_channels = tuple(on_channels)
        self.on_providers = ()
        self.on_commands = ()
        self.on_tools = ()
        self.on_models = ()


class _Manifest:
    def __init__(self, pid, activation=None) -> None:  # noqa: ANN001
        self.id = pid
        self.activation = activation
        self.tool_names = ()


class _Cand:
    def __init__(self, manifest) -> None:  # noqa: ANN001
        self.manifest = manifest


def _cand(pid, on_channels=None):  # noqa: ANN001, ANN202
    act = _Activation(on_channels) if on_channels is not None else None
    return _Cand(_Manifest(pid, act))


def test_drops_annotated_channel_adapter() -> None:
    cands = [_cand("telegram", ["telegram"]), _cand("coding-harness")]
    assert channel_narrowed_ids(cands) == ["coding-harness"]


def test_keeps_plugin_with_no_activation_block() -> None:
    cands = [_cand("coding-harness"), _cand("dev-tools")]
    assert set(channel_narrowed_ids(cands)) == {"coding-harness", "dev-tools"}


def test_keeps_activation_without_on_channels() -> None:
    # A provider plugin: activation present (on_providers/on_models) but
    # on_channels empty → it is NOT a channel adapter → kept.
    cands = [_cand("anthropic-provider", on_channels=[])]
    assert channel_narrowed_ids(cands) == ["anthropic-provider"]


def test_coding_harness_survives_a_catalog_full_of_channels() -> None:
    cands = [_cand(f"chan{i}", [f"chan{i}"]) for i in range(20)]
    cands.append(_cand("coding-harness"))
    assert "coding-harness" in channel_narrowed_ids(cands)


def test_result_is_sorted_for_determinism() -> None:
    cands = [_cand("zeta"), _cand("alpha"), _cand("mid")]
    assert channel_narrowed_ids(cands) == ["alpha", "mid", "zeta"]


def test_empty_candidates_yields_empty() -> None:
    assert channel_narrowed_ids([]) == []


def test_all_channels_dropped_leaves_only_non_channels() -> None:
    cands = [
        _cand("telegram", ["telegram"]),
        _cand("discord", ["discord"]),
        _cand("anthropic-provider", on_channels=[]),
        _cand("coding-harness"),
    ]
    assert channel_narrowed_ids(cands) == ["anthropic-provider", "coding-harness"]


# ── real-tree wiring (best-of-three R3 follow-up) ─────────────────────


def test_real_tree_narrowing_drops_channels_keeps_tools() -> None:
    from opencomputer.cli import _activation_narrowed_enabled_ids
    from opencomputer.plugins.discovery import standard_search_paths

    narrowed = _activation_narrowed_enabled_ids(standard_search_paths())
    assert narrowed is not None
    # tool + provider plugins survive narrowing
    assert "coding-harness" in narrowed
    assert "anthropic-provider" in narrowed
    # annotated messaging adapters are dropped
    assert "telegram" not in narrowed
    assert "slack" not in narrowed
    # channel-KIND plugins that register tools are NOT dropped
    assert "homeassistant" in narrowed
    assert "discord" in narrowed


def test_real_tree_narrowing_is_strict_subset() -> None:
    from opencomputer.cli import _activation_narrowed_enabled_ids
    from opencomputer.plugins.discovery import discover, standard_search_paths

    sp = standard_search_paths()
    narrowed = _activation_narrowed_enabled_ids(sp)
    all_ids = {c.manifest.id for c in discover(sp)}
    assert isinstance(narrowed, frozenset)
    assert narrowed < all_ids  # strict — channel adapters were dropped


def _capture_load_all(  # noqa: ANN202
    monkeypatch,  # noqa: ANN001
    plugin_filter: str | frozenset[str] | None = "*",
):
    """Stub PluginRegistry.load_all to capture the enabled_ids it gets,
    so the gating wiring is tested without a real plugin load.

    ``plugin_filter`` defaults to ``"*"`` — what ``_resolve_plugin_filter``
    returns for a fresh profile with no curated plugin list, i.e. the
    realistic common case.
    """
    import opencomputer.cli as cli_mod
    from opencomputer.plugins.registry import PluginRegistry

    captured: dict = {}

    def _fake(self, search_paths, enabled_ids=None):  # noqa: ANN001, ANN202
        captured["enabled_ids"] = enabled_ids
        return []

    monkeypatch.setattr(PluginRegistry, "load_all", _fake)
    monkeypatch.setattr(cli_mod, "_resolve_plugin_filter", lambda: plugin_filter)
    monkeypatch.delenv("OPENCOMPUTER_LOAD_ALL_PLUGINS", raising=False)
    return cli_mod, captured


def test_discover_plugins_narrows_when_filter_is_wildcard(monkeypatch) -> None:
    """The common case: no curated profile.yaml → filter resolves to
    ``"*"`` → narrowing must still fire."""
    cli_mod, captured = _capture_load_all(monkeypatch, plugin_filter="*")
    monkeypatch.setenv("OPENCOMPUTER_PLUGIN_ACTIVATION", "plan")
    cli_mod._discover_plugins(narrow_channels=True)
    eids = captured["enabled_ids"]
    assert eids is not None and eids != "*"
    assert "telegram" not in eids        # channel adapter dropped
    assert "coding-harness" in eids      # tool plugin kept


def test_discover_plugins_narrows_when_filter_is_none(monkeypatch) -> None:
    """Malformed/missing config → filter resolves to ``None`` → narrows."""
    cli_mod, captured = _capture_load_all(monkeypatch, plugin_filter=None)
    monkeypatch.setenv("OPENCOMPUTER_PLUGIN_ACTIVATION", "plan")
    cli_mod._discover_plugins(narrow_channels=True)
    eids = captured["enabled_ids"]
    assert eids is not None
    assert "telegram" not in eids
    assert "coding-harness" in eids


def test_discover_plugins_no_narrow_when_flag_off(monkeypatch) -> None:
    cli_mod, captured = _capture_load_all(monkeypatch)
    monkeypatch.delenv("OPENCOMPUTER_PLUGIN_ACTIVATION", raising=False)
    cli_mod._discover_plugins(narrow_channels=True)
    assert captured["enabled_ids"] == "*"  # flag off → filter untouched


def test_discover_plugins_no_narrow_when_param_false(monkeypatch) -> None:
    """The gateway calls _discover_plugins() with the default
    narrow_channels=False — it must keep loading every channel adapter
    even when the flag is on."""
    cli_mod, captured = _capture_load_all(monkeypatch)
    monkeypatch.setenv("OPENCOMPUTER_PLUGIN_ACTIVATION", "plan")
    cli_mod._discover_plugins(narrow_channels=False)
    assert captured["enabled_ids"] == "*"


def test_discover_plugins_respects_curated_user_filter(monkeypatch) -> None:
    """A real profile.yaml filter (a frozenset) is never second-guessed
    by narrowing, even with the flag on."""
    user_filter = frozenset({"coding-harness", "telegram"})
    cli_mod, captured = _capture_load_all(
        monkeypatch, plugin_filter=user_filter
    )
    monkeypatch.setenv("OPENCOMPUTER_PLUGIN_ACTIVATION", "plan")
    cli_mod._discover_plugins(narrow_channels=True)
    assert captured["enabled_ids"] == user_filter  # untouched
