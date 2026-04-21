"""
The agent loop — THE while loop.

Kept intentionally small (target <500 lines). All the architectural ideas
we studied condense to this:
    1. user message arrives
    2. loop:
         call LLM with current messages + tool schemas
         if response has tool_calls:
             dispatch them in parallel (where safe), append results
             continue
         else:
             break — this is the final answer
    3. persist the conversation to SQLite
    4. return the final message
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from opencomputer.agent.compaction import CompactionEngine
from opencomputer.agent.config import Config
from opencomputer.agent.injection import engine as injection_engine
from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.prompt_builder import PromptBuilder
from opencomputer.agent.state import SessionDB
from opencomputer.agent.step import StepOutcome
from opencomputer.tools.registry import registry
from plugin_sdk.core import Message, StopReason, ToolCall
from plugin_sdk.injection import InjectionContext
from plugin_sdk.provider_contract import BaseProvider
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext


@dataclass(slots=True)
class ConversationResult:
    """What a full run_conversation call returns."""

    final_message: Message
    messages: list[Message]
    session_id: str
    iterations: int
    input_tokens: int
    output_tokens: int


class AgentLoop:
    """The single while-loop that runs the agent."""

    def __init__(
        self,
        provider: BaseProvider,
        config: Config,
        db: SessionDB | None = None,
        memory: MemoryManager | None = None,
        prompt_builder: PromptBuilder | None = None,
        compaction_disabled: bool = False,
    ) -> None:
        self.provider = provider
        self.config = config
        self.db = db or SessionDB(config.session.db_path)
        self.memory = memory or MemoryManager(
            config.memory.declarative_path, config.memory.skills_path
        )
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.compaction = CompactionEngine(
            provider=provider,
            model=config.model.model,
            disabled=compaction_disabled,
        )
        self._last_input_tokens = 0
        # Per-session frozen system prompt. Populated on the first turn of each
        # session so subsequent turns reuse the exact same prefix (→ prompt-cache
        # hits on turn 2+). Memory edits mid-session go to disk immediately but
        # do NOT mutate this snapshot. Compaction invalidates only the suffix,
        # never the system-prompt prefix. Source: hermes-agent
        # tools/memory_tool.py:_system_prompt_snapshot.
        self._prompt_snapshots: dict[str, str] = {}

    # ─── the loop ──────────────────────────────────────────────────

    async def run_conversation(
        self,
        user_message: str,
        session_id: str | None = None,
        system_override: str | None = None,
        runtime: RuntimeContext | None = None,
        stream_callback=None,
    ) -> ConversationResult:
        sid = session_id or str(uuid.uuid4())
        self._runtime = runtime or DEFAULT_RUNTIME_CONTEXT

        # If this is a fresh session, create it in the DB and seed history from disk.
        existing = self.db.get_session(sid) if session_id else None
        if existing is None:
            self.db.create_session(
                session_id=sid,
                platform="cli",
                model=self.config.model.model,
            )
            messages: list[Message] = []
        else:
            messages = self.db.get_messages(sid)

        # System prompt is frozen per session: built once on the first turn,
        # then reused verbatim so the prefix cache hits on turn 2+. Memory
        # edits during a session do NOT retrigger a rebuild — that's the
        # invariant that makes hermes's prompt_cache ~10× cheaper than
        # per-turn rebuilds.
        if system_override is not None:
            base_system = system_override
        else:
            snapshot = self._prompt_snapshots.get(sid)
            if snapshot is None:
                skills = self.memory.list_skills()
                snapshot = self.prompt_builder.build(skills=skills)
                self._prompt_snapshots[sid] = snapshot
            base_system = snapshot

        # Collect dynamic injections (plan_mode, yolo_mode, etc. from plugins)
        inj_ctx = InjectionContext(
            messages=tuple(messages),
            runtime=self._runtime,
            session_id=sid,
        )
        injected = injection_engine.compose(inj_ctx)
        system = base_system + ("\n\n" + injected if injected else "")

        # Append user message + persist
        user_msg = Message(role="user", content=user_message)
        messages.append(user_msg)
        self.db.append_message(sid, user_msg)

        total_input = 0
        total_output = 0
        iterations = 0

        for _iter in range(self.config.loop.max_iterations):
            iterations += 1

            # Compaction check — uses REAL measured tokens from prior turn.
            # First iteration (no prior measurement) skips the check.
            if self._last_input_tokens > 0:
                result = await self.compaction.maybe_run(
                    messages, self._last_input_tokens
                )
                if result.did_compact:
                    messages = result.messages
                    # Re-collect injections with the new message list
                    inj_ctx = InjectionContext(
                        messages=tuple(messages),
                        runtime=self._runtime,
                        session_id=sid,
                    )
                    injected = injection_engine.compose(inj_ctx)
                    system = base_system + ("\n\n" + injected if injected else "")

            step = await self._run_one_step(
                messages=messages, system=system, stream_callback=stream_callback
            )
            self._last_input_tokens = step.input_tokens
            total_input += step.input_tokens
            total_output += step.output_tokens

            if not step.should_continue:
                # No tool calls — safe to persist the assistant message alone.
                messages.append(step.assistant_message)
                self.db.append_message(sid, step.assistant_message)
                self.db.end_session(sid)
                return ConversationResult(
                    final_message=step.assistant_message,
                    messages=messages,
                    session_id=sid,
                    iterations=iterations,
                    input_tokens=total_input,
                    output_tokens=total_output,
                )

            # Push the current runtime to DelegateTool so subagents inherit it
            try:
                from opencomputer.tools.delegate import DelegateTool

                DelegateTool.set_runtime(self._runtime)
            except Exception:
                pass  # delegate tool may not be registered yet in some contexts

            # Dispatch tools BEFORE persisting the assistant message. If we saved
            # it first and then got cancelled mid-dispatch, the DB would hold a
            # tool_use with no matching tool_result — Anthropic 400s on resume.
            # Atomic batch persist below restores the invariant.
            tool_results = await self._dispatch_tool_calls(
                step.assistant_message.tool_calls or [],
                session_id=sid,
            )
            turn_messages: list[Message] = [step.assistant_message, *tool_results]
            messages.extend(turn_messages)
            self.db.append_messages_batch(sid, turn_messages)

        # Budget exhausted
        final = Message(
            role="assistant",
            content="[loop iteration budget exhausted — agent did not finish]",
        )
        messages.append(final)
        self.db.append_message(sid, final)
        self.db.end_session(sid)
        return ConversationResult(
            final_message=final,
            messages=messages,
            session_id=sid,
            iterations=iterations,
            input_tokens=total_input,
            output_tokens=total_output,
        )

    # ─── one step ──────────────────────────────────────────────────

    async def _run_one_step(
        self,
        *,
        messages: list[Message],
        system: str,
        stream_callback=None,
    ) -> StepOutcome:
        """One LLM call + classification of the result.

        If `stream_callback` is provided, stream_complete is used and each
        text chunk is passed to the callback synchronously.
        """
        if stream_callback is not None:
            final_response = None
            async for event in self.provider.stream_complete(
                model=self.config.model.model,
                messages=messages,
                system=system,
                tools=registry.schemas(),
                max_tokens=self.config.model.max_tokens,
                temperature=self.config.model.temperature,
            ):
                if event.kind == "text_delta":
                    stream_callback(event.text)
                elif event.kind == "done":
                    final_response = event.response
            if final_response is None:
                raise RuntimeError("stream ended without a 'done' event")
            resp = final_response
        else:
            resp = await self.provider.complete(
                model=self.config.model.model,
                messages=messages,
                system=system,
                tools=registry.schemas(),
                max_tokens=self.config.model.max_tokens,
                temperature=self.config.model.temperature,
            )

        stop_reason_map = {
            "end_turn": StopReason.END_TURN,
            "tool_use": StopReason.TOOL_USE,
            "max_tokens": StopReason.MAX_TOKENS,
            "stop_sequence": StopReason.END_TURN,
        }
        stop = stop_reason_map.get(resp.stop_reason, StopReason.END_TURN)

        # If the model called tools, even if the raw stop_reason was "end_turn",
        # we need to continue so the model can process results.
        if resp.message.tool_calls and stop == StopReason.END_TURN:
            stop = StopReason.TOOL_USE

        return StepOutcome(
            stop_reason=stop,
            assistant_message=resp.message,
            tool_calls_made=len(resp.message.tool_calls or []),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

    # ─── tool dispatch ─────────────────────────────────────────────

    async def _dispatch_tool_calls(
        self, calls: list[ToolCall], session_id: str = ""
    ) -> list[Message]:
        """Run all tool calls — in parallel where safe — and return result Messages.

        Fires PreToolUse hooks before each tool runs. If a hook blocks, the tool
        is skipped and an error ToolResult is synthesized. Runtime context flows
        to hooks so plan_mode_block etc. can read it.
        """
        if not calls:
            return []

        # Fire PreToolUse hooks first (blocking). Determine which calls are blocked.
        from opencomputer.hooks.engine import engine as hook_engine
        from plugin_sdk.core import ToolResult
        from plugin_sdk.hooks import HookContext, HookEvent

        blocked: dict[str, str] = {}  # call.id → block reason
        for c in calls:
            ctx = HookContext(
                event=HookEvent.PRE_TOOL_USE,
                session_id=session_id,
                tool_call=c,
                runtime=self._runtime,
            )
            decision = await hook_engine.fire_blocking(ctx)
            if decision is not None and decision.decision == "block":
                blocked[c.id] = decision.reason or "blocked by hook"

        async def _run_one(c: ToolCall):
            if c.id in blocked:
                return ToolResult(
                    tool_call_id=c.id,
                    content=f"[blocked by PreToolUse hook: {blocked[c.id]}]",
                    is_error=True,
                )
            return await registry.dispatch(c)

        if self.config.loop.parallel_tools and self._all_parallel_safe(calls):
            results = await asyncio.gather(*(_run_one(c) for c in calls))
        else:
            results = [await _run_one(c) for c in calls]

        return [
            Message(
                role="tool",
                content=r.content,
                tool_call_id=r.tool_call_id,
                name=next((c.name for c in calls if c.id == r.tool_call_id), None),
            )
            for r in results
        ]

    def _all_parallel_safe(self, calls: list[ToolCall]) -> bool:
        """Only parallelize when every tool in the batch declared parallel_safe."""
        for c in calls:
            tool = registry.get(c.name)
            if tool is None or not tool.parallel_safe:
                return False
        return True


__all__ = ["AgentLoop", "ConversationResult"]
