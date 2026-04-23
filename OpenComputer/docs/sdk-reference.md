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

### `StopReason`

Enum of reasons a turn ended: `END_TURN`, `TOOL_USE`, `MAX_TOKENS`,
`INTERRUPTED`, `BUDGET_EXHAUSTED`, `ERROR`. Providers set it on
`ProviderResponse.stop_reason`.

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

## See also

- [`plugin-authors.md`](./plugin-authors.md) — the guided 30-minute
  quickstart.
- [`extensions/weather-example/`](../extensions/weather-example/) — a
  bundled reference plugin that uses several of these types end-to-end.
