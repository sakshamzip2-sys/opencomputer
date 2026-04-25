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

## See also

- [`plugin-authors.md`](./plugin-authors.md) — the guided 30-minute
  quickstart.
- [`extensions/weather-example/`](../extensions/weather-example/) — a
  bundled reference plugin that uses several of these types end-to-end.
