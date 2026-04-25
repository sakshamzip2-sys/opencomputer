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
    tool_names: tuple[str, ...] = ()
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
    "VALID_ACTIVATION_SOURCES",
    "StopReason",
    "SingleInstanceError",
]
