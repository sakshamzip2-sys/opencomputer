"""DynamicInjectionProvider that adds a ``<think>...</think>`` system
instruction when the active provider lacks native extended thinking.

Pairs with :class:`opencomputer.agent.thinking_parser.ThinkingTagsParser`,
which extracts the contents of those tags from the text stream and
routes them to the existing ``thinking_callback`` chain so the
StreamingRenderer + ReasoningStore (PR #382) pick them up unchanged.

Activates when:
    1. ``runtime.custom["reasoning_effort"]`` is anything other than
       ``"none"`` (default is ``"medium"`` per the reasoning_cmd
       contract — so unset === active).
    2. ``runtime.custom["_provider_supports_native_thinking"]`` is
       falsy. CLI wires this at session start by reading the active
       provider's :meth:`supports_native_thinking_for` for the
       configured model. If the wire isn't set (e.g. wire-protocol
       path), default to ``False`` so the fallback kicks in — safer
       than silently dropping thinking.
"""
from __future__ import annotations

from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext


_INSTRUCTION = """\
## Extended Thinking

Before producing your final response, write your private reasoning
inside `<think>...</think>` tags. The contents of these tags are
NEVER shown to the user directly — they are routed to a separate
"reasoning" panel that the user can expand on demand.

Rules:
1. Use the tags ONLY for your private chain-of-thought reasoning.
2. Place the `<think>` block(s) BEFORE your visible response, not
   inside code blocks or other formatting.
3. Always close `<think>` with `</think>`. Multiple separate blocks
   per turn are fine.
4. Keep visible output (outside the tags) clean and final — the user
   does not need to see meta-commentary about your reasoning.
5. NEVER use these tags inside code blocks, examples, or other
   contexts where `<think>` would be legitimate content. They are
   reserved exclusively for your private reasoning.
"""


class ThinkingInjector(DynamicInjectionProvider):
    """Injects the ``<think>``-tag instruction for non-native-thinking
    providers."""

    priority = 60  # plan=10, yolo=20, custom modes 50+; thinking 60

    @property
    def provider_id(self) -> str:
        return "thinking_tags_fallback"

    async def collect(self, ctx: InjectionContext) -> str | None:
        effort = str(
            ctx.runtime.custom.get("reasoning_effort", "medium")
        ).lower()
        if effort == "none":
            return None
        # Default to False (fallback active) when CLI hasn't wired the
        # capability flag — model-agnostic visibility wins on tie.
        native = bool(
            ctx.runtime.custom.get(
                "_provider_supports_native_thinking", False
            )
        )
        if native:
            return None
        return _INSTRUCTION


__all__ = ["ThinkingInjector"]
