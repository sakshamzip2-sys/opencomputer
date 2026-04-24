"""
OpenComputer Plugin SDK — the ONLY public contract for plugins.

Third-party plugins must import from `plugin_sdk/*` exclusively. Never
import from `opencomputer/**` directly — those modules are internal
and may change without warning. The SDK is versioned and evolves with
backwards-compatible guarantees across minor releases.
"""

__version__ = "0.1.0"

from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import (
    Message,
    MessageEvent,
    Platform,
    PluginManifest,
    Role,
    SendResult,
    SingleInstanceError,
    StopReason,
    ToolCall,
    ToolResult,
)
from plugin_sdk.doctor import (
    HealthContribution,
    HealthRunFn,
    HealthStatus,
    RepairResult,
)
from plugin_sdk.hooks import (
    ALL_HOOK_EVENTS,
    HookContext,
    HookDecision,
    HookEvent,
    HookHandler,
    HookSpec,
)
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext
from plugin_sdk.interaction import InteractionRequest, InteractionResponse
from plugin_sdk.memory import MemoryProvider
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

__all__ = [
    "__version__",
    # core types
    "Role",
    "Message",
    "ToolCall",
    "ToolResult",
    "Platform",
    "MessageEvent",
    "SendResult",
    "PluginManifest",
    "StopReason",
    "SingleInstanceError",
    # contracts
    "BaseTool",
    "ToolSchema",
    "BaseProvider",
    "ProviderResponse",
    "StreamEvent",
    "Usage",
    "BaseChannelAdapter",
    # hooks
    "HookEvent",
    "HookContext",
    "HookDecision",
    "HookHandler",
    "HookSpec",
    "ALL_HOOK_EVENTS",
    # runtime + injection
    "RuntimeContext",
    "DEFAULT_RUNTIME_CONTEXT",
    "DynamicInjectionProvider",
    "InjectionContext",
    # doctor
    "HealthContribution",
    "HealthRunFn",
    "HealthStatus",
    "RepairResult",
    # interaction (Phase 11b)
    "InteractionRequest",
    "InteractionResponse",
    # memory (Phase 10f)
    "MemoryProvider",
    # slash commands (Phase 12b.6, Task D8)
    "SlashCommand",
    "SlashCommandResult",
]
