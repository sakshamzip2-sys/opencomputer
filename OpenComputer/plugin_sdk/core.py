"""
Core public types — the canonical vocabulary plugins use.

These types are the STABLE contract. Changes here are breaking changes
and require a major version bump of plugin_sdk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, get_args

# ─── Message / conversation primitives ─────────────────────────────────

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class Message:
    """A single conversation message — canonical form used everywhere internally.

    Reasoning fields (``reasoning``, ``reasoning_details``,
    ``codex_reasoning_items``) carry provider-specific reasoning-chain
    output (Anthropic extended thinking, OpenAI o1 / o3 reasoning
    replay, Nous / OpenRouter unified reasoning). Default ``None``
    keeps round-trips backwards-compatible for non-reasoning models.

    * ``reasoning``             — free-form reasoning TEXT from the
                                  provider (single string).
    * ``reasoning_details``     — OpenRouter / Nous unified-format
                                  structured array (list of dicts).
    * ``codex_reasoning_items`` — OpenAI o1/o3 reasoning items that
                                  must be replayed verbatim on the
                                  next turn to preserve continuity.

    SessionDB serialises the two list fields as JSON and restores them
    via ``json.loads`` on read — see ``opencomputer.agent.state``.
    """

    role: Role
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None
    name: str | None = None  # for tool messages, the tool name
    reasoning: str | None = None  # extended thinking, if supported
    reasoning_details: Any = None  # list[dict[str, Any]] | None
    codex_reasoning_items: Any = None  # list[dict[str, Any]] | None
    attachments: list[str] = field(default_factory=list)
    """Absolute filesystem paths to image attachments associated with this
    message. Forward-compatible field added 2026-04-27 for TUI image-paste
    support. Empty list (the default) means "text-only message" — every
    pre-existing call site keeps working without modification.

    Providers that support multimodal input (Anthropic, OpenAI vision)
    convert these paths into provider-specific image content blocks at
    request time. Providers that don't simply ignore them.

    SessionDB serialises this as JSON alongside the other list fields."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A request from the model to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The result of executing a tool call."""

    tool_call_id: str
    content: str
    is_error: bool = False


# ─── Platform / channel primitives ─────────────────────────────────────


class Platform(str, Enum):
    """Supported messaging platforms. Plugins can register new ones."""

    CLI = "cli"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    WHATSAPP = "whatsapp"
    SIGNAL = "signal"
    IMESSAGE = "imessage"
    WEB = "web"
    SMS = "sms"
    MATRIX = "matrix"
    MATTERMOST = "mattermost"
    EMAIL = "email"
    WEBHOOK = "webhook"
    HOMEASSISTANT = "homeassistant"


@dataclass(frozen=True, slots=True)
class MessageEvent:
    """Platform-agnostic inbound message — the common format produced by every adapter."""

    platform: Platform
    chat_id: str
    user_id: str
    text: str
    timestamp: float  # unix timestamp
    attachments: list[str] = field(default_factory=list)  # file paths or URLs
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outbound delivery result from a channel adapter."""

    success: bool
    message_id: str | None = None
    error: str | None = None


# ─── Plugin manifest ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SetupProvider:
    """Cheap setup metadata for one provider id a plugin exposes.

    Sub-project G.23 (Tier 4 OpenClaw port). Mirrors OpenClaw's
    ``PluginManifestSetupProvider`` at
    ``sources/openclaw-2026.4.23/src/plugins/manifest.ts:76-83``.
    Sub-project G.24 enriches it with display fields used by the
    interactive setup wizard.

    Lets the setup wizard + ``opencomputer doctor`` know which env vars
    must be set for a provider plugin to function — without loading the
    plugin's Python code. Today both pieces of core hard-code this
    knowledge in a small dict (``cli._check_provider_key``,
    ``setup_wizard._SUPPORTED_PROVIDERS``); G.23/G.24 push the source
    of truth back into the plugin manifest so third-party providers
    can self-describe.
    """

    id: str
    """Provider id surfaced during setup (e.g. ``"anthropic"``, ``"openai"``)."""

    auth_methods: tuple[str, ...] = ()
    """Auth modes this provider supports (e.g. ``("api_key", "bearer")``)."""

    env_vars: tuple[str, ...] = ()
    """Env vars that satisfy setup without runtime loading.

    Order matters: the first env var is treated as the canonical one
    by setup tools — ``opencomputer doctor`` checks the first entry to
    decide whether the provider is configured. Use additional entries
    for proxy modes or alternate auth.
    """

    label: str = ""
    """Human-readable display name (e.g. ``"Anthropic (Claude)"``).

    Sub-project G.24. Used by ``opencomputer setup`` when listing
    provider choices. Empty string falls back to ``id``.
    """

    default_model: str = ""
    """Default model id surfaced by the setup wizard (e.g. ``"claude-opus-4-7"``).

    Sub-project G.24. Empty string means "no default" — the wizard
    prompts the user to enter a model with no pre-fill.
    """

    signup_url: str = ""
    """URL where the user can obtain an API key.

    Sub-project G.24. The setup wizard surfaces this so the user knows
    where to go. Empty string suppresses the hint.
    """


@dataclass(frozen=True, slots=True)
class SetupChannel:
    """Cheap setup metadata for one channel id a plugin exposes.

    Sub-project G.25 (Tier 4 OpenClaw port follow-up). Symmetric to
    :class:`SetupProvider` but for channel plugins (Telegram, Discord,
    iMessage, etc.). The setup wizard reads this so the user can be
    walked through each channel's required env vars without core
    hard-coding the per-channel knowledge.
    """

    id: str
    """Channel id surfaced during setup (e.g. ``"telegram"``, ``"discord"``)."""

    env_vars: tuple[str, ...] = ()
    """Env vars that must be set for the channel to authenticate.

    Order matters: the first env var is treated as the primary
    credential. Subsequent entries cover supplemental auth (user-id
    allowlists, webhook secrets, etc.).
    """

    label: str = ""
    """Human-readable display name (e.g. ``"Telegram"``).

    Empty string falls back to ``id`` when rendered in the wizard.
    """

    signup_url: str = ""
    """URL where the user can obtain the credential (e.g. BotFather).

    Empty string suppresses the hint.
    """

    requires_user_id: bool = False
    """``True`` if the channel needs a user-id allowlist (Telegram pattern).

    Telegram bots accept any chat unless restricted via a user-id
    allowlist. The setup wizard prompts for the user's Telegram id
    when this is ``True``.
    """


@dataclass(frozen=True, slots=True)
class PluginSetup:
    """Cheap setup metadata exposed before plugin runtime loads.

    Sub-project G.23 (Tier 4 OpenClaw port). Mirrors OpenClaw's
    ``PluginManifestSetup`` at
    ``sources/openclaw-2026.4.23/src/plugins/manifest.ts:85-97``.
    Sub-project G.25 adds ``channels`` for channel-plugin metadata
    symmetric to ``providers``.

    Default-empty fields mean "no declarations" — backwards-compatible
    with existing manifests.
    """

    providers: tuple[SetupProvider, ...] = ()
    """Provider ids this plugin exposes to setup/doctor flows."""

    channels: tuple[SetupChannel, ...] = ()
    """Channel ids this plugin exposes to setup/doctor flows (G.25)."""

    requires_runtime: bool = False
    """``True`` if setup still needs to import the plugin's Python.

    Currently informational — the wizard reads it before deciding
    whether to defer plugin activation until after credentials are
    collected. Default ``False`` matches every existing bundled plugin.
    """


@dataclass(frozen=True, slots=True)
class ModelSupport:
    """Declares which model ids a provider plugin can serve.

    Sub-project G.21 (Tier 4 OpenClaw port). Mirrors OpenClaw's
    ``modelSupport`` manifest field at
    ``sources/openclaw-2026.4.23/src/plugins/providers.ts:316-337``.
    The matcher tries ``model_patterns`` (regex, ``re.search``) first
    then ``model_prefixes`` (``str.startswith``); the first hit wins.

    Fields are tuples so the dataclass stays hashable + matches the
    SDK's "immutable values everywhere" rule. Default-empty tuples mean
    the plugin declares no model affinity (legacy behavior — caller
    falls back to explicit ``provider`` selection).
    """

    model_prefixes: tuple[str, ...] = ()
    model_patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Metadata for a plugin — parsed from plugin.json at discovery time."""

    id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    homepage: str = ""
    license: str = "MIT"
    kind: Literal["channel", "provider", "tool", "skill", "mixed"] = "mixed"
    entry: str = ""  # path to the entry module, relative to plugin root
    # Phase 14.C: profile scoping. None or ["*"] means "any profile can
    # opt in" (most permissive, backward-compatible default). A list of
    # profile names restricts the plugin to those profiles only.
    profiles: tuple[str, ...] | None = None
    # Phase 14.C: single-instance plugins (e.g. plugins that own a bot
    # token) can only belong to ONE profile at a time. Core tracks a
    # lock in ~/.opencomputer/.locks/<plugin-id> when this is True.
    single_instance: bool = False
    # Phase 12b1 (Sub-project A): plugins that should be active on a fresh
    # install without the user opting in. Currently only `memory-honcho`
    # uses this (so Honcho becomes the default memory provider when
    # Docker is available). Wizard + config consumers honor this flag
    # rather than the legacy "empty provider = baseline only" fallback.
    enabled_by_default: bool = False
    # Phase 12b5 (Sub-project E, Task E1): schema names of tools this
    # plugin registers via ``api.register_tool``. Used by the core demand
    # tracker (E2) to resolve tool-not-found events to candidate plugins
    # without loading them. Default ``()`` means the plugin registers no
    # tools (provider-only / channel-only / memory-only plugins).
    #
    # Tools that depend on optional pip extras (and may not register at
    # runtime if the extra isn't installed) belong in
    # ``optional_tool_names`` instead — the drift-guard test enforces
    # that ``tool_names`` matches *registered* tools and tolerates
    # missing optional ones.
    tool_names: tuple[str, ...] = ()
    # Tools registered conditionally — typically gated on an optional
    # pip extra (e.g. ``coding-harness``'s introspection tools depend
    # on ``mss`` / ``rapidocr_onnxruntime``). The demand-tracker queries
    # both ``tool_names`` and ``optional_tool_names`` so the user can
    # still be pointed at the right plugin when an optional tool is
    # unavailable. Default ``()`` keeps every existing manifest valid.
    optional_tool_names: tuple[str, ...] = ()
    # Sub-project G.11 (Tier 2.13): MCP servers this plugin needs in order
    # to function. Each entry is either:
    #   - a preset slug (e.g. ``"filesystem"``, ``"github"``) — resolved
    #     against ``opencomputer.mcp.presets.PRESETS`` at activation
    #     time, or
    #   - the literal string ``"<plugin_root>/.mcp.json"`` — load custom
    #     servers declared in the plugin's own ``.mcp.json`` file.
    # When the plugin's ``register()`` runs, the loader installs these
    # MCPs into ``config.yaml`` (idempotent — skips if already present)
    # so the agent picks them up on next start. Default ``()`` means the
    # plugin needs no MCPs.
    mcp_servers: tuple[str, ...] = ()
    # Sub-project G.21 (Tier 4 — OpenClaw port). Provider plugins declare
    # which model ids they can serve (e.g. ``["claude-"]`` for
    # anthropic-provider, ``["gpt-", "o1", "o3", "o4"]`` for
    # openai-provider). The plugin loader uses this to auto-activate the
    # matching provider when the user picks a model whose id matches —
    # solves the friction of "I changed model to gpt-4o, why doesn't
    # this work?" when openai-provider was disabled in the active
    # profile preset. Default ``None`` means the plugin declares no
    # model affinity (the legacy behavior).
    model_support: ModelSupport | None = None
    # Sub-project G.22 (Tier 4 — OpenClaw port). Historical ids this
    # plugin used to be known by. When the user's profile.yaml or
    # workspace overlay still references an old id (because they wrote
    # the config before the rename), the loader transparently maps the
    # old id to ``self.id``. Mirrors OpenClaw's ``legacyPluginIds``
    # field at ``sources/openclaw-2026.4.23/src/plugins/manifest-
    # registry.ts:100`` and the ``normalizePluginId`` lookup at
    # ``sources/openclaw-2026.4.23/src/plugins/config-state.ts:69-74``.
    # Default ``()`` means the plugin has never been renamed.
    legacy_plugin_ids: tuple[str, ...] = ()
    # Sub-project G.23 (Tier 4 — OpenClaw port). Cheap setup metadata
    # exposed before plugin runtime loads. The setup wizard + doctor
    # read this to know which env vars / auth methods a provider plugin
    # needs — instead of hard-coding that knowledge in core. Default
    # ``None`` means "no declarations", and core keeps its legacy
    # hard-coded behavior for that plugin (backwards-compatible).
    setup: PluginSetup | None = None


# ─── Plugin activation source (Task I.7) ───────────────────────────────

PluginActivationSource = Literal[
    "bundled",
    "global_install",
    "profile_local",
    "workspace_overlay",
    "user_enable",
    "auto_enable_default",
    "auto_enable_demand",
]
"""Why a plugin was activated this process — surfaced on ``PluginAPI``.

Mirrors OpenClaw's ``createPluginActivationSource`` pattern at
``sources/openclaw/src/plugins/config-state.ts``. Plugins can read
``api.activation_source`` inside ``register(api)`` and behave differently
based on WHY they were enabled (e.g. log at INFO when user-enabled,
at DEBUG when auto-enabled).

Values:

* ``bundled``             — shipped under ``extensions/`` (auto-active
                            per manifest ``enabled_by_default`` or the
                            active profile's preset).
* ``global_install``      — user installed via
                            ``opencomputer plugin install --global``.
* ``profile_local``       — user installed into the active profile's
                            plugin directory.
* ``workspace_overlay``   — enabled via ``.opencomputer/config.yaml``
                            in the current workspace (Phase 14.N).
* ``user_enable``         — explicit ``opencomputer plugin enable <id>``
                            toggled the plugin on in the active profile.
* ``auto_enable_default`` — manifest ``enabled_by_default: true`` on
                            fresh install (first-run wizard path).
* ``auto_enable_demand``  — demand-driven activation (Sub-project E:
                            a tool-not-found signal resolved to this
                            plugin via ``tool_names`` and the user
                            accepted the auto-enable prompt).
"""


VALID_ACTIVATION_SOURCES: frozenset[str] = frozenset(get_args(PluginActivationSource))
"""Materialised tuple of ``PluginActivationSource`` values for runtime checks.

``Literal`` is erased at runtime, so callers that want to validate a
string argument (``PluginAPI.__init__``) need a concrete set. This is
the single source of truth — both the Literal and the set are
maintained together; updating one without the other is caught by
``test_activation_source_literal_exports_seven_values``.
"""


# ─── Stop reasons ──────────────────────────────────────────────────────


class StopReason(str, Enum):
    """Why a conversation step ended."""

    END_TURN = "end_turn"  # model produced final response, no more tool calls
    TOOL_USE = "tool_use"  # model wants to call tools — loop continues
    MAX_TOKENS = "max_tokens"  # hit output limit
    INTERRUPTED = "interrupted"  # user cancelled
    BUDGET_EXHAUSTED = "budget_exhausted"  # iteration budget spent
    ERROR = "error"  # unrecoverable error
    PAUSE_TURN = "pause_turn"  # server-tool work paused; re-send to continue (cap 3)
    REFUSAL = "refusal"  # model refused; surface as final, do not retry


# ─── Plugin exceptions ─────────────────────────────────────────────────


class SingleInstanceError(RuntimeError):
    """Raised when a ``single_instance`` plugin can't acquire its lock.

    Single-instance plugins own an exclusive resource (a bot token, a
    UDP port, an OS-level mutex). Only ONE copy can run at a time across
    all profiles on the machine. Core enforces this via an atomic PID
    lock at ``~/.opencomputer/.locks/<plugin-id>.lock``. This exception
    carries the plugin id and the PID currently holding the lock so
    callers can render a helpful message.

    Subclasses ``RuntimeError`` so generic ``except RuntimeError`` paths
    (e.g. in ``PluginRegistry.load_all``) will catch it. Added in Phase
    12b.2 (Sub-project B, Task B6).
    """


# ─── Processing lifecycle ──────────────────────────────────────────────


class ProcessingOutcome(str, Enum):
    """Outcome reported to ``BaseChannelAdapter.on_processing_complete``.

    Used by the reaction lifecycle hook (PR 2 of the Hermes channel-port
    series): adapters that opt into the REACTIONS capability translate
    these outcomes into platform-native reactions (e.g. ✅ / ❌ on
    Telegram messages) once the agent finishes processing a message.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


__all__ = [
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
    "VALID_ACTIVATION_SOURCES",
    "StopReason",
    "SingleInstanceError",
]
