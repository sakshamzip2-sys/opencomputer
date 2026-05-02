"""AskUserQuestion tool — synchronously ask the user a question and wait
for an answer.

v1 design: works in CLI mode (blocks on stdin). In async-channel mode
(Telegram, Discord) returns an explicit error directing the agent to use
PushNotification + the user's next message instead. Phase 11e adds
proper pending-tool state to SessionDB so that route can land cleanly.

Source: claude-code's `AskUserQuestion`, hermes's `clarify_tool`.
"""

from __future__ import annotations

import sys

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.interaction import InteractionRequest
from plugin_sdk.tool_contract import BaseTool, ToolSchema


def _prompt_stdin(req: InteractionRequest) -> str:
    """Render the question on stderr (so it doesn't pollute stdout for piping)
    and read the answer from stdin. Numbered-options UX matches claude-code."""
    print(f"\n{req.question}", file=sys.stderr)
    if req.options:
        for i, opt in enumerate(req.options, 1):
            print(f"  {i}. {opt}", file=sys.stderr)
        print(f"  {len(req.options) + 1}. (other — type free-form text)", file=sys.stderr)
    sys.stderr.write("> ")
    sys.stderr.flush()
    return sys.stdin.readline().rstrip("\n")


class AskUserQuestionTool(BaseTool):
    # Not parallel-safe: blocks on user input.
    parallel_safe = False
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True

    def __init__(self, *, cli_mode: bool | None = None) -> None:
        # cli_mode auto-detects: if stdin is a TTY AND we're not in
        # headless mode, treat as CLI; else gateway. Tests pass cli_mode
        # explicitly to avoid this detection.
        if cli_mode is None:
            from opencomputer.headless import is_headless
            cli_mode = sys.stdin.isatty() and not is_headless()
        self._cli_mode = cli_mode

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="AskUserQuestion",
            description=(
                "Ask the user a question and BLOCK on their reply. Use this only when "
                "you genuinely need a decision and cannot proceed without it — e.g. an "
                "ambiguous spec, a destructive choice, missing credentials. Do NOT use "
                "this to confirm routine work, request approval for the obvious next "
                "step, or paraphrase what the user already said. Provide `options` for "
                "multiple-choice asks (cleaner UX); omit for free-form answers. CAUTION: "
                "in gateway mode (Telegram/Discord) this returns an error pointing you "
                "to PushNotification + waiting for the next inbound message; the synchronous "
                "blocking path only works in interactive CLI mode."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask.",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of suggested answers.",
                    },
                },
                "required": ["question"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        question = str(call.arguments.get("question", "")).strip()
        raw_opts = call.arguments.get("options") or []
        try:
            options = tuple(str(o) for o in raw_opts)
        except TypeError:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: options must be a list of strings",
                is_error=True,
            )

        if not question:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: question is required",
                is_error=True,
            )

        if not self._cli_mode:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Error: AskUserQuestion is not yet supported in async channels "
                    "(Telegram, Discord, etc.). Workaround: use PushNotification to "
                    "ask, then proceed when the user's next message arrives. Phase "
                    "11e will add proper pending-tool support."
                ),
                is_error=True,
            )

        req = InteractionRequest(
            question=question,
            options=options,
            presentation="choice" if options else "text",
        )

        try:
            answer = _prompt_stdin(req)
        except (EOFError, KeyboardInterrupt):
            return ToolResult(
                tool_call_id=call.id,
                content="Error: user cancelled (EOF or Ctrl-C)",
                is_error=True,
            )

        # If the user typed a number that maps to an option, expand it.
        if options and answer.strip().isdigit():
            idx = int(answer.strip()) - 1
            if 0 <= idx < len(options):
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"User chose option {idx + 1}: {options[idx]}",
                )
        return ToolResult(
            tool_call_id=call.id,
            content=f"User answered: {answer}",
        )


__all__ = ["AskUserQuestionTool"]
