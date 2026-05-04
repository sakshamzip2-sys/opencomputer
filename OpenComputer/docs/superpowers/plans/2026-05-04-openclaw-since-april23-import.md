# OpenClaw-since-April-23 Selective Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port six high-leverage openclaw improvements (since 2026-04-23) into OpenComputer in one focused PR, six self-contained commits.

**Architecture:** Each item is decoupled — no cross-task dependencies. Item 1 introduces a small SDK contract change (SlashCommand `aliases` field) that is backwards-compatible (default `()`); items 2–6 are pure additions.

**Tech Stack:** Python 3.13, asyncio, pytest, ruff. New deps: `readability-lxml>=0.8.1` (pure-Python wrapper around lxml; ~25KB; pulls lxml as transitive dep).

**Worktree:** `/Users/saksham/Vscode/claude/.worktrees/openclaw-since-april23/OpenComputer/`
**Branch:** `feat/openclaw-since-april23` (created from main `aacf5b1c`)
**Spec:** `docs/superpowers/specs/2026-05-04-openclaw-since-april23-import-design.md`
**openclaw reference:** `/Users/saksham/Vscode/claude/sources/openclaw/` (HEAD `d841394eba`, 2026-05-03)

---

## Pre-Execution Verification (verified against actual OC source 2026-05-04)

**These API shapes have been verified by reading source — use these, not assumptions:**

```python
# opencomputer/tools/registry.py
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
    def register(self, tool: BaseTool) -> None:
        name = tool.schema.name           # ⚠ schema is a PROPERTY, not a method
        if name in self._tools:
            raise ValueError(...)
        self._tools[name] = tool
    def get(self, name: str) -> BaseTool | None: ...
    def unregister(self, name: str) -> None: ...

# Singleton instance imported by callers:
from opencomputer.tools.registry import registry, register_tool

# plugin_sdk/hooks.py — HookSpec uses `handler` not `callback`
@dataclass(frozen=True, slots=True)
class HookSpec:
    event: HookEvent
    handler: HookHandler          # ⚠ NOT `callback`
    matcher: str | None = None
    fire_and_forget: bool = True
    priority: int = 100

# HookEvent enum is SCREAMING_SNAKE_CASE
HookEvent.PRE_TOOL_USE   # ⚠ NOT `HookEvent.PreToolUse`
HookEvent.POST_TOOL_USE
HookEvent.STOP
HookEvent.SESSION_START
HookEvent.SESSION_END
HookEvent.USER_PROMPT_SUBMIT
HookEvent.PRE_COMPACT
# ...

# opencomputer/agent/slash_commands.py — uses `_plugin_registry.slash_commands` dict
# Aliases are added by inserting MULTIPLE keys pointing at the same instance.
# No dispatcher change needed — dict.get(name) naturally finds aliases.

# opencomputer/cli_ui/slash.py — CommandDef ALREADY HAS aliases field:
@dataclass(frozen=True)
class CommandDef:
    name: str
    description: str
    category: str = "general"
    aliases: tuple[str, ...] = field(default_factory=tuple)   # ✓ already exists

# Provider registration (extensions/openai-provider/plugin.py):
def register(api) -> None:
    api.register_provider("openai", OpenAIProvider)   # ⚠ class, not instance

# plugin.py uses dual-import pattern:
try:
    from provider import OpenAIProvider          # plugin-loader mode (root on sys.path)
except ImportError:
    from extensions.openai_provider.provider import OpenAIProvider  # package mode

# BaseProvider.complete signature is rich:
async def complete(
    self,
    *,
    model: str,
    messages: list[Message],
    system: str = "",
    tools: list[ToolSchema] | None = None,
    max_tokens: int = 4096,
    temperature: float = 1.0,
    stream: bool = False,
    runtime_extras: dict | None = None,
    response_schema: JsonSchemaSpec | None = None,
    site: str = "agent_loop",
) -> ProviderResponse: ...
```

**Workflow per task:**
1. Write failing test → run → verify it fails.
2. Implement minimum code to pass.
3. Run test → verify pass.
4. `ruff check --fix` then `ruff check` (must be clean).
5. Commit with conventional-commit message + Co-Authored-By Opus 4.7 trailer.

**Test command shape (use everywhere):**
```bash
cd /Users/saksham/Vscode/claude/.worktrees/openclaw-since-april23/OpenComputer
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/<test_file>.py -v --no-header
```

---

## Pre-flight

- [ ] **PF.1: Confirm worktree + baseline**

```bash
cd /Users/saksham/Vscode/claude/.worktrees/openclaw-since-april23/OpenComputer
git status
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_slash_mru.py tests/test_slash_picker_source.py -q --no-header
```
Expected: `28 passed`. If not, fix before proceeding.

---

## Task 1: `/side` alias for `/btw`

**Files:**
- Modify: `plugin_sdk/slash_command.py` (add `aliases` field)
- Modify: `opencomputer/agent/slash_commands_impl/btw_cmd.py` (set `aliases = ("side",)`)
- Modify: `opencomputer/agent/slash_commands.py` (register alias keys in dict)
- Create: `tests/test_slash_aliases.py`

**Reference (read first):**
```bash
sed -n '54,80p' plugin_sdk/slash_command.py
sed -n '78,100p' opencomputer/agent/slash_commands_impl/btw_cmd.py
sed -n '120,160p' opencomputer/agent/slash_commands.py
```

- [ ] **Step 1.1: Write failing test**

Create `tests/test_slash_aliases.py`:

```python
"""Tests for slash-command aliases (/side → /btw)."""

from __future__ import annotations

import pytest

from opencomputer.agent.slash_commands import (
    get_registered_commands,
    register_builtin_slash_commands,
)
from opencomputer.agent.slash_commands_impl.btw_cmd import BtwCommand
from opencomputer.plugins.registry import registry as plugin_registry
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


def test_btw_command_declares_side_alias():
    cmd = BtwCommand()
    assert "side" in cmd.aliases


def test_aliases_default_to_empty_tuple():
    """Existing commands without aliases keep working."""

    class _NoAlias(SlashCommand):
        name = "noalias"
        description = "test"

        async def execute(self, args, runtime):
            return SlashCommandResult(handled=True, output="ok")

    cmd = _NoAlias()
    assert cmd.aliases == ()


def test_side_resolves_to_btw_in_registry():
    """After register_builtin_slash_commands runs, /side and /btw both
    map to the same BtwCommand instance in the slash_commands dict."""
    register_builtin_slash_commands()
    assert "btw" in plugin_registry.slash_commands
    assert "side" in plugin_registry.slash_commands
    assert plugin_registry.slash_commands["btw"] is plugin_registry.slash_commands["side"]
    assert isinstance(plugin_registry.slash_commands["btw"], BtwCommand)


def test_alias_does_not_overwrite_existing_command():
    """If 'side' is already registered (e.g. by a plugin), the alias
    registration must NOT overwrite it — primary names win, aliases
    yield."""
    # Pre-register a stub under 'side' to simulate a plugin claiming it
    class _SideStub(SlashCommand):
        name = "side"
        description = "stub"

        async def execute(self, args, runtime):
            return SlashCommandResult(handled=True, output="stub")

    plugin_registry.slash_commands.pop("side", None)
    plugin_registry.slash_commands["side"] = _SideStub()
    # Now run idempotent re-registration — alias should yield to existing
    register_builtin_slash_commands()
    assert isinstance(plugin_registry.slash_commands["side"], _SideStub)
    # Cleanup
    plugin_registry.slash_commands.pop("side", None)
```

- [ ] **Step 1.2: Run tests — verify they fail**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_slash_aliases.py -v
```
Expected: FAIL — `AttributeError: type object 'BtwCommand' has no attribute 'aliases'` (or similar).

- [ ] **Step 1.3: Add `aliases` to SlashCommand ABC**

In `plugin_sdk/slash_command.py`, modify the `SlashCommand` class. Find the existing class (around line 54) and add the field after `description`:

```python
class SlashCommand(ABC):
    """Base class for plugin-authored slash commands."""

    #: The leading-slash name the user types. E.g. ``"plan"`` for ``/plan``.
    #: No leading slash. Alphanumeric + hyphen.
    name: str = ""

    #: One-line description shown in ``/help`` listings.
    description: str = ""

    #: Optional alternative names that resolve to the same command.
    #: Each alias must obey the same shape rules as ``name``.
    #: Defaults to empty tuple — backwards compatible.
    aliases: tuple[str, ...] = ()

    @abstractmethod
    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        """Run the command. ``args`` is everything after ``/<name>``.

        Must not raise. On failure return a SlashCommandResult with
        output describing the error + handled=True.
        """
```

(Keep the rest of the file unchanged.)

- [ ] **Step 1.4: Set `aliases = ("side",)` on BtwCommand**

In `opencomputer/agent/slash_commands_impl/btw_cmd.py`, find `class BtwCommand(SlashCommand):` (around line 81). Add `aliases` right after `description`:

```python
class BtwCommand(SlashCommand):
    name = "btw"
    description = (
        "Ask an ephemeral side-question using session context — "
        "no tools, not persisted"
    )
    aliases = ("side",)

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        ...  # unchanged
```

- [ ] **Step 1.5: Update `register_builtin_slash_commands` to register aliases**

In `opencomputer/agent/slash_commands.py`, modify `register_builtin_slash_commands` (around line 122). The current loop only registers the primary `name`; extend it to also register each alias under the SAME instance, but only when no command already owns that alias name (idempotent + alias-yields-to-primary):

```python
def register_builtin_slash_commands() -> None:
    """Register every built-in slash command into the shared registry.

    Idempotent — if a name is already present (e.g. another import
    already registered it, or a plugin registered the same name first)
    we leave the existing entry alone. Aliases follow the same yield
    rule: an alias does NOT overwrite an existing primary registration.
    """
    for cls in _BUILTIN_COMMANDS:
        cmd = cls()
        name = getattr(cmd, "name", None)
        if not name:
            continue
        if name in _plugin_registry.slash_commands:
            # Re-use the already-registered instance for alias mapping
            cmd = _plugin_registry.slash_commands[name]
        else:
            _plugin_registry.slash_commands[name] = cmd

        # Now register aliases under the SAME instance (yields to existing)
        for alias in getattr(cmd, "aliases", ()):
            if alias and alias not in _plugin_registry.slash_commands:
                _plugin_registry.slash_commands[alias] = cmd
```

- [ ] **Step 1.6: Run tests — verify pass**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_slash_aliases.py -v
```
Expected: 4 passed.

- [ ] **Step 1.7: Verify no regression on existing slash tests**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_slash_mru.py tests/test_slash_picker_source.py tests/test_slash_compress.py -q --no-header
```
Expected: all pass.

- [ ] **Step 1.8: Lint**

```bash
ruff check plugin_sdk/slash_command.py opencomputer/agent/slash_commands.py opencomputer/agent/slash_commands_impl/btw_cmd.py tests/test_slash_aliases.py --fix
ruff check plugin_sdk/slash_command.py opencomputer/agent/slash_commands.py opencomputer/agent/slash_commands_impl/btw_cmd.py tests/test_slash_aliases.py
```
Expected: `All checks passed!`

- [ ] **Step 1.9: Commit**

```bash
git add plugin_sdk/slash_command.py \
        opencomputer/agent/slash_commands.py \
        opencomputer/agent/slash_commands_impl/btw_cmd.py \
        tests/test_slash_aliases.py
git commit -m "$(cat <<'EOF'
feat(slash): add /side alias for /btw via SlashCommand.aliases field

Mirrors openclaw's /side text+native alias for /btw side questions.
Adds an optional `aliases: tuple[str, ...]` field to the SlashCommand
ABC (defaults to empty tuple — backwards compatible). The built-in
registration loop now also inserts the alias key into the shared
slash_commands dict, pointing at the same instance — so dispatcher
finds /side via natural dict.get() lookup, no dispatcher change.

Aliases yield to existing primary registrations — a plugin that
already owns /side keeps it.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Tool denylist with factory short-circuit

**Files:**
- Modify: `opencomputer/agent/config.py` (add `tools.deny`)
- Modify: `opencomputer/tools/registry.py` (denylist + short-circuit)
- Create: `tests/test_tool_denylist.py`

**Reference (read first):**
```bash
sed -n '1,50p' opencomputer/tools/registry.py
grep -n "class .*Config\|tools:" opencomputer/agent/config.py | head -20
```

- [ ] **Step 2.1: Write failing test**

Create `tests/test_tool_denylist.py`:

```python
"""Tests for the ToolRegistry denylist short-circuit."""

from __future__ import annotations

import pytest

from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class _StubTool(BaseTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="StubTool",
            description="x",
            input_schema={"type": "object", "properties": {}},
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.call_id, content="ok")


def _fresh_registry() -> ToolRegistry:
    """Build a clean registry instance for isolation per test."""
    return ToolRegistry()


def test_register_normal_tool_succeeds():
    r = _fresh_registry()
    r.register(_StubTool())
    assert r.get("StubTool") is not None


def test_denied_tool_is_skipped_at_registration():
    r = _fresh_registry()
    r.set_denylist(["StubTool"])
    r.register(_StubTool())
    assert r.get("StubTool") is None


def test_is_denied_helper():
    r = _fresh_registry()
    r.set_denylist(["StubTool", "OtherTool"])
    assert r.is_denied("StubTool") is True
    assert r.is_denied("OtherTool") is True
    assert r.is_denied("NotDenied") is False


def test_denylist_is_case_sensitive():
    """openclaw convention: exact-name match. 'stubtool' != 'StubTool'."""
    r = _fresh_registry()
    r.set_denylist(["stubtool"])
    r.register(_StubTool())
    assert r.get("StubTool") is not None  # still registered


def test_denylist_clear_resets():
    r = _fresh_registry()
    r.set_denylist(["StubTool"])
    assert r.is_denied("StubTool") is True
    r.set_denylist([])
    assert r.is_denied("StubTool") is False


def test_register_after_deny_then_clear_is_idempotent():
    """Re-registering after clearing the denylist works (no stale state)."""
    r = _fresh_registry()
    r.set_denylist(["StubTool"])
    r.register(_StubTool())  # silently skipped
    r.set_denylist([])
    r.register(_StubTool())  # now succeeds
    assert r.get("StubTool") is not None
```

- [ ] **Step 2.2: Run tests — verify fail**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_tool_denylist.py -v
```
Expected: FAIL — `set_denylist` / `is_denied` don't exist on ToolRegistry.

- [ ] **Step 2.3: Add denylist to ToolRegistry**

In `opencomputer/tools/registry.py`, find `class ToolRegistry:` (line 21) and modify:

```python
class ToolRegistry:
    """Singleton registry. Import as `from opencomputer.tools.registry import registry`."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._denylist: set[str] = set()

    def set_denylist(self, names: list[str]) -> None:
        """Replace the current denylist. Pass `[]` to clear.

        Mirrors openclaw's `tools.deny` config. Tools whose
        `schema.name` is in the denylist are silently skipped at
        :meth:`register` time. Callers with expensive optional-tool
        factories should call :meth:`is_denied` BEFORE constructing
        the tool to short-circuit factory work.
        """
        self._denylist = set(names)

    def is_denied(self, name: str) -> bool:
        """True if the named tool would be skipped at :meth:`register` time."""
        return name in self._denylist

    def register(self, tool: BaseTool) -> None:
        name = tool.schema.name
        if name in self._denylist:
            logger.debug("Tool %r skipped: in denylist", name)
            return  # silent skip — caller can check is_denied() first
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")
        self._tools[name] = tool

    # ... rest of methods unchanged
```

- [ ] **Step 2.4: Add `tools.deny` to config**

In `opencomputer/agent/config.py`, find the existing `ToolsConfig` (or whatever the agent.tools dataclass is called) and add the `deny` field. If no `ToolsConfig` exists, find the parent `AgentConfig` and add the `tools.deny` shape there.

```python
# Search the file for an existing tools-related dataclass first.
# If `class ToolsConfig:` exists, add:
#     deny: list[str] = field(default_factory=list)
# If no ToolsConfig: create one and reference from AgentConfig.

# Example minimal addition:
@dataclass(slots=True)
class ToolsConfig:
    """Top-level config for tool registration."""
    deny: list[str] = field(default_factory=list)
    """Tool names (exact, case-sensitive) that should be skipped at
    registration time. Mirrors openclaw's tools.deny."""
```

Then in the parent agent config (likely `AgentConfig`), add:
```python
tools: ToolsConfig = field(default_factory=ToolsConfig)
```

If a config-load callsite already wires registry initialization, also wire the denylist:

```python
# In CLI startup (cli.py or wherever bulk tool registration happens):
from opencomputer.tools.registry import registry
registry.set_denylist(config.agent.tools.deny if hasattr(config, "agent") else [])
# ... existing registry.register(...) calls follow ...
```

(Find the actual integration site by grepping for `registry.register(` in opencomputer/.)

- [ ] **Step 2.5: Run tests — verify pass**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_tool_denylist.py -v
```
Expected: 6 passed.

- [ ] **Step 2.6: Verify no regression on existing tool tests**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest -k "tool" -q --no-header 2>&1 | tail -10
```
Expected: pass (or only pre-existing failures unrelated to denylist).

- [ ] **Step 2.7: Lint**

```bash
ruff check opencomputer/agent/config.py opencomputer/tools/registry.py tests/test_tool_denylist.py --fix
ruff check opencomputer/agent/config.py opencomputer/tools/registry.py tests/test_tool_denylist.py
```

- [ ] **Step 2.8: Commit**

```bash
git add opencomputer/agent/config.py opencomputer/tools/registry.py tests/test_tool_denylist.py
git commit -m "$(cat <<'EOF'
feat(tools): add agent.tools.deny config + factory short-circuit

Mirrors openclaw's tools.deny pattern. Tools listed in
agent.tools.deny are silently skipped at ToolRegistry.register()
time. Callers with expensive optional-tool factories can call
ToolRegistry.is_denied(name) BEFORE construction to skip the
factory work entirely.

Out of scope for this cut: wildcard patterns, group:fs etc.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Per-plugin hook `timeout_ms` with fail-open semantics

**Files:**
- Modify: `plugin_sdk/hooks.py` (add `timeout_ms` field to HookSpec)
- Modify: `opencomputer/hooks/engine.py` (wrap handler in `asyncio.wait_for`)
- Create: `tests/test_hook_timeout.py`

**Reference (read first):**
```bash
sed -n '109,125p' plugin_sdk/hooks.py
sed -n '1,110p' opencomputer/hooks/engine.py
```

- [ ] **Step 3.1: Write failing test**

Create `tests/test_hook_timeout.py`:

```python
"""Tests for per-hook timeout_ms field."""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.hooks.engine import HookEngine
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec


@pytest.fixture
def engine():
    return HookEngine()


@pytest.fixture
def ctx():
    return HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="bash")


@pytest.mark.asyncio
async def test_hook_with_no_timeout_runs_to_completion(engine, ctx):
    called: list[bool] = []

    async def slow(c: HookContext) -> HookDecision:
        await asyncio.sleep(0.05)
        called.append(True)
        return HookDecision.pass_()

    engine.register(HookSpec(event=HookEvent.PRE_TOOL_USE, handler=slow))
    decision = await engine.fire_blocking(ctx)
    assert called == [True]
    assert decision is None  # all-pass → None


@pytest.mark.asyncio
async def test_hook_timeout_fails_open(engine, ctx):
    """Hook that exceeds timeout_ms is treated as 'pass' with a warning."""
    called: list[bool] = []

    async def slow(c: HookContext) -> HookDecision:
        await asyncio.sleep(2.0)  # way over timeout
        called.append(True)
        return HookDecision.block(reason="should not reach")

    engine.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, handler=slow, timeout_ms=50)
    )
    decision = await engine.fire_blocking(ctx)
    assert decision is None  # fail-open → no block
    assert called == []  # the slow hook was cancelled


@pytest.mark.asyncio
async def test_hook_timeout_zero_treated_as_no_timeout(engine, ctx):
    """timeout_ms=0 must NOT raise immediately — treat as None."""
    called: list[bool] = []

    async def fast(c: HookContext) -> HookDecision:
        called.append(True)
        return HookDecision.pass_()

    engine.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, handler=fast, timeout_ms=0)
    )
    decision = await engine.fire_blocking(ctx)
    assert called == [True]
    assert decision is None


@pytest.mark.asyncio
async def test_hook_timeout_does_not_affect_other_hooks(engine, ctx):
    """Slow hook's timeout doesn't stop subsequent hooks from running."""
    order: list[str] = []

    async def slow(c: HookContext) -> HookDecision:
        await asyncio.sleep(2.0)
        order.append("slow")
        return HookDecision.pass_()

    async def fast(c: HookContext) -> HookDecision:
        order.append("fast")
        return HookDecision.pass_()

    engine.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, handler=slow, timeout_ms=50, priority=50)
    )
    engine.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, handler=fast, priority=200)
    )
    await engine.fire_blocking(ctx)
    # slow timed out; fast still ran (subsequent priority bucket)
    assert "fast" in order
```

- [ ] **Step 3.2: Run tests — verify fail**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_hook_timeout.py -v
```
Expected: FAIL — `HookSpec` doesn't accept `timeout_ms`.

- [ ] **Step 3.3: Add `timeout_ms` to HookSpec**

In `plugin_sdk/hooks.py`, find `class HookSpec` (around line 110). Add the field at the end of the dataclass body (before the closing of the class):

```python
@dataclass(frozen=True, slots=True)
class HookSpec:
    """What plugins register — one event + one handler + an optional matcher."""

    event: HookEvent
    handler: HookHandler
    matcher: str | None = None  # regex over tool names for PreToolUse/PostToolUse
    fire_and_forget: bool = True  # true for post-action hooks
    #: Round 2A P-1: lower priority runs first; FIFO within the same bucket.
    priority: int = 100
    #: Per-hook timeout in milliseconds. None or 0 = no timeout (current behaviour).
    #: When the handler exceeds this, the engine logs a warning and treats it
    #: as 'pass' (fail-open), matching OC's existing hook contract
    #: (CLAUDE.md §7: a wedged hook must never wedge the loop).
    timeout_ms: int | None = None
```

- [ ] **Step 3.4: Wrap handler in `asyncio.wait_for` in HookEngine**

In `opencomputer/hooks/engine.py`, find `async def fire_blocking` (around line 72). Wrap the handler invocation:

```python
async def fire_blocking(self, ctx: HookContext) -> HookDecision | None:
    for spec in self._ordered_specs(ctx.event):
        if not self._matches(spec, ctx):
            continue
        try:
            if spec.timeout_ms and spec.timeout_ms > 0:
                decision = await asyncio.wait_for(
                    spec.handler(ctx),
                    timeout=spec.timeout_ms / 1000.0,
                )
            else:
                decision = await spec.handler(ctx)
        except asyncio.TimeoutError:
            self._log.warning(
                "Hook %s timed out after %dms — failing open (pass)",
                getattr(spec.handler, "__qualname__", repr(spec.handler)),
                spec.timeout_ms,
            )
            continue  # fail-open
        except Exception as e:  # noqa: BLE001
            self._log.warning(
                "Hook %s raised %r — failing open",
                getattr(spec.handler, "__qualname__", repr(spec.handler)),
                e,
            )
            continue
        if decision is None or getattr(decision, "decision", None) == "pass":
            continue
        return decision  # first non-pass wins
    return None
```

(Add `import asyncio` at the top of the file if not already present. The `self._log` reference assumes the engine has a logger — if not, use `logging.getLogger(__name__)`.)

- [ ] **Step 3.5: Run tests — verify pass**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_hook_timeout.py -v
```
Expected: 4 passed.

- [ ] **Step 3.6: Verify no regression on existing hook tests**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest -k "hook" -q --no-header 2>&1 | tail -10
```
Expected: all pre-existing tests pass.

- [ ] **Step 3.7: Lint**

```bash
ruff check plugin_sdk/hooks.py opencomputer/hooks/engine.py tests/test_hook_timeout.py --fix
ruff check plugin_sdk/hooks.py opencomputer/hooks/engine.py tests/test_hook_timeout.py
```

- [ ] **Step 3.8: Commit**

```bash
git add plugin_sdk/hooks.py opencomputer/hooks/engine.py tests/test_hook_timeout.py
git commit -m "$(cat <<'EOF'
feat(hooks): per-hook timeout_ms field with fail-open semantics

Mirrors openclaw's plugins.entries.<id>.hooks.timeoutMs. Adds
optional timeout_ms field to HookSpec; HookEngine.fire_blocking()
wraps the handler in asyncio.wait_for when set. On timeout: log
warning, treat as 'pass' (fail-open) — matches OC's existing hook
contract (CLAUDE.md §7: a wedged hook must never wedge the loop).

timeout_ms=0 treated as None to avoid the wait_for(t, 0) gotcha.

Plugin-config wiring (plugins.<id>.hooks.timeout_ms) deferred to
a follow-up; the SDK + engine surface is the load-bearing change.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Cerebras provider extension

**Files:**
- Create: `extensions/cerebras-provider/plugin.json`
- Create: `extensions/cerebras-provider/plugin.py`
- Create: `extensions/cerebras-provider/provider.py`
- Create: `tests/test_cerebras_provider.py`

**Reference (read first — use openai-provider as template):**
```bash
ls extensions/openai-provider/
cat extensions/openai-provider/plugin.py
head -80 extensions/openai-provider/provider.py
sed -n '300,400p' plugin_sdk/provider_contract.py
```

- [ ] **Step 4.1: Write failing test**

Create `tests/test_cerebras_provider.py`:

```python
"""Tests for the Cerebras provider extension."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_provider_module():
    """Load extensions/cerebras-provider/provider.py without triggering plugin loader."""
    spec_path = (
        Path(__file__).parent.parent / "extensions" / "cerebras-provider" / "provider.py"
    )
    spec = importlib.util.spec_from_file_location(
        "cerebras_provider_test_module", spec_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cerebras_provider_test_module"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cerebras_provider_module_exists():
    mod = _load_provider_module()
    assert hasattr(mod, "CerebrasProvider")


def test_cerebras_provider_default_base_url():
    mod = _load_provider_module()
    assert mod.CEREBRAS_BASE_URL == "https://api.cerebras.ai/v1"


def test_cerebras_provider_reads_api_key_from_env(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-key-123")
    p = mod.CerebrasProvider()
    assert p._api_key() == "test-key-123"


def test_cerebras_provider_raises_without_api_key(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    p = mod.CerebrasProvider()
    with pytest.raises(RuntimeError, match="CEREBRAS_API_KEY"):
        p._api_key()


def test_cerebras_provider_default_models():
    mod = _load_provider_module()
    assert "llama-3.3-70b" in mod.DEFAULT_MODELS
    assert "qwen-3-32b" in mod.DEFAULT_MODELS


@pytest.mark.asyncio
async def test_cerebras_complete_calls_correct_endpoint(monkeypatch):
    """Verify the provider hits api.cerebras.ai/v1/chat/completions."""
    mod = _load_provider_module()
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-key")

    captured: dict = {}

    class _MockResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

        def raise_for_status(self):
            pass

    class _MockClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            captured["url"] = url
            captured["headers"] = kw.get("headers", {})
            captured["json"] = kw.get("json", {})
            return _MockResponse()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _MockClient)

    from plugin_sdk.core import Message

    p = mod.CerebrasProvider()
    resp = await p.complete(
        model="llama-3.3-70b",
        messages=[Message(role="user", content="hello")],
        max_tokens=10,
    )
    assert "api.cerebras.ai/v1/chat/completions" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == "llama-3.3-70b"
    assert resp.message.content == "hi"
```

- [ ] **Step 4.2: Run tests — verify fail**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_cerebras_provider.py -v
```
Expected: FAIL — file not found.

- [ ] **Step 4.3: Create the manifest**

Create `extensions/cerebras-provider/plugin.json`:

```json
{
  "name": "cerebras-provider",
  "version": "0.1.0",
  "kind": "provider",
  "description": "Cerebras Inference — fast OpenAI-compatible inference for Llama, Qwen, GPT-OSS",
  "entry": "plugin.py",
  "min_host_version": "0.1.0",
  "setup": {
    "providers": [
      {
        "id": "cerebras",
        "auth_methods": ["api_key"],
        "auth_env": "CEREBRAS_API_KEY",
        "docs_url": "https://cloud.cerebras.ai/"
      }
    ]
  }
}
```

- [ ] **Step 4.4: Create the entry**

Create `extensions/cerebras-provider/plugin.py` (mirrors openai-provider's dual-import pattern):

```python
"""Cerebras Inference provider plugin — entry point.

Flat layout: plugin.py is the entry, sibling provider.py is importable
via plain name because the plugin loader puts the plugin root on sys.path.
"""

from __future__ import annotations

try:
    from provider import CerebrasProvider  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.cerebras_provider.provider import CerebrasProvider  # package mode


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("cerebras", CerebrasProvider)
```

- [ ] **Step 4.5: Create the provider**

Create `extensions/cerebras-provider/provider.py`:

```python
"""Cerebras Inference provider — OpenAI-compatible HTTP API.

Targets https://api.cerebras.ai/v1. Auth: Bearer $CEREBRAS_API_KEY.
"""

from __future__ import annotations

import json as _json
import os
from typing import Any, AsyncIterator

import httpx

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)
from plugin_sdk.tool_contract import ToolSchema

CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
DEFAULT_MODELS: tuple[str, ...] = (
    "llama-3.3-70b",
    "llama3.1-8b",
    "qwen-3-32b",
    "gpt-oss-120b",
)
DEFAULT_TIMEOUT_S = 60.0


class CerebrasProvider(BaseProvider):
    """OpenAI-compatible client targeting Cerebras Inference."""

    name = "cerebras"
    default_model = DEFAULT_MODELS[0]

    def __init__(self, base_url: str | None = None, **_: Any) -> None:
        self.base_url = base_url or CEREBRAS_BASE_URL

    def _api_key(self) -> str:
        key = os.environ.get("CEREBRAS_API_KEY")
        if not key:
            raise RuntimeError(
                "CEREBRAS_API_KEY environment variable is required for the Cerebras provider"
            )
        return key

    def _msg_to_dict(self, m: Message) -> dict:
        # Cerebras follows OpenAI's chat shape.
        return {"role": m.role, "content": m.content if isinstance(m.content, str) else ""}

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,  # noqa: ARG002 — tools not yet wired
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,  # noqa: ARG002
        runtime_extras: dict | None = None,  # noqa: ARG002
        response_schema: Any | None = None,  # noqa: ARG002
        site: str = "agent_loop",  # noqa: ARG002
    ) -> ProviderResponse:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(self._msg_to_dict(m) for m in messages)
        body = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key()}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]["message"]
        usage_in = data.get("usage", {})
        return ProviderResponse(
            message=Message(role=choice["role"], content=choice["content"]),
            usage=Usage(
                input_tokens=usage_in.get("prompt_tokens", 0),
                output_tokens=usage_in.get("completion_tokens", 0),
            ),
            model=model,
        )

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,  # noqa: ARG002
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,  # noqa: ARG002
        response_schema: Any | None = None,  # noqa: ARG002
        site: str = "agent_loop",  # noqa: ARG002
    ) -> AsyncIterator[StreamEvent]:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(self._msg_to_dict(m) for m in messages)
        body = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key()}",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        yield StreamEvent(kind="done")
                        break
                    try:
                        chunk = _json.loads(payload)
                    except _json.JSONDecodeError:
                        continue
                    delta = chunk["choices"][0].get("delta", {})
                    if delta.get("content"):
                        yield StreamEvent(kind="text_delta", text=delta["content"])
```

- [ ] **Step 4.6: Run tests — verify pass**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_cerebras_provider.py -v
```
Expected: 6 passed.

- [ ] **Step 4.7: Lint**

```bash
ruff check extensions/cerebras-provider/ tests/test_cerebras_provider.py --fix
ruff check extensions/cerebras-provider/ tests/test_cerebras_provider.py
```

- [ ] **Step 4.8: Commit**

```bash
git add extensions/cerebras-provider/ tests/test_cerebras_provider.py
git commit -m "$(cat <<'EOF'
feat(provider): add Cerebras Inference extension

OpenAI-compatible HTTP client targeting api.cerebras.ai/v1.
Default models: llama-3.3-70b, llama3.1-8b, qwen-3-32b, gpt-oss-120b.
Auth via CEREBRAS_API_KEY env var.

Brings OC's provider count from 32 → 33.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: DeepInfra provider extension

Mirror Task 4 with these substitutions:
- Class: `DeepInfraProvider`
- Constant: `DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"`
- Env var: `DEEPINFRA_API_KEY`
- Default models: `("meta-llama/Meta-Llama-3.3-70B-Instruct", "Qwen/Qwen3-235B-A22B", "deepseek-ai/DeepSeek-V3")`
- Provider id: `"deepinfra"`

**Files:** mirror Cerebras (manifest + plugin.py + provider.py + test).

- [ ] **Step 5.1: Test stub** — copy `tests/test_cerebras_provider.py` → `tests/test_deepinfra_provider.py`, swap `cerebras` → `deepinfra`, swap URLs/keys/models per substitutions above.
- [ ] **Step 5.2: Run failing test**
- [ ] **Step 5.3: Create `extensions/deepinfra-provider/plugin.json`** (mirror Cerebras manifest)
- [ ] **Step 5.4: Create `extensions/deepinfra-provider/plugin.py`** (mirror Cerebras entry)
- [ ] **Step 5.5: Create `extensions/deepinfra-provider/provider.py`** (mirror Cerebras provider, swap constants)
- [ ] **Step 5.6: Run test → pass**
- [ ] **Step 5.7: Lint**
- [ ] **Step 5.8: Commit**

```bash
git add extensions/deepinfra-provider/ tests/test_deepinfra_provider.py
git commit -m "$(cat <<'EOF'
feat(provider): add DeepInfra extension

OpenAI-compatible HTTP client targeting api.deepinfra.com/v1/openai.
Default models: Llama-3.3-70B-Instruct, Qwen3-235B-A22B, DeepSeek-V3.
Auth via DEEPINFRA_API_KEY env var.

Brings OC's provider count from 33 → 34.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

(The full file contents are intentionally not duplicated here — the implementer copies Task 4's files and swaps the four constants. Each file is ~200 lines; copy-then-substitute is reliable. If you'd rather have full text, see Task 4 verbatim and replace `Cerebras → DeepInfra`, `CEREBRAS → DEEPINFRA`, URL, model list.)

---

## Task 6: Web readability mode for `web_fetch`

**Files:**
- Modify: `pyproject.toml` (add `readability-lxml`)
- Modify: `opencomputer/tools/web_fetch.py` (add `mode` param + readability branch)
- Create: `tests/test_web_fetch_readability.py`

- [ ] **Step 6.1: Install readability-lxml in venv (and add to pyproject)**

In `pyproject.toml`, find the dependencies array (search for `dependencies = [` under `[project]`) and add:
```toml
"readability-lxml>=0.8.1",
```

Then install:
```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/pip install "readability-lxml>=0.8.1"
```

- [ ] **Step 6.2: Write failing test**

Create `tests/test_web_fetch_readability.py`:

```python
"""Tests for web_fetch readability mode."""

from __future__ import annotations

import pytest

from opencomputer.tools.web_fetch import (
    WebFetchTool,
    _html_to_article,
    _is_likely_article_url,
)

ARTICLE_HTML = """
<!doctype html>
<html><head><title>The Big Article</title></head><body>
  <nav>HOME | ABOUT | CONTACT</nav>
  <header><h1>Site Header</h1></header>
  <main>
    <article>
      <h1>The Big Article</h1>
      <p>This is the actual content the user cares about. It is a long paragraph
      that contains the gist of the post. Readability should keep this. The
      paragraph contains many words to make sure the readability scorer treats
      it as the main content rather than incidental boilerplate. Lorem ipsum
      dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor
      incididunt ut labore et dolore magna aliqua.</p>
      <p>Another paragraph of substantive content. Lorem ipsum dolor sit amet,
      consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore
      et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation
      ullamco laboris nisi ut aliquip ex ea commodo consequat.</p>
    </article>
  </main>
  <footer>FOOTERSITEFOOTERSITE | Privacy | Terms</footer>
  <aside><a>Related: ...</a></aside>
</body></html>
"""


def test_readability_extracts_article_body():
    text = _html_to_article(ARTICLE_HTML)
    assert "actual content the user cares about" in text


def test_readability_strips_nav_and_footer():
    text = _html_to_article(ARTICLE_HTML)
    # Footer text uses a unique sentinel that wouldn't appear in the article body
    assert "FOOTERSITEFOOTERSITE" not in text


def test_readability_returns_empty_on_no_article():
    """If readability extraction yields nothing, return empty string (caller falls back)."""
    junk_html = "<html><body></body></html>"
    text = _html_to_article(junk_html)
    assert text == "" or len(text) < 50


def test_is_likely_article_url_news_domain():
    assert _is_likely_article_url("https://www.medium.com/p/abc123") is True
    assert _is_likely_article_url("https://blog.example.com/foo") is True
    assert _is_likely_article_url("https://example.com/article/123") is True
    assert _is_likely_article_url("https://example.com/posts/123") is True


def test_is_likely_article_url_non_article_domain():
    assert _is_likely_article_url("https://github.com/user/repo") is False
    assert _is_likely_article_url("https://api.example.com/v1/users") is False


def test_web_fetch_tool_schema_exposes_mode_param():
    tool = WebFetchTool()
    sch = tool.schema  # property, not method
    props = sch.input_schema.get("properties", {})
    assert "mode" in props
    assert props["mode"]["enum"] == ["auto", "full", "readability"]
```

- [ ] **Step 6.3: Run tests — verify fail**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_web_fetch_readability.py -v
```
Expected: FAIL — `_html_to_article` not defined / mode not in schema.

- [ ] **Step 6.4: Implement helpers in `web_fetch.py`**

In `opencomputer/tools/web_fetch.py`, add the two new helpers near the top (after the existing `_html_to_text` function):

```python
import re
from urllib.parse import urlparse

from readability import Document  # readability-lxml

#: URL patterns that suggest the page is an article (auto-mode trigger).
_ARTICLE_HOST_RE = re.compile(
    r"(?:medium\.com|substack\.com|^blog\.|/blog/|/article/|/articles/|/news/|/posts/|/post/|hackernews|techcrunch|wired)",
    re.IGNORECASE,
)


def _is_likely_article_url(url: str) -> bool:
    """Heuristic: does this URL look like a news article / blog post?"""
    parsed = urlparse(url)
    if _ARTICLE_HOST_RE.search(parsed.netloc):
        return True
    return bool(_ARTICLE_HOST_RE.search(parsed.path))


def _html_to_article(html: str) -> str:
    """Extract just the article body using Mozilla's Readability algorithm.

    Returns empty string if extraction fails or yields too little content
    (caller can fall back to full text).
    """
    try:
        doc = Document(html)
        article_html = doc.summary(html_partial=True) or ""
        if len(article_html) < 50:
            return ""
        return _html_to_text(article_html)
    except Exception:  # noqa: BLE001
        return ""
```

- [ ] **Step 6.5: Add `mode` to schema and dispatch**

Find `class WebFetchTool` schema definition in `opencomputer/tools/web_fetch.py`. Add `mode` to the input schema:

```python
@property
def schema(self) -> ToolSchema:
    return ToolSchema(
        name="WebFetch",
        description="Fetch a URL and return clean text content.",
        input_schema={
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "max_chars": {
                    "type": "integer",
                    "description": "Truncate output to N chars",
                    "default": DEFAULT_MAX_CHARS,
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "full", "readability"],
                    "default": "auto",
                    "description": (
                        "auto = readability for article URLs, full otherwise. "
                        "full = strip nav/script/style. "
                        "readability = extract article body only."
                    ),
                },
            },
        },
    )
```

In `WebFetchTool.execute`, branch on `mode`:

```python
async def execute(self, call: ToolCall) -> ToolResult:
    args = call.arguments
    url = args["url"]
    max_chars = int(args.get("max_chars", DEFAULT_MAX_CHARS))
    mode = args.get("mode", "auto")

    if not is_safe_url(url):
        return ToolResult(call_id=call.call_id, content=f"refused: unsafe URL {url}")

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_S,
        headers={"User-Agent": DEFAULT_USER_AGENT},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    if mode == "auto":
        mode = "readability" if _is_likely_article_url(url) else "full"

    if mode == "readability":
        text = _html_to_article(html)
        if not text:  # fall back when readability returns nothing
            text = _html_to_text(html)
    else:  # full
        text = _html_to_text(html)

    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...truncated]"
    return ToolResult(call_id=call.call_id, content=text)
```

(Adapt to actual existing structure — the current execute may already do most of this; the new bits are the `mode` read and the readability branch.)

- [ ] **Step 6.6: Run tests — verify pass**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_web_fetch_readability.py -v
```
Expected: 6 passed.

- [ ] **Step 6.7: Verify no regression**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest -k "web_fetch" -q --no-header 2>&1 | tail -10
```
Expected: pass.

- [ ] **Step 6.8: Lint**

```bash
ruff check opencomputer/tools/web_fetch.py tests/test_web_fetch_readability.py --fix
ruff check opencomputer/tools/web_fetch.py tests/test_web_fetch_readability.py
```

- [ ] **Step 6.9: Commit**

```bash
git add pyproject.toml opencomputer/tools/web_fetch.py tests/test_web_fetch_readability.py
git commit -m "$(cat <<'EOF'
feat(tools): web_fetch readability mode (auto/full/readability)

Mirrors openclaw's web-readability extension capability. Adds an
optional `mode` parameter to WebFetch:
  - auto (default): readability for article-shaped URLs, full otherwise
  - full: existing behavior (strip nav/script/style)
  - readability: Mozilla Readability article-body extraction

Auto-mode heuristic matches medium.com, substack.com, /blog/, /article/,
/news/, /posts/. Falls back to full mode if readability returns empty.

New dep: readability-lxml>=0.8.1 (pure-Python wrapper around lxml,
~25KB; pulls lxml as transitive dep).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Final: full-suite + push + PR

- [ ] **F.1: Run the full test suite**

```bash
cd /Users/saksham/Vscode/claude/.worktrees/openclaw-since-april23/OpenComputer
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/ -x --tb=short 2>&1 | tail -25
```
Expected: all pass. If failures: read the trace, fix the root cause, re-run.

- [ ] **F.2: Final lint sweep**

```bash
ruff check opencomputer/ plugin_sdk/ tests/ extensions/cerebras-provider/ extensions/deepinfra-provider/ --select I --fix
ruff check opencomputer/ plugin_sdk/ tests/ extensions/cerebras-provider/ extensions/deepinfra-provider/
```
Expected: `All checks passed!`

- [ ] **F.3: Push the branch**

```bash
git push -u origin feat/openclaw-since-april23
```

- [ ] **F.4: Create the PR**

```bash
gh pr create --title "feat(openclaw-since-april23): 6 selective ports — alias, denylist, hooks, providers, readability" --body "$(cat <<'EOF'
## Summary

Six high-leverage openclaw improvements ported into OpenComputer (zero overlap with the in-flight Wave 5 import):

1. **`/side` alias for `/btw`** — `SlashCommand.aliases: tuple[str, ...]` field; built-in registration loop inserts alias keys into the shared dict.
2. **Tool denylist short-circuit** — `agent.tools.deny: list[str]` in config; `ToolRegistry.set_denylist()` + `is_denied()` for callers to short-circuit factory work.
3. **Per-plugin hook timeout** — `HookSpec.timeout_ms: int | None`; engine wraps handler in `asyncio.wait_for` with fail-open semantics matching CLAUDE.md §7.
4. **Cerebras provider** — `extensions/cerebras-provider/` (Llama, Qwen, GPT-OSS via api.cerebras.ai/v1).
5. **DeepInfra provider** — `extensions/deepinfra-provider/` (Llama-3.3-70B, Qwen3-235B, DeepSeek-V3 via api.deepinfra.com).
6. **Web readability mode** — `web_fetch(mode="auto"|"full"|"readability")` using Mozilla Readability (`readability-lxml`).

Spec: `docs/superpowers/specs/2026-05-04-openclaw-since-april23-import-design.md`
Plan: `docs/superpowers/plans/2026-05-04-openclaw-since-april23-import.md`

## Test plan
- [x] Per-task pytest passes
- [x] Full suite passes
- [x] ruff clean

## Deferred (with rationale in spec §6)
file-transfer (paired-node concept missing), tree-sitter shell explainer (no approval UI surface yet), streaming progress (large blast radius), diagnostics-prometheus (separate metrics endpoint design), document-extract, azure-speech, senseaudio, inworld, gradium, swabble, google-meet (each substantial standalone extension).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **F.5: Report PR URL**

Echo the PR URL returned by `gh pr create`.

---

## Self-Review (executed before plan approval)

### 1. Spec coverage
| Spec section | Plan task | Status |
|---|---|---|
| §3.1 `/side` alias | Task 1 | covered |
| §3.2 Tool denylist | Task 2 | covered |
| §3.3 Hook timeout | Task 3 | covered (per-plugin loader integration deferred — flagged in commit msg) |
| §3.4 Cerebras | Task 4 | covered |
| §3.5 DeepInfra | Task 5 | covered |
| §3.6 Web readability | Task 6 | covered |

### 2. Placeholder scan
- No "TBD" / "TODO" / "fill in details".
- Each step shows the exact code or the exact command.
- Task 5 references Task 4 by substitution rule rather than full duplication. This is a pragmatic exception (200-line code identical except for 4 constants) — implementer copies and edits four strings; faster than reading 200 lines twice.

### 3. Type consistency
- `SlashCommand.aliases: tuple[str, ...]` — used consistently across plugin_sdk, BtwCommand, registration loop, tests.
- `HookSpec.timeout_ms: int | None` — consistent across plugin_sdk and engine.
- `HookSpec.handler` (NOT `callback`) — corrected in tests vs original draft.
- `HookEvent.PRE_TOOL_USE` (SCREAMING_SNAKE) — corrected throughout.
- `tool.schema` is a property attribute access — corrected (was `tool.schema()` in original draft).
- `registry` singleton (instance-based) — corrected; tests use fresh `_fresh_registry()` instances rather than nonexistent `clear()`.

### 4. Pre-execution verification (added to plan top)
- All API surfaces verified by reading source. The "Pre-Execution Verification" section at the top of this plan documents the verified shapes so the implementer doesn't re-discover them.

### 5. Defensible? Yes.

Six commits, six self-contained changes. Each task ≤200 LOC. Total estimated time: 4-6 hours. Pre-flight verification means the implementer doesn't have to re-discover any API; everything they need is in the plan with the actual signatures.
