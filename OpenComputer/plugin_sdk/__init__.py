"""
OpenComputer Plugin SDK — the ONLY public contract for plugins.

Third-party plugins must import from `plugin_sdk/*` exclusively. Never import
from `opencomputer/**` directly — those modules are internal and may change
without warning. The SDK is versioned and evolves with backwards-compatible
guarantees across minor releases.

Exports:
    # Core types
    MessageEvent      — platform-agnostic inbound message
    SendResult        — outbound delivery result
    Platform          — enum of supported channel platforms
    PluginManifest    — plugin metadata structure

    # Contracts
    BaseChannelAdapter — inherit for a new channel plugin
    BaseTool          — inherit for a new tool plugin
    BaseProvider      — inherit for a new LLM provider plugin
    HookSpec          — declare a hook in your plugin

    # Helpers
    register_plugin   — decorator/factory to register with the host
"""

__version__ = "0.1.0"

# Phase 1 will expand this. Phase 0 just reserves the package.
__all__: list[str] = []
