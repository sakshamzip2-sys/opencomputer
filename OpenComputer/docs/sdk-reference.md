# `plugin_sdk` reference

One section per public export in
[`plugin_sdk/__init__.py`](../plugin_sdk/__init__.py). Everything a
plugin is allowed to import lives here; anything missing from this
document is **not** part of the plugin contract.

Every type below is importable from the package root:

```python
from plugin_sdk import Message, ToolCall, BaseProvider, HookSpec   # etc.
```

> Skim this file when you need the 30-second shape of a type. For
> the guided tour, read [`plugin-authors.md`](./plugin-authors.md)
> first.

---

## Core types

### `__version__`

Package version string (currently `"0.1.0"`). Bumped on contract changes.

### `Role`

String literal: `"system" | "user" | "assistant" | "tool"` — the
`Message.role` value.

### `Message`

Frozen dataclass — one conversation turn. Fields: `role`, `content`,
optional `tool_call_id`, `tool_calls`, `name`, `reasoning`.

```python
from plugin_sdk import Message
msg = Message(role="user", content="Hello")
```

### `ToolCall`

Frozen dataclass — a model-issued tool invocation. Fields: `id`,
`name`, `arguments` (dict).

```python
from plugin_sdk import ToolCall
ToolCall(id="t-1", name="Read", arguments={"path": "/etc/hosts"})
```

### `ToolResult`

Frozen dataclass — the result of executing a `ToolCall`. Fields:
`tool_call_id`, `content`, `is_error`.

### `Platform`

Enum of messaging platforms: `CLI`, `TELEGRAM`, `DISCORD`, `SLACK`,
`WHATSAPP`, `SIGNAL`, `IMESSAGE`, `WEB`. Channel adapters set one as
their `platform` class attribute.

### `MessageEvent`

Frozen dataclass — an inbound message in platform-agnostic form.
Fields: `platform`, `chat_id`, `user_id`, `text`, `timestamp`,
`attachments`, `metadata`.

### `SendResult`

Frozen dataclass returned by `BaseChannelAdapter.send()`. Fields:
`success`, `message_id`, `error`.

### `PluginManifest`

Frozen dataclass mirror of `plugin.json`. Parsed by the loader;
plugins rarely construct one by hand. Fields map 1:1 to manifest
keys — see [`plugin-authors.md`](./plugin-authors.md) §2 for the
"when to set" table.

### `ModelSupport`

Frozen dataclass declaring which model ids a provider plugin can
serve. Set on `PluginManifest.model_support` from the JSON
`"model_support"` key. Two fields, both tuples:

- `model_prefixes` — `tuple[str, ...]` of `str.startswith` prefixes
  (e.g. `("claude-",)` for Anthropic, `("gpt-", "o1", "o3", "o4")`
  for OpenAI).
- `model_patterns` — `tuple[str, ...]` of regex strings tried via
  `re.search` BEFORE prefixes win.

The plugin loader auto-activates a matching provider plugin even
when the user's profile preset didn't list it: pick `gpt-4o` and
`openai-provider` comes along. Mirrors OpenClaw's `modelSupport`
field at `sources/openclaw-2026.4.23/src/plugins/providers.ts`. See
[`plugin-authors.md`](./plugin-authors.md) §2 for the JSON shape.

### `PluginSetup`

Frozen dataclass holding cheap setup metadata exposed before the
plugin's Python is imported. Used by the setup wizard +
`opencomputer doctor` so provider plugins self-describe their
env-var / auth-method requirements rather than core hard-coding the
knowledge. Two fields:

- `providers` — `tuple[SetupProvider, ...]` of provider ids this
  plugin exposes during setup.
- `requires_runtime` — `bool`. Default `False` matches every
  bundled plugin; set `True` if setup needs to import the plugin to
  finish.

Mirrors OpenClaw's `PluginManifestSetup` at
`sources/openclaw-2026.4.23/src/plugins/manifest.ts:85-97`. Default
`PluginManifest.setup` is `None` (no declarations).

### `SetupProvider`

Frozen dataclass for one provider id surfaced during setup. Six
fields:

- `id` — provider id (e.g. `"anthropic"`, `"openai"`).
- `auth_methods` — `tuple[str, ...]` (e.g. `("api_key", "bearer")`).
- `env_vars` — `tuple[str, ...]`. **Order matters**: the first
  entry is treated as canonical by setup tools.
- `label` — human-readable name shown in `opencomputer setup`
  (G.24). Empty string falls back to `id`.
- `default_model` — model id pre-filled by the wizard (G.24).
- `signup_url` — where the user can obtain an API key (G.24).

Mirrors OpenClaw's `PluginManifestSetupProvider` at
`sources/openclaw-2026.4.23/src/plugins/manifest.ts:76-83`.

### `SetupChannel`

Frozen dataclass for one channel id surfaced during setup
(symmetric to `SetupProvider`, G.25). Five fields:

- `id` — channel id (e.g. `"telegram"`, `"discord"`).
- `env_vars` — `tuple[str, ...]`. Order matters; first entry is
  the primary credential, subsequent entries cover supplemental
  auth (allowlists, webhook secrets).
- `label` — human-readable name (e.g. `"Telegram"`).
- `signup_url` — where the user can obtain the credential (e.g.
  `https://t.me/BotFather`).
- `requires_user_id` — `bool`. Telegram-style: when `True`, the
  wizard prompts for a user-id allowlist after the bot token.

Channel plugins now self-describe in `opencomputer setup` —
add a `setup.channels[]` block to your `plugin.json` and the
wizard knows how to walk a user through your channel.

### `PluginActivationSource`

`Literal` describing WHY the plugin was activated this process. Core
threads the origin through `PluginAPI.activation_source` so
`register(api)` can adapt — e.g. verbose onboarding on `user_enable`,
quiet on `auto_enable_demand`. The seven values are: `bundled`,
`global_install`, `profile_local`, `workspace_overlay`, `user_enable`,
`auto_enable_default`, `auto_enable_demand`. Mirrors OpenClaw's
`createPluginActivationSource` at
`sources/openclaw/src/plugins/config-state.ts`.

```python
def register(api):
    if api.activation_source == "user_enable":
        api.hooks.notify("thanks for enabling <plugin>!")
    elif api.activation_source == "auto_enable_demand":
        # Quiet — the user didn't explicitly ask for us.
        pass
```

Default is `"bundled"` — backwards compatible for every
`extensions/*` plugin shipped before I.7.

### `StopReason`

Enum of reasons a turn ended: `END_TURN`, `TOOL_USE`, `MAX_TOKENS`,
`INTERRUPTED`, `BUDGET_EXHAUSTED`, `ERROR`. Providers set it on
`ProviderResponse.stop_reason`.

### `ProcessingOutcome`

String enum reported to `BaseChannelAdapter.on_processing_complete`
once the agent finishes handling an inbound message: `SUCCESS`,
`FAILURE`, `CANCELLED`. Adapters that opt into the `REACTIONS`
capability translate these into platform-native reactions (e.g. ✅ /
❌ on Telegram). The value is a plain string so log lines can compare
against `"success"` etc. without enum imports.

### `SingleInstanceError`

`RuntimeError` subclass raised by the plugin loader when a
`single_instance` plugin can't acquire its exclusive PID lock at
`~/.opencomputer/.locks/<plugin-id>.lock`. If your plugin owns an
exclusive resource (bot token, UDP port), set `single_instance: true`
in `plugin.json`; core handles the lock automatically and will raise
this when a second profile tries to load the same plugin.

```python
from plugin_sdk import SingleInstanceError

try:
    ...
except SingleInstanceError as e:
    # Another profile already owns the resource — fall back gracefully.
    ...
```

`PluginRegistry.load_all` catches this internally and downgrades it to
a WARNING so one contended plugin doesn't block the rest.

---

## Tool contract

### `BaseTool`

Abstract base for every tool. Subclass and implement the `schema`
property + `execute` coroutine.

```python
from plugin_sdk import BaseTool, ToolSchema, ToolCall, ToolResult

class EchoTool(BaseTool):
    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="echo",
            description="Echo the input.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content=call.arguments["text"])
```

### `ToolSchema`

Frozen dataclass — JSON Schema describing a tool. Has
`to_openai_format()` and `to_anthropic_format()` helpers.

---

## Provider contract

### `BaseProvider`

Abstract base for LLM providers. Implement `complete()` (one-shot) and
`stream_complete()` (streaming). Both are message-shaped so the agent
loop stays backend-agnostic.

```python
from plugin_sdk import BaseProvider, ProviderResponse, StreamEvent, Message, Usage

class MyProvider(BaseProvider):
    name = "my-provider"
    default_model = "my-v1"

    async def complete(self, *, model, messages, **kw) -> ProviderResponse:
        return ProviderResponse(
            message=Message(role="assistant", content="hi"),
            stop_reason="end_turn",
            usage=Usage(),
        )

    async def stream_complete(self, *, model, messages, **kw):
        yield StreamEvent(kind="text_delta", text="hi")
        final = await self.complete(model=model, messages=messages)
        yield StreamEvent(kind="done", response=final)
```

### `ProviderResponse`

Frozen dataclass — returned by `complete()`. Fields: `message` (the
assistant turn, possibly with `tool_calls`), `stop_reason`, `usage`.

### `StreamEvent`

Frozen dataclass — one event from `stream_complete()`. Three kinds:
`"text_delta"` (incremental text), `"tool_call"` (assembled call),
`"done"` (final; `response` carries the aggregated `ProviderResponse`).

### `Usage`

Frozen dataclass — token counts: `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_write_tokens`.

<<<<<<< HEAD
### `ProviderCapabilities`

Frozen dataclass each provider returns from its `capabilities` property
(default: everything off / safe). Drives provider-agnostic context
economy decisions in the agent loop:

* `requires_reasoning_resend_in_tool_cycle: bool` — set True when the
  provider requires the assistant message that originally produced a
  `tool_use` to include the corresponding reasoning block (with
  signature) when the `tool_result` is sent back. Anthropic extended
  thinking requires this; OpenAI Chat Completions does not.
* `reasoning_block_kind: Literal["anthropic_thinking", "openai_reasoning", None]` —
  opaque tag the provider uses to distinguish its reasoning replay
  shape.
* `extracts_cache_tokens: Callable[[Any], CacheTokens]` — maps the
  provider's usage payload to canonical `CacheTokens(read, write)`.
  Default returns zeros.
* `min_cache_tokens: Callable[[str], int]` — minimum block size (in
  tokens) for which a `cache_control` marker is worth placing.
  Provider-aware (receives the model name). Default returns 0
  (no filter).
* `supports_long_ttl: bool` — True if the provider exposes a 1-hour
  cache TTL knob (Anthropic only today).

### `CacheTokens`

Frozen dataclass — `read: int = 0`, `write: int = 0`. Returned by a
provider's `extracts_cache_tokens(usage)` capability when surfacing
prompt-cache token counts uniformly across providers.
=======
### `JsonSchemaSpec`

TypedDict for structured outputs (Subsystem C, 2026-05-02). Pass
to `complete()`/`stream_complete()` via the `response_schema` kwarg
to get schema-validated JSON responses. Providers translate to their
native shape — Anthropic `output_config.format`, OpenAI
`response_format` with `strict: true`. Fields: `schema` (JSON Schema
dict, required), `name` (optional, surfaced to OpenAI's
`json_schema.name`), `description` (optional one-liner). Use the
`opencomputer.agent.structured.parse_structured()` helper for
Pydantic-model integration.
>>>>>>> 9d7fd568 (feat: structured outputs (Subsystem C))

---

## Channel contract

### `BaseChannelAdapter`

Abstract base for messaging channel plugins. The gateway sets an
inbound handler via `set_message_handler`; the adapter translates
platform events into `MessageEvent` and relays outbound text via
`send()`.

```python
from plugin_sdk import BaseChannelAdapter, Platform, SendResult

class DummyAdapter(BaseChannelAdapter):
    platform = Platform.CLI

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id: str, text: str, **kw) -> SendResult:
        print(f"{chat_id}: {text}")
        return SendResult(success=True)
```

### `ChannelCapabilities`

`enum.Flag` declaring the optional features an adapter supports
(typing, reactions, photo/document/voice in/out, edit, delete, threads).
Adapters set ``capabilities = ChannelCapabilities.X | ChannelCapabilities.Y``;
callers check via ``adapter.capabilities & ChannelCapabilities.X`` before
calling optional methods. Default optional methods raise
``NotImplementedError`` so adapters only override what they actually support.

```python
from plugin_sdk import BaseChannelAdapter, ChannelCapabilities, Platform

class FullFeaturedAdapter(BaseChannelAdapter):
    platform = Platform.TELEGRAM
    capabilities = (
        ChannelCapabilities.TYPING
        | ChannelCapabilities.PHOTO_OUT
        | ChannelCapabilities.REACTIONS
    )
    # ... overrides for send_photo, send_reaction
```

---

## Hooks

### `HookEvent`

Enum of lifecycle events: `PRE_TOOL_USE`, `POST_TOOL_USE`, `STOP`,
`SESSION_START`, `SESSION_END`, `USER_PROMPT_SUBMIT`, `PRE_COMPACT`,
`SUBAGENT_STOP`, `NOTIFICATION`.

### `HookContext`

Frozen dataclass — read-only data passed to every hook call. Fields:
`event`, `session_id`, `tool_call`, `tool_result`, `message`, `runtime`.
Not every event populates every field — check for `None`.

### `HookDecision`

Frozen dataclass — what a hook returns. `decision` is one of
`"approve" | "block" | "pass"`. Only `PreToolUse` hooks should actively
block; everything else returns `"pass"` (or `None`, equivalent).

### `HookHandler`

Async callable type: `(HookContext) -> Awaitable[HookDecision | None]`.

### `HookSpec`

Frozen dataclass — registered via `api.register_hook(spec)`. Fields:
`event`, `handler`, optional `matcher` (regex on tool names for
tool events), `fire_and_forget`.

```python
from plugin_sdk import HookSpec, HookEvent, HookDecision

async def log_tool(ctx):
    print(f"about to run {ctx.tool_call.name}")
    return HookDecision(decision="pass")

api.register_hook(HookSpec(
    event=HookEvent.PRE_TOOL_USE,
    handler=log_tool,
    matcher=r"Read|Write",
))
```

### `ALL_HOOK_EVENTS`

Tuple of every `HookEvent` value in declaration order. Useful when
registering one handler against every event (audit logging).

```python
from plugin_sdk import ALL_HOOK_EVENTS, HookSpec
for ev in ALL_HOOK_EVENTS:
    api.register_hook(HookSpec(event=ev, handler=audit_log))
```

---

## Runtime + injection

### `RuntimeContext`

Frozen dataclass — per-invocation flags. Fields: `plan_mode`,
`yolo_mode`, `agent_context` (`"chat" | "cron" | "flush" | "review"`),
plus a `custom: dict` escape hatch for third-party modes.

### `DEFAULT_RUNTIME_CONTEXT`

Sentinel `RuntimeContext()` with defaults — used when callers don't
care about modes. Prefer reading `ctx.runtime.plan_mode` etc. over
constructing your own.

### `RequestContext`

Frozen dataclass — per-REQUEST scope populated by the gateway during a
dispatch. Fields: `request_id` (UUID), `channel` (e.g. `"telegram"`,
`"wire"`), `user_id`, `session_id`, `started_at` (`time.monotonic()`
reading).

Plugins read this via `api.request_context` (returns `None` outside a
dispatch — the CLI + direct `AgentLoop` path produces no scope). The
gateway enters a scope with `api.in_request(ctx)` around each inbound
message. Nested scopes on one `PluginAPI` raise `RuntimeError` — one
request in flight at a time per scope.

Use cases: auth gating (check `ctx.channel` + `ctx.user_id` against an
allowlist), rate limiting (key a token-bucket on
`(channel, user_id)`), and activation-context queries ("am I running
from Telegram or from the CLI right now?"). Matches OpenClaw's per-
request plugin scope at
`sources/openclaw/src/gateway/server-plugins.ts`.

### `DynamicInjectionProvider`

Abstract base — implement `collect(ctx)` to return a string that gets
appended to the system prompt, or `None` to skip. `priority` orders
providers (lower first).

```python
from plugin_sdk import DynamicInjectionProvider, InjectionContext

class PlanHint(DynamicInjectionProvider):
    priority = 10
    @property
    def provider_id(self) -> str:
        return "plan-hint"
    def collect(self, ctx: InjectionContext) -> str | None:
        return "Plan mode ON" if ctx.runtime.plan_mode else None
```

### `InjectionContext`

Frozen dataclass passed to `DynamicInjectionProvider.collect(...)`.
Fields: `messages` (full history), `runtime`, `session_id`,
`turn_index`.

---

## Doctor

### `HealthContribution`

Frozen dataclass — one named check + its async runner. Register via
`api.register_doctor_contribution(contribution)`.

```python
from plugin_sdk import HealthContribution, RepairResult

async def run_check(fix: bool) -> RepairResult:
    return RepairResult(id="my-check", status="pass")

api.register_doctor_contribution(HealthContribution(
    id="my-check",
    description="Checks the thing.",
    run=run_check,
))
```

### `HealthRunFn`

Async callable type: `(fix: bool) -> Awaitable[RepairResult]`. If
`fix=True`, the contribution is expected to repair in place before
returning.

### `HealthStatus`

String literal: `"pass" | "warn" | "fail" | "skip"`. Set on
`RepairResult.status`.

### `RepairResult`

Frozen dataclass — outcome of one check. Fields: `id`, `status`,
`detail`, `repaired` (True only when `fix=True` actually mutated state).

---

## Interaction

### `InteractionRequest`

Frozen dataclass — a question the agent asks the user. Fields:
`question`, `options`, `presentation` (`"text" | "choice"`). Used by
the built-in `AskUserQuestion` tool.

### `InteractionResponse`

Frozen dataclass — the user's reply. Fields: `text`, `option_index`
(set if the user picked one of the supplied options).

---

## Memory

### `MemoryProvider`

Abstract base for external memory plugins (Honcho, Mem0, Cognee). At
most one may be active per session. Required methods: `provider_id`
(property), `tool_schemas()`, `handle_tool_call()`, `prefetch()`,
`sync_turn()`, `health_check()`. Optional: `on_session_start`,
`on_session_end`.

```python
from plugin_sdk import MemoryProvider, ToolSchema, ToolCall, ToolResult

class MyMemory(MemoryProvider):
    @property
    def provider_id(self) -> str:
        return "my-memory:default"
    def tool_schemas(self) -> list[ToolSchema]:
        return []
    async def handle_tool_call(self, call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content="", is_error=True)
    async def prefetch(self, query: str, turn_index: int) -> str | None:
        return None
    async def sync_turn(self, user: str, assistant: str, turn_index: int) -> None:
        return None
    async def health_check(self) -> bool:
        return True
```

---

## Slash commands

### `SlashCommand`

Abstract base for plugin-authored in-chat slash commands (e.g. `/plan`,
`/diff`). Set the class attributes `name` (no leading slash) and
`description`, and implement async `execute(args, runtime)` returning a
`SlashCommandResult`. Register via `api.register_slash_command(cmd)`
from your plugin's `register(api)`. Legacy duck-typed commands that
return a bare `str` from `execute` are accepted for backwards compat —
the dispatcher wraps them into a `SlashCommandResult` transparently.

```python
from plugin_sdk import SlashCommand, SlashCommandResult, RuntimeContext

class HelloCommand(SlashCommand):
    name = "hello"
    description = "Say hi."

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        return SlashCommandResult(output=f"hi {args}".strip(), handled=True)
```

### `SlashCommandResult`

Frozen dataclass returned by `SlashCommand.execute`. Fields: `output:
str` (shown to the user) and `handled: bool = True`. When `handled=True`
the agent loop returns early without invoking the LLM — zero tokens
for the turn. Set `handled=False` for side-effect commands that flip a
flag and want the chat turn to proceed (rare).

---

## Consent (Sub-project F1)

Consent primitives gate privileged tool calls. A tool declares what
capabilities it needs via a `CapabilityClaim` on its class; the core
`ConsentGate` resolves each claim against stored `ConsentGrant`s and
returns a `ConsentDecision`. The gate runs in `AgentLoop` BEFORE any
`PreToolUse` hook, so it cannot be bypassed by disabling a plugin.
See `~/.claude/plans/i-want-you-to-twinkly-squirrel.md` for the full
architectural rationale.

### `ConsentTier`

`IntEnum` with four ordered tiers. Lower value = less friction, less
trust required:
- `IMPLICIT` (0) — user told agent in chat; no external data read.
- `EXPLICIT` (1) — user clicked "enable" for a source; revocable.
- `PER_ACTION` (2) — per-action prompt naming the specific data.
- `DELEGATED` (3) — time-windowed autonomy, capability-scoped.

### `CapabilityClaim`

Frozen dataclass a plugin attaches to its `BaseTool` subclass to declare
what the tool needs. Fields: `capability_id: str`, `tier_required:
ConsentTier`, `human_description: str`, `data_scope: str | None = None`.
The gate uses `capability_id` + (optionally) runtime scope to match
against grants.

### `ConsentGrant`

Frozen dataclass representing a user-approved grant. Fields:
`capability_id: str`, `tier: ConsentTier`, `scope_filter: str | None`,
`granted_at: float`, `expires_at: float | None` (null = never expires),
`granted_by: Literal["user", "auto", "promoted"]`. Grants persist in the
per-profile SQLite `consent_grants` table.

### `ConsentDecision`

Frozen dataclass returned by `ConsentGate.check`. Fields: `allowed:
bool`, `reason: str` (human-readable), `tier_matched: ConsentTier |
None`, `audit_event_id: int | None` (row id in the append-only
`audit_log`). Plugins don't construct this themselves — the core
produces it.

---

## Ingestion / Signal bus (Phase 3.A, F2)

The `plugin_sdk.ingestion` module is the public vocabulary for the
shared typed-event bus. Publishers emit `SignalEvent` subclass
instances to `opencomputer.ingestion.bus.default_bus`; subscribers
attach via `default_bus.subscribe("tool_call", handler)` or
`default_bus.subscribe_pattern("web_*", handler)`.

### `SignalEvent`

Frozen+slots base dataclass with `event_id: str` (UUID4,
auto-generated), `event_type: str` (discriminator), `timestamp: float`
(Unix epoch seconds), `session_id: str | None`, `source: str`,
`metadata: Mapping[str, Any]`. Every concrete event inherits this
shape. Subclasses set `event_type` via a default — don't override at
construction time.

### `ToolCallEvent`

Subclass with `tool_name: str`, `arguments: Mapping[str, Any]`,
`outcome: Literal["success","failure","blocked","cancelled"]`
(see `ToolCallOutcome`), `duration_seconds: float`. Emitted by the
agent loop after each tool invocation settles. `event_type =
"tool_call"`.

### `WebObservationEvent`

Subclass with `url: str`, `domain: str`, `content_kind: Literal["html",
"json","text","markdown"]` (see `WebContentKind`), `payload_size_bytes:
int`. Emitted by web-scraping plugins. `event_type =
"web_observation"`.

### `FileObservationEvent`

Subclass with `path: str`, `operation: Literal["read","write","stat",
"delete","list"]` (see `FileOperation`), `size_bytes: int | None`.
`event_type = "file_observation"`.

### `MessageSignalEvent`

Subclass with `role: Literal["user","assistant","system","tool"]` (see
`MessageRole`), `content_length: int` (NOT the raw content — privacy
preservation). Named with the `Signal` infix to avoid shadowing the
unrelated `MessageEvent` channel-adapter dataclass in `plugin_sdk.core`.
`event_type = "message"`.

### `HookSignalEvent`

Subclass with `hook_name: str`, `decision: Literal["pass","approve",
"block"]` (see `HookDecisionKind`), `reason: str`. Named with the
`Signal` infix to avoid shadowing the unrelated `HookEvent` enum in
`plugin_sdk.hooks`. `event_type = "hook"`.

### `SignalNormalizer` + `IdentityNormalizer`

Abstract base + concrete pass-through. Subclass `SignalNormalizer` and
implement `normalize(raw: Any) -> SignalEvent | None` to adapt
third-party objects into the typed vocabulary; return `None` to skip.
`IdentityNormalizer` returns the input unchanged when it is already a
`SignalEvent`. Register custom normalizers via `register_normalizer(
event_type, normalizer)`; look them up with `get_normalizer(
event_type)`; `clear_normalizers()` is a test-only reset helper.

---

## Behavioral inference (Phase 3.B)

The `plugin_sdk.inference` module is the public vocabulary for the
behavioral-inference engine. Phase 3.B ships three heuristic
extractors that subscribe to the F2 bus, derive structured "things we
noticed" patterns, and persist them as `Motif` records to a SQLite
store under `<profile_home>/inference/motifs.sqlite`. Phase 3.C
user-model graph reads from `MotifStore.list(kind=...)`.

### `Motif`

Frozen+slots dataclass — one observed pattern. Fields: `motif_id: str`
(UUID4 default factory); `kind: MotifKind` discriminator
(`"temporal"`, `"transition"`, `"implicit_goal"`); `confidence: float`
in `[0.0, 1.0]`; `support: int` (events that contributed); `summary:
str` (one-line human-readable, safe for direct display); `payload:
Mapping[str, Any]` (kind-specific JSON-serialisable structured data);
`evidence_event_ids: tuple[str, ...]` (bus event ids); `created_at:
float`; `session_id: str | None` (set only for per-session motifs
like `"implicit_goal"`).

### `MotifExtractor`

`runtime_checkable` Protocol. Required class vars: `name: ClassVar[str]`
(stable id), `kind: ClassVar[MotifKind]` (discriminator emitted on
the resulting motifs). One method: `extract(self, events:
Sequence[SignalEvent]) -> list[Motif]` — pure, no side effects.
`opencomputer.inference.engine.BehavioralInferenceEngine` is the
internal default-bus subscriber that runs configured extractors over
event batches and persists results.

### `MotifKind`

`Literal["temporal", "transition", "implicit_goal"]` — alphabet of
valid `Motif.kind` values. Adding a new kind is a coordinated change
across the SDK (this Literal), the storage schema (`MotifStore` knows
no schema for unknown kinds), and downstream consumers (Phase 3.C).

---

## User-model graph (Phase 3.C, F4 layer)

The `plugin_sdk.user_model` module is the public vocabulary for the
user-model graph (nodes + edges) read by the context-assembly path.
Concrete storage + ranking lives in `opencomputer/user_model/` (internal).
Edges carry four-factor weights (`salience × confidence × recency_weight
× source_reliability`) that the `ContextRanker` multiplies for selection.

### `NodeKind`

`Literal["identity", "attribute", "relationship", "goal", "preference"]`
— alphabet of valid `Node.kind` values. Extending this tuple is a
**breaking change**; downstream scorers dispatch on the literal.

### `EdgeKind`

`Literal["asserts", "contradicts", "supersedes", "derives_from"]` —
alphabet of valid `Edge.kind` values. `contradicts` is reserved for
Phase 3.D drift detection and explicit user statements; motif import
never emits it.

### `Node`

Frozen+slots dataclass — a stable entity tracked about the user.
Fields: `node_id: str` (UUID4), `kind: NodeKind`, `value: str`
(human-readable, e.g. `"prefers Python over JS"`), `confidence: float`
(node-level prior, updated by aggregation), `created_at: float`,
`last_seen_at: float` (bumped on repeat assertion), `metadata: Mapping`.

```python
from plugin_sdk import Node
Node(kind="goal", value="learn Rust by Q3")
```

### `Edge`

Frozen+slots dataclass — a typed assertion between two nodes.
Fields: `edge_id: str` (UUID4), `kind: EdgeKind`, `from_node: str`,
`to_node: str`, `salience: float`, `confidence: float`,
`recency_weight: float` (updated by Phase 3.D decay),
`source_reliability: float` (1.0 explicit user / 0.6 motif / 0.4
scraped), `decay_rate: float` (per-day, default 0.01),
`created_at: float`, `evidence: Mapping[str, Any]` (e.g.
`{"motif_id": "...", "event_ids": [...]}`).

### `UserModelQuery`

Frozen+slots dataclass — the ranker input. Fields: `kinds:
tuple[NodeKind, ...] | None` (filter), `text: str | None` (FTS5 query),
`top_k: int = 20`, `token_budget: int | None` (char-approx; 4 chars ≈
1 token).

### `UserModelSnapshot`

Frozen+slots dataclass — the ranker output. Fields: `nodes:
tuple[Node, ...]` (ordered, most salient first), `edges:
tuple[Edge, ...]` (incident edges of selected nodes), `total_score:
float` (sum of per-node scores), `truncated: bool` (`True` if
`token_budget` cut selection short).

---

## Sandbox (Phase 3.E)

The `plugin_sdk.sandbox` module is the public contract for pluggable
containment. Concrete strategies live in `opencomputer/sandbox/` (internal
— may evolve). Plugins import the contract types from here; tools that
need to spawn untrusted argv use the `run_sandboxed(...)` helper from
the core.

### `SandboxConfig`

Frozen+slots dataclass describing per-invocation policy. Fields:
`strategy: SandboxStrategyName = "auto"` (auto / macos_sandbox_exec /
linux_bwrap / docker / none); `cpu_seconds_limit: int = 60` (wall-clock
cap, enforced via subprocess timeout); `memory_mb_limit: int = 512`
(passed to bwrap+docker, ignored by macOS sandbox-exec); `network_allowed:
bool = False` (default deny); `read_paths: tuple[str, ...] = ()`
(extra paths the sandboxed process may read); `write_paths: tuple[str,
...] = ()` (extra writable paths); `allowed_env_vars: tuple[str, ...] =
("PATH", "HOME", "LANG", "LC_ALL")` (env-var allowlist); `image: str
= "alpine:latest"` (Docker image — only used by the docker strategy).
Defaults are conservative: deny network, no extra paths, 60s wall
clock, 512 MB memory.

### `SandboxResult`

Frozen+slots dataclass returned by every strategy's `run()`. Fields:
`exit_code: int`, `stdout: str`, `stderr: str`, `duration_seconds:
float`, `wrapped_command: list[str]` (the actual argv that was
executed, useful for debugging / auditing), `strategy_name: str`
(short id of the strategy that produced this result).

### `SandboxStrategy`

Abstract base class for concrete strategies. Subclasses live in
`opencomputer/sandbox/`. ABC contract: `name: ClassVar[str]`,
`is_available(self) -> bool` (cheap, cached host capability check),
`async run(self, argv, *, config, stdin=None, cwd=None) -> SandboxResult`,
`explain(self, argv, *, config) -> list[str]` (returns the wrapped
command without running it — useful for `--dry-run`). Implementations
MUST use `asyncio.create_subprocess_exec` (never the blocking
`subprocess` module), strip env vars not in `config.allowed_env_vars`,
and enforce `config.cpu_seconds_limit` via timeout.

### `SandboxStrategyName`

`Literal["auto","macos_sandbox_exec","linux_bwrap","docker","none"]`
— the alphabet of valid `SandboxConfig.strategy` values.

### `SandboxUnavailable`

`RuntimeError` subclass raised when a requested strategy can't run on
the host. The `auto` strategy raises this only when **no** strategy is
available; the message names `SandboxConfig(strategy="none")` as the
opt-out for trusted internal use.

---

## Temporal decay + drift (Phase 3.D, F5 layer)

Phase 3.D ages out user-model edges and flags distribution shifts in
the 3.B motif stream. These primitives are the public contract;
concrete engines live in `opencomputer/user_model/{decay,drift,
drift_store,scheduler}.py`.

### `DecayConfig`

Frozen+slots dataclass of per-edge-kind half-life knobs for the
exponential decay formula `weight = 0.5 ** (age_days / half_life)`
floored at `min_recency_weight`. Fields: `asserts_half_life_days:
float = 30.0`, `contradicts_half_life_days: float = 14.0`,
`supersedes_half_life_days: float = 60.0`,
`derives_from_half_life_days: float = 21.0`, `min_recency_weight:
float = 0.05` (floor below which decay stops), `default_half_life_days:
float = 30.0` (fallback for unknown edge kinds).

### `DriftConfig`

Frozen+slots dataclass of knobs for symmetrized-KL drift detection
over motif distributions. Fields: `recent_window_days: float = 7.0`,
`min_lifetime_count: int = 5` (drop sparse labels from KL), `kl_significance_threshold:
float = 0.5` (total_kl above this → `significant=True`),
`top_changes_count: int = 5` (how many biggest label deltas the report
carries), `smoothing_epsilon: float = 0.01` (Laplace smoothing for
zero counts).

### `DriftReport`

Frozen+slots dataclass summarising one drift-detection run. Fields:
`report_id: str` (UUID4), `created_at: float` (unix epoch seconds),
`window_seconds: float` (recent window analysed),
`total_kl_divergence: float`, `per_kind_drift: Mapping[str, float]`
(KL grouped by motif kind prefix), `recent_distribution: Mapping[str,
int]`, `lifetime_distribution: Mapping[str, int]`, `top_changes:
tuple[Mapping[str, Any], ...]` (biggest deltas — each entry carries
`label`, `recent_count`, `lifetime_count`, `delta_ratio`),
`significant: bool` (total_kl > threshold). Persisted by
`opencomputer.user_model.drift_store.DriftStore`.

---

## Transport ABC (PR-C)

The `plugin_sdk.transports` module is the public contract for the
**optional** Transport layer — pluggable format conversion + HTTP
transport extracted from monolithic provider adapters. Ported from
hermes-agent v0.11 `agent/transports/` (PRs #10549, #13347 etc.).

**Backwards compatible**: existing providers continue to inherit
`BaseProvider` directly (unchanged). The Transport ABC is a new layer
that NEW providers (e.g. `aws-bedrock-provider`) opt in to.

### `NormalizedRequest`

Frozen+slots dataclass — provider-agnostic request shape.
Fields: `model: str`, `messages: list[Message]`, `system: str = ""`,
`tools: tuple[ToolSchema, ...] = ()`, `max_tokens: int = 4096`,
`temperature: float = 1.0`, `stream: bool = False`.
Each `TransportBase` subclass converts this into the provider's native
API format (Messages API, Chat Completions, Bedrock Converse, etc.).

### `NormalizedResponse`

Frozen+slots dataclass — provider-agnostic response shape.
Fields: `provider_response: ProviderResponse` (wraps the existing type
for backwards compat), `raw_native: Any = None` (raw provider payload,
for debugging). Providers that use `TransportBase` still satisfy
`BaseProvider.complete() -> ProviderResponse` by returning
`normalized.provider_response`.

### `TransportBase`

Abstract base class for the Transport layer. Subclasses implement:
- `format_request(req: NormalizedRequest) -> dict[str, Any]` — convert to provider-native dict.
- `async send(native_request: dict) -> Any` — send non-streaming request, return raw response.
- `async send_stream(native_request: dict) -> AsyncIterator[StreamEvent]` — stream events; final event is `kind="done"`.
- `parse_response(raw: Any) -> NormalizedResponse` — parse native response.

Class attribute `name: str` is a stable transport identifier
(e.g. `"bedrock"`, `"anthropic"`).

The `aws-bedrock-provider` extension ships `BedrockTransport(TransportBase)`
as the first concrete implementation. To add a new provider using this
layer, subclass both `TransportBase` and `BaseProvider`; delegate
`BaseProvider.complete` to `transport.send + transport.parse_response`.

---

## Classifier abstraction (2026-04-28)

Codebase audit found 7+ files implementing the same shape: a list of
regex patterns mapped to category labels, iterated, with a verdict
returned. This module abstracts that pattern so a future embedding /
LLM back-end can plug in via the same protocol on call sites that
benefit (vibe classifier, life-event detectors, persona classifier).

### `Classifier`

Runtime-checkable `Protocol` that a classifier back-end satisfies. One
method:

```python
def classify(self, text: str) -> ClassifierVerdict[L]:
    ...
```

`L` is the label type — `str`, `Enum`, or any other hashable. Each
implementation can choose its own labels; the protocol is generic.

### `RegexClassifier[L]`

Regex-table back-end of `Classifier`. Construct with a sequence of
`Rule[L]` plus an `AggregationPolicy`; the classifier is stateless
and thread-safe.

```python
from plugin_sdk import RegexClassifier, Rule, AggregationPolicy
import re

c = RegexClassifier(
    rules=[
        Rule(pattern=re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
             label="sql_drop", severity="critical"),
        Rule(pattern=re.compile(r"\brm\s+-rf"), label="destructive_rm"),
    ],
    policy=AggregationPolicy.ALL_MATCHES,
)
verdict = c.classify(user_input)
if verdict.has_match:
    log.warning("triggered: %s", verdict.matched_labels)
```

### `Rule[L]`

Frozen dataclass: `(pattern, label, weight=1.0, severity="", description="")`.
`weight` is only consulted by `WEIGHTED_SUM` policies; `severity` and
`description` are advisory metadata for audit output.

### `ClassifierVerdict[L]`

Frozen dataclass returned by `classify`. Fields:

- `matched_labels: list[L]` — distinct labels that fired, in policy order
- `weights_by_label: dict[L, float]` — accumulated weights (only populated for `WEIGHTED_SUM`)
- `triggered_rules: tuple[Rule[L], ...]` — every Rule object that matched (audit trail)
- `has_match: bool` — convenience property
- `top_label: L | None` — convenience property; first entry of `matched_labels` or None

### `AggregationPolicy`

Enum that selects how multiple matched rules combine into a verdict:

- `FIRST_MATCH` — return the first matching rule's label only. Rule order encodes priority.
- `ALL_MATCHES` — return every matched label, deduped, in rule order.
- `WEIGHTED_SUM` — accumulate `Rule.weight` per label; return labels ranked descending.

---

## Realtime Voice (2026-04-29)

Port of OpenClaw's `realtime-voice` abstraction. Plugins implementing
two-way streaming voice (e.g. OpenAI Realtime API) inherit
`BaseRealtimeVoiceBridge` and implement seven abstract methods. Audio
is PCM16 raw bytes; μ-law (telephony) is intentionally out of scope.

### `BaseRealtimeVoiceBridge`

ABC. Required methods: `connect()` (async), `send_audio(bytes)`,
`send_user_message(text)`, `submit_tool_result(call_id, result)`,
`trigger_greeting(instructions=None)`, `close()`, `is_connected()`.

### `RealtimeVoiceTool`

Frozen dataclass — function-tool schema sent to the realtime model on
session.update. Fields: `type` (`"function"`), `name`, `description`,
`parameters` (JSON-Schema dict).

### `RealtimeVoiceToolCallEvent`

Frozen dataclass — emitted by the bridge when the model invokes a tool
mid-stream. Fields: `item_id`, `call_id`, `name`, `args` (decoded JSON).

### `RealtimeVoiceRole`

`Literal["user", "assistant"]` — the role on `on_transcript` callbacks.

### `RealtimeVoiceCloseReason`

`Literal["completed", "error"]` — the reason passed to `on_close`.

---

## Skills Hub (2026-04-28)

Public ABC + dataclasses for the Skills Hub system. Plugins and OC's bundled
hub adapters both implement `SkillSource` to expose searchable skill registries
to the agent. Skill metadata flowing through the hub is a `SkillMeta`; full
installable content (SKILL.md + auxiliary files) is a `SkillBundle`.

### `SkillSource`

Abstract base class for skill registry adapters. Implementations expose three
methods:

- `search(query: str, limit: int = 10) -> list[SkillMeta]` — return up to
  `limit` skills matching the query.
- `fetch(identifier: str) -> SkillBundle | None` — return the full bundle for
  an identifier, or `None` if unknown.
- `inspect(identifier: str) -> SkillMeta | None` — return rich metadata for
  an identifier, or `None` if unknown.

Adapters declare a stable `name` (e.g. `"well-known"`, `"github"`,
`"agentskills_io"`) used in the `<source>/<name>` identifier form so the
router can route `fetch()` calls back to the right source.

### `SkillMeta`

Frozen dataclass — lightweight descriptor returned by
`SkillSource.search` / `inspect`. Required fields: `identifier`,
`name`, `description`. Optional: `version`, `author`, `homepage`, `tags`,
`trust_level` (defaults to `"community"`), `extra` (free-form dict).

`identifier` MUST be `<source>/<name>` form so a router can route
`fetch()` calls back to the right source.

### `SkillBundle`

Frozen dataclass — full installable content of a skill. Fields:

- `identifier: str` — same `<source>/<name>` form as `SkillMeta`
- `skill_md: str` — the SKILL.md body
- `files: dict[str, str]` — auxiliary file paths → contents

### `TrustLevel`

`Literal["builtin", "trusted", "community", "untrusted"]`. Used on
`SkillMeta.trust_level` to label the provenance of a skill so the agent
loop / approval flow can render the right warnings.

---

## Permission modes (2026-04-29)

Canonical taxonomy of session-wide tool-approval modes. New code should
read modes through the `effective_permission_mode()` helper rather than
the legacy `runtime.plan_mode` / `runtime.yolo_mode` bools — the helper
accounts for slash-command toggles in `runtime.custom`.

### `PermissionMode`

`StrEnum` with four members:

| Member | Value | Behaviour |
|---|---|---|
| `PermissionMode.DEFAULT` | `"default"` | Per-action ConsentGate prompts (status quo). |
| `PermissionMode.PLAN` | `"plan"` | Destructive tools refused; agent describes the plan. |
| `PermissionMode.ACCEPT_EDITS` | `"accept-edits"` | Auto-approve Edit/Write/MultiEdit/NotebookEdit; Bash and network still prompt. |
| `PermissionMode.AUTO` | `"auto"` | Skip per-action prompts. Audit-logged via `actor='bypass'`. |

### `effective_permission_mode(runtime: RuntimeContext) -> PermissionMode`

Resolves "what mode is this session in right now?" through this
precedence chain (top wins):

1. `runtime.custom["permission_mode"]` (canonical session-mutable key)
2. `runtime.custom["plan_mode"] == True` → `PLAN` (legacy `/plan`)
   `runtime.custom["yolo_session"] == True` → `AUTO` (legacy `/yolo`)
3. `runtime.permission_mode` (canonical CLI-set frozen field)
4. `runtime.plan_mode == True` → `PLAN` (legacy `--plan`)
   `runtime.yolo_mode == True` → `AUTO` (legacy `--yolo`)
5. `PermissionMode.DEFAULT`

Plan beats auto on conflict (matches existing CLI precedence).

---

## Profile context — per-task profile scoping

*Module: `plugin_sdk.profile_context` (re-exported from `plugin_sdk`).*

Phase 1 of the profile-as-agent multi-routing work. Lets the gateway set
an asyncio-Task-local profile home so two simultaneous dispatches each
see their own profile directory without any locking.

### `current_profile_home`

```python
from plugin_sdk import current_profile_home

home: Path | None = current_profile_home.get()
```

A `contextvars.ContextVar[Path | None]`. Default is `None` (no profile
scope active — `config._home()` falls back to `OPENCOMPUTER_HOME` env
var then `~/.opencomputer`).

Each `asyncio.Task` inherits the contextvar value at task-creation time;
mutations within a task are local to that task, so two concurrent
dispatches each see their own profile without any locking.

### `set_profile(home: Path) -> Iterator[None]`

```python
from plugin_sdk import set_profile

with set_profile(Path("~/.opencomputer/work")):
    ...  # current_profile_home.get() == Path("~/.opencomputer/work")
# restored to prior value on exit (including on exception)
```

Context manager. Binds `current_profile_home` to `home` for the duration
of the `with` block. Restores the prior value on exit (including on
exception). Safe to nest.

Will be set by `Dispatch._do_dispatch` (Phase 3) during a per-message
agent loop. Plugin authors can also use it in tests to exercise
profile-aware code paths in isolation.

### `scope_subprocess_env(env, *, profile_home) -> dict[str, str]`

```python
import os
from plugin_sdk import current_profile_home, scope_subprocess_env

profile_home = current_profile_home.get()
env = scope_subprocess_env(os.environ.copy(), profile_home=profile_home)
proc = await asyncio.create_subprocess_shell(cmd, env=env, ...)
```

Stateless helper that returns a NEW env dict with `HOME` /
`XDG_CONFIG_HOME` / `XDG_DATA_HOME` redirected to the given profile's
home directory. The caller's env is never mutated. `profile_home=None`
returns a shallow copy unchanged (subprocess inherits the parent's real
HOME).

Convention: the resolved `HOME` is `<profile_home>/home` unless
`profile_home` already ends with `/home` (in which case it's used
directly). XDG paths are derived from the resolved HOME.

Use this from plugin tools (git/ssh/npm-style spawners) to ensure
subprocesses see the active profile's tool config rather than the
user's real home. Added in PR #284 follow-up to fix the architectural
HOME-mutation regression.

---

## See also

- [`plugin-authors.md`](./plugin-authors.md) — the guided 30-minute
  quickstart.
- [`extensions/weather-example/`](../extensions/weather-example/) — a
  bundled reference plugin that uses several of these types end-to-end.
