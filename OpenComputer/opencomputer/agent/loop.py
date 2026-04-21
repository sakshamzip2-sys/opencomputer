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

from opencomputer.agent.config import Config
from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.prompt_builder import PromptBuilder
from opencomputer.agent.state import SessionDB
from opencomputer.agent.step import StepOutcome
from opencomputer.tools.registry import registry
from plugin_sdk.core import Message, StopReason, ToolCall
from plugin_sdk.provider_contract import BaseProvider


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
    ) -> None:
        self.provider = provider
        self.config = config
        self.db = db or SessionDB(config.session.db_path)
        self.memory = memory or MemoryManager(
            config.memory.declarative_path, config.memory.skills_path
        )
        self.prompt_builder = prompt_builder or PromptBuilder()

    # ─── the loop ──────────────────────────────────────────────────

    async def run_conversation(
        self,
        user_message: str,
        session_id: str | None = None,
        system_override: str | None = None,
    ) -> ConversationResult:
        sid = session_id or str(uuid.uuid4())

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

        # Build system prompt fresh every turn (cheap, keeps skill list up to date)
        if system_override is not None:
            system = system_override
        else:
            skills = self.memory.list_skills()
            system = self.prompt_builder.build(skills=skills)

        # Append user message + persist
        user_msg = Message(role="user", content=user_message)
        messages.append(user_msg)
        self.db.append_message(sid, user_msg)

        total_input = 0
        total_output = 0
        iterations = 0

        for _iter in range(self.config.loop.max_iterations):
            iterations += 1
            step = await self._run_one_step(messages=messages, system=system)
            total_input += step.input_tokens
            total_output += step.output_tokens
            messages.append(step.assistant_message)
            self.db.append_message(sid, step.assistant_message)

            if not step.should_continue:
                self.db.end_session(sid)
                return ConversationResult(
                    final_message=step.assistant_message,
                    messages=messages,
                    session_id=sid,
                    iterations=iterations,
                    input_tokens=total_input,
                    output_tokens=total_output,
                )

            # Dispatch all tool calls from this step
            tool_results = await self._dispatch_tool_calls(
                step.assistant_message.tool_calls or []
            )
            for tr_msg in tool_results:
                messages.append(tr_msg)
                self.db.append_message(sid, tr_msg)

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
        self, *, messages: list[Message], system: str
    ) -> StepOutcome:
        """One LLM call + classification of the result."""
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

    async def _dispatch_tool_calls(self, calls: list[ToolCall]) -> list[Message]:
        """Run all tool calls — in parallel where safe — and return result Messages."""
        if not calls:
            return []
        if self.config.loop.parallel_tools and self._all_parallel_safe(calls):
            results = await asyncio.gather(
                *(registry.dispatch(c) for c in calls), return_exceptions=False
            )
        else:
            results = [await registry.dispatch(c) for c in calls]

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
