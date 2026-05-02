"""``/capabilities`` — show what the current provider supports + alternatives.

Discoverability surface for the capability-by-method-override pattern
established by Subsystem E (PR #350) and extended for vision in
:class:`plugin_sdk.VisionUnsupportedError`. Each provider declares a
capability by overriding the corresponding BaseProvider method
(``complete_vision``, ``submit_batch``, etc.). This command introspects
which providers override which methods.

Output format::

    Current provider: openai (gpt-5.4)
    Supports:  vision

    Other providers:
      - anthropic: vision, batch
      - openrouter: vision (inherited from openai)

    Missing on current provider: batch
      Switch via: /provider <name>  or  oc model
      Providers with batch: anthropic
"""
from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

#: Mapping of capability token → BaseProvider method name. A provider
#: "supports" the capability iff it overrides the method (i.e. the
#: method is present in the class's ``__dict__``, not inherited from
#: BaseProvider's default-raises implementation).
_CAPABILITY_METHODS: dict[str, str] = {
    "vision": "complete_vision",
    "batch": "submit_batch",
}


def _provider_supports(plugin: object, capability: str) -> bool:
    """True iff this plugin's class overrides the method for ``capability``.

    Walks the MRO so subclasses (e.g. OpenRouterProvider extends
    OpenAIProvider) inherit overrides from their parents — but only
    while the parent is NOT BaseProvider itself.
    """
    method_name = _CAPABILITY_METHODS.get(capability)
    if method_name is None:
        return False
    cls = plugin if isinstance(plugin, type) else type(plugin)
    from plugin_sdk.provider_contract import BaseProvider

    for base in cls.__mro__:
        if base is BaseProvider:
            return False
        if method_name in base.__dict__:
            return True
    return False


def _capabilities_for(plugin: object) -> list[str]:
    """Return the sorted list of capability tokens this plugin supports."""
    out = []
    for cap in _CAPABILITY_METHODS:
        if _provider_supports(plugin, cap):
            out.append(cap)
    return sorted(out)


def _providers_with(capability: str) -> list[str]:
    """Names of registered providers that support ``capability``."""
    from opencomputer.plugins.registry import registry as plugin_registry

    out: list[str] = []
    for plugin in plugin_registry.providers.values():
        try:
            name = plugin.name
        except Exception:  # noqa: BLE001
            continue
        if _provider_supports(plugin, capability):
            out.append(name)
    return sorted(out)


class CapabilitiesCommand(SlashCommand):
    name = "capabilities"
    description = (
        "List what the current provider supports + which other providers "
        "have which features"
    )

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        del args, runtime  # no inputs

        from opencomputer.agent.config import default_config
        from opencomputer.plugins.registry import registry as plugin_registry

        try:
            cfg = default_config()
            current_name = cfg.model.provider
            current_model = cfg.model.name
        except Exception as e:  # noqa: BLE001
            return SlashCommandResult(
                output=f"could not read config: {type(e).__name__}: {e}",
                handled=True,
            )

        cur_plugin = None
        try:
            cur_plugin = plugin_registry.providers.get(current_name)
        except Exception:  # noqa: BLE001
            pass

        current_caps: list[str] = []
        if cur_plugin is not None:
            current_caps = _capabilities_for(cur_plugin)

        # All other providers with capabilities
        all_decls: dict[str, list[str]] = {}
        empty_provider_names: list[str] = []
        try:
            for name, plugin in plugin_registry.providers.items():
                if name == current_name:
                    continue
                try:
                    caps = _capabilities_for(plugin)
                except Exception:  # noqa: BLE001
                    continue
                if caps:
                    all_decls[name] = caps
                else:
                    empty_provider_names.append(name)
        except Exception:  # noqa: BLE001
            pass

        # Build output
        lines: list[str] = []
        if cur_plugin is None:
            lines.append(
                f"Current provider: {current_name} ({current_model}) — NOT REGISTERED"
            )
        else:
            lines.append(f"Current provider: {current_name} ({current_model})")
        if current_caps:
            lines.append(f"Supports: {', '.join(current_caps)}")
        else:
            lines.append("Supports: (none)")

        if all_decls:
            lines.append("")
            lines.append("Other providers:")
            for name in sorted(all_decls, key=lambda n: (-len(all_decls[n]), n)):
                lines.append(f"  - {name}: {', '.join(all_decls[name])}")
        if empty_provider_names:
            preview = ", ".join(sorted(empty_provider_names)[:5])
            extra = (
                f" + {len(empty_provider_names) - 5} more"
                if len(empty_provider_names) > 5
                else ""
            )
            lines.append(f"  - {preview}{extra} (no extra capabilities)")

        # Missing-on-current section
        all_caps_known: set[str] = set()
        for caps in all_decls.values():
            all_caps_known.update(caps)
        missing = sorted(all_caps_known - set(current_caps))
        if missing:
            lines.append("")
            lines.append(f"Missing on current provider: {', '.join(missing)}")
            lines.append("  Switch via: /provider <name>  or  oc model")
            for cap in missing:
                alts = _providers_with(cap)
                if alts:
                    lines.append(f"  Providers with {cap}: {', '.join(alts)}")

        return SlashCommandResult(output="\n".join(lines), handled=True)


__all__ = ["CapabilitiesCommand"]
