"""
OpenComputer Plugin SDK — the ONLY public contract for plugins.

Third-party plugins must import from `plugin_sdk/*` exclusively. Never
import from `opencomputer/**` directly — those modules are internal
and may change without warning. The SDK is versioned and evolves with
backwards-compatible guarantees across minor releases.
"""

__version__ = "0.1.0"

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.classifier import (
    AggregationPolicy,
    Classifier,
    ClassifierVerdict,
    RegexClassifier,
    Rule,
)
from plugin_sdk.consent import (
    CapabilityClaim,
    ConsentDecision,
    ConsentGrant,
    ConsentTier,
)
from plugin_sdk.core import (
    Message,
    MessageEvent,
    ModelSupport,
    Platform,
    PluginActivationSource,
    PluginManifest,
    PluginSetup,
    ProcessingOutcome,
    Role,
    SendResult,
    SetupChannel,
    SetupProvider,
    SingleInstanceError,
    StopReason,
    ToolCall,
    ToolResult,
)
from plugin_sdk.decay import (
    DecayConfig,
    DriftConfig,
    DriftReport,
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
from plugin_sdk.inference import (
    Motif,
    MotifExtractor,
    MotifKind,
)
from plugin_sdk.ingestion import (
    FileObservationEvent,
    FileOperation,
    HookDecisionKind,
    HookSignalEvent,
    IdentityNormalizer,
    MessageRole,
    MessageSignalEvent,
    SignalEvent,
    SignalNormalizer,
    ToolCallEvent,
    ToolCallOutcome,
    WebContentKind,
    WebObservationEvent,
    clear_normalizers,
    get_normalizer,
    register_normalizer,
)
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext
from plugin_sdk.interaction import (
    ASK_USER_QUESTION_HANDLER,
    AskUserQuestionHandler,
    InteractionRequest,
    InteractionResponse,
)
from plugin_sdk.memory import MemoryProvider
from plugin_sdk.pdf_helpers import (
    PDF_HARD_PAGE_LIMIT,
    PDF_MAX_BYTES,
    PDF_SOFT_PAGE_LIMIT,
    count_pdf_pages,
    pdf_to_base64,
)
from plugin_sdk.permission_mode import PermissionMode, effective_permission_mode
from plugin_sdk.profile_context import current_profile_home, set_profile
from plugin_sdk.profile_subprocess import scope_subprocess_env
from plugin_sdk.provider_contract import (
    BaseProvider,
    BatchRequest,
    BatchResult,
    BatchUnsupportedError,
    CacheTokens,
    JsonSchemaSpec,
    ProviderCapabilities,
    ProviderResponse,
    StreamEvent,
    Usage,
    VisionUnsupportedError,
)
from plugin_sdk.realtime_voice import (
    BaseRealtimeVoiceBridge,
    RealtimeVoiceCloseReason,
    RealtimeVoiceRole,
    RealtimeVoiceTool,
    RealtimeVoiceToolCallEvent,
)
from plugin_sdk.runtime_context import (
    DEFAULT_RUNTIME_CONTEXT,
    RequestContext,
    RuntimeContext,
)
from plugin_sdk.sandbox import (
    SandboxConfig,
    SandboxResult,
    SandboxStrategy,
    SandboxStrategyName,
    SandboxUnavailable,
)
from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource, TrustLevel
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema
from plugin_sdk.transports import NormalizedRequest, NormalizedResponse, TransportBase
from plugin_sdk.user_model import (
    Edge,
    EdgeKind,
    Node,
    NodeKind,
    UserModelQuery,
    UserModelSnapshot,
)

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
    "ModelSupport",
    "PluginManifest",
    "PluginActivationSource",
    "PluginSetup",
    "ProcessingOutcome",
    "SetupChannel",
    "SetupProvider",
    "StopReason",
    "SingleInstanceError",
    # contracts
    "BaseTool",
    "ToolSchema",
    "BaseProvider",
    "BatchRequest",
    "BatchResult",
    "BatchUnsupportedError",
    "CacheTokens",
    "JsonSchemaSpec",
    "ProviderCapabilities",
    "ProviderResponse",
    "StreamEvent",
    "Usage",
    "VisionUnsupportedError",
    "BaseChannelAdapter",
    "ChannelCapabilities",
    # classifier abstraction (2026-04-28)
    "AggregationPolicy",
    "Classifier",
    "ClassifierVerdict",
    "RegexClassifier",
    "Rule",
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
    "RequestContext",
    "DynamicInjectionProvider",
    "InjectionContext",
    # doctor
    "HealthContribution",
    "HealthRunFn",
    "HealthStatus",
    "RepairResult",
    # interaction (Phase 11b + AUQ-handler context)
    "ASK_USER_QUESTION_HANDLER",
    "AskUserQuestionHandler",
    "InteractionRequest",
    "InteractionResponse",
    # memory (Phase 10f)
    "MemoryProvider",
    # permission modes (2026-04-29)
    "PermissionMode",
    "effective_permission_mode",
    # profile context (2026-04-30) — per-task profile ContextVar
    "current_profile_home",
    "set_profile",
    # profile-scoped subprocess env (Follow-up A to PR #284) — public
    # plugin SDK helper for spawning subprocesses scoped to the active
    # profile (HOME / XDG_*). Stateless: takes profile_home directly.
    "scope_subprocess_env",
    # realtime voice (2026-04-29) — port of openclaw/src/realtime-voice/
    "BaseRealtimeVoiceBridge",
    "RealtimeVoiceCloseReason",
    "RealtimeVoiceRole",
    "RealtimeVoiceTool",
    "RealtimeVoiceToolCallEvent",
    # slash commands (Phase 12b.6, Task D8)
    "SlashCommand",
    "SlashCommandResult",
    # consent (F1)
    "ConsentTier",
    "CapabilityClaim",
    "ConsentGrant",
    "ConsentDecision",
    # ingestion / signal bus (Phase 3.A, F2)
    "SignalEvent",
    "ToolCallEvent",
    "WebObservationEvent",
    "FileObservationEvent",
    "MessageSignalEvent",
    "HookSignalEvent",
    "SignalNormalizer",
    "IdentityNormalizer",
    "register_normalizer",
    "get_normalizer",
    "clear_normalizers",
    "ToolCallOutcome",
    "WebContentKind",
    "FileOperation",
    "MessageRole",
    "HookDecisionKind",
    # behavioral inference (Phase 3.B, F2 continued)
    "Motif",
    "MotifExtractor",
    "MotifKind",
    # user-model graph (Phase 3.C, F4 layer)
    "Node",
    "NodeKind",
    "Edge",
    "EdgeKind",
    "UserModelQuery",
    "UserModelSnapshot",
    # sandbox (Phase 3.E)
    "SandboxConfig",
    "SandboxResult",
    "SandboxStrategy",
    "SandboxStrategyName",
    "SandboxUnavailable",
    # decay + drift (Phase 3.D, F5 layer)
    "DecayConfig",
    "DriftConfig",
    "DriftReport",
    # transport ABC (PR-C)
    "NormalizedRequest",
    "NormalizedResponse",
    "TransportBase",
    # skills hub (Tier 1.A)
    "SkillSource",
    "SkillMeta",
    "SkillBundle",
    "TrustLevel",
    # PDF helpers (SP2, 2026-05-02) — shared by Anthropic / Bedrock provider
    # plugins for size + page-count guard rails on PDF attachments.
    "PDF_MAX_BYTES",
    "PDF_HARD_PAGE_LIMIT",
    "PDF_SOFT_PAGE_LIMIT",
    "count_pdf_pages",
    "pdf_to_base64",
]
