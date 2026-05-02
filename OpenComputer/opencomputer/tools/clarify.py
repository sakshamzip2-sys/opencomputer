"""ClarifyTool — agent-callable disambiguation prompt.

Sub-project 1.G of the openclaw-tier1 plan
(`docs/superpowers/plans/2026-04-28-openclaw-tier1-port.md`).

When the agent reads a request and finds 2-4 plausible interpretations, it
calls ``Clarify`` with the ambiguity description + the candidate options.
The user picks one; the chosen option flows back as the tool's result so
the agent can proceed without guessing.

This is intentionally a thin wrapper over `AskUserQuestionTool` — the
existing user-question machinery (stdin in CLI mode, error in async-channel
mode) is exactly what we need; we just constrain the *shape* of the ask:

- Required ``ambiguity`` text (one sentence framing the disagreement).
- Required ``options`` list of 2-4 concrete interpretations.

Use `AskUserQuestion` directly for free-form questions or destructive
confirmations; use `Clarify` when the question is "which of these did you
mean?".

Source: claude-code's `AskUserQuestion` shape narrowed to disambiguation;
hermes-agent's `clarify_tool` (same name, same intent).
"""

from __future__ import annotations

from opencomputer.tools.ask_user_question import AskUserQuestionTool
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

MIN_OPTIONS = 2
MAX_OPTIONS = 4


class ClarifyTool(BaseTool):
    """Ask the user to pick between 2-4 plausible interpretations.

    Reuses `AskUserQuestionTool` internally for the actual user-prompt
    machinery — same CLI-mode stdin path, same async-channel error path.
    Only the input schema is narrower (forces the agent to supply concrete
    options instead of asking open questions).
    """

    # Not parallel-safe: blocks on user input via the underlying
    # AskUserQuestion machinery.
    parallel_safe = False
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True

    def __init__(self, *, cli_mode: bool | None = None) -> None:
        # Compose rather than inherit — we wrap AskUserQuestionTool so any
        # future improvements to its routing (e.g. Phase 11e pending-tool
        # state for async channels) flow through without duplication.
        self._asker = AskUserQuestionTool(cli_mode=cli_mode)

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Clarify",
            description=(
                "When the user's request is genuinely ambiguous (multiple "
                "plausible interpretations), call this with a list of concrete "
                "options. The user picks one, and the tool returns the chosen "
                "option as the result. Do NOT call this when the answer is "
                "obvious — only when there are 2-4 plausible interpretations "
                "and you genuinely need user input to disambiguate. For "
                "free-form questions or destructive confirmations, use "
                "AskUserQuestion instead."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ambiguity": {
                        "type": "string",
                        "description": (
                            "One sentence describing what's ambiguous about "
                            "the request."
                        ),
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "2-4 concrete interpretations to choose from."
                        ),
                        "minItems": MIN_OPTIONS,
                        "maxItems": MAX_OPTIONS,
                    },
                },
                "required": ["ambiguity", "options"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        ambiguity = str(call.arguments.get("ambiguity", "")).strip()
        raw_opts = call.arguments.get("options") or []
        try:
            options = [str(o) for o in raw_opts]
        except TypeError:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: options must be a list of strings",
                is_error=True,
            )

        # ── Validate at the tool layer (the JSON-schema minItems/maxItems
        # is advisory for the model; we still defend against bad calls). ──
        if not ambiguity:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: ambiguity description is required",
                is_error=True,
            )
        if len(options) < MIN_OPTIONS or len(options) > MAX_OPTIONS:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: options must contain {MIN_OPTIONS}-{MAX_OPTIONS} "
                    f"items (got {len(options)}). If the request is not "
                    "actually ambiguous, don't call Clarify; if there are 5+ "
                    "plausible readings, narrow them down before asking."
                ),
                is_error=True,
            )

        # Delegate to AskUserQuestionTool, framing the ambiguity as the
        # question and the options as multiple-choice. This routes through
        # the same CLI-stdin / async-channel-error logic for free.
        inner = ToolCall(
            id=call.id,
            name="AskUserQuestion",
            arguments={"question": ambiguity, "options": options},
        )
        return await self._asker.execute(inner)


__all__ = ["MAX_OPTIONS", "MIN_OPTIONS", "ClarifyTool"]
