"""Cheap-model client for auxiliary LLM tasks (summarization, classification, ...).

Auxiliary tasks (compaction summary, title generation, intent classification,
fact extraction) don't need the user-facing model — they need a fast, cheap,
"good-enough" model. Hermes calls this an ``auxiliary_client``; we mirror
the contract so compaction (today) and title-gen (after archit-2's T6 lands)
can call ``auxiliary.complete_summary(text)`` instead of the hard-coded main
provider.

Rationale (from the user's audit):

> ``cheap_route.py`` exists (76 LOC) but only routes turn-0; compaction
> always uses the main provider. Hermes's auxiliary_client provides a
> multi-provider fallback chain for ALL auxiliary tasks.

This module ships the *contract* + a single-provider default. The
multi-provider fallback chain (OpenRouter → Nous → Anthropic → ...) is
a follow-up — implementing it cleanly requires the registry-based
provider lookup that came with Phase 3.x. Since the call sites that
need a router most (compaction's ``_summarize``) currently hard-code
``self.provider``, the first win is just teaching them to call through
``AuxiliaryClient`` so a follow-up PR can swap the resolution strategy
without touching every caller.

Scope of THIS PR:

- Define the public contract (``AuxiliaryClient`` with task-typed
  methods: ``complete_summary``, ``complete_classify``, ``complete_extract``).
- Provide a single-provider default that wraps the main provider with a
  cheap-model preference (defaults to Haiku 4.5 when calling Anthropic;
  configurable).
- Subsume the existing first-turn ``cheap_route`` decision into a
  ``cheap_for_first_turn`` static method so callers gradually migrate
  their model-selection logic into one place.

Out of scope (follow-ups, by design):

- Multi-provider fallback chain. Needs registry hookup that lives in
  ``opencomputer/plugins/registry.py``; touching that without a
  concrete second-provider use case is YAGNI.
- Title-gen wiring. Blocked on archit-2's ``feat/tier-s-port`` T6
  shipping ``opencomputer/agent/title_generator.py`` — once merged,
  a one-liner change to call ``auxiliary.complete_title``.
- Compaction wiring. Will land in a small follow-up after this PR
  merges so the diff stays reviewable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider

logger = logging.getLogger("opencomputer.agent.auxiliary_client")

#: Sensible defaults per auxiliary task. Tuned from the model_metadata
#: registry (Phase G.32) — Haiku 4.5 is cheap and fast for the kinds of
#: short, structured tasks auxiliary work consists of. Override via
#: :class:`AuxiliaryConfig`.
DEFAULT_MODEL_BY_TASK: dict[str, str] = {
    "summary": "claude-haiku-4-5",
    "classify": "claude-haiku-4-5",
    "extract": "claude-haiku-4-5",
    "title": "claude-haiku-4-5",
}

TaskKind = Literal["summary", "classify", "extract", "title"]


@dataclass(frozen=True, slots=True)
class AuxiliaryConfig:
    """Per-task model overrides. ``None`` falls back to ``DEFAULT_MODEL_BY_TASK``."""

    summary_model: str | None = None
    classify_model: str | None = None
    extract_model: str | None = None
    title_model: str | None = None
    #: Default temperature for auxiliary tasks. Conservative — these calls
    #: are deterministic in spirit (summarize, classify) so we don't want
    #: stylistic drift.
    temperature: float = 0.3


class AuxiliaryClient:
    """Single-provider auxiliary LLM client.

    Holds one ``BaseProvider`` instance and a per-task model map.
    Each ``complete_<task>`` method runs the task on the resolved
    cheap model with task-appropriate defaults.

    Failure handling: on any exception from the underlying provider,
    re-raises after logging. Callers are responsible for fallback
    (e.g. compaction's ``_truncate_fallback``). The multi-provider
    fallback chain is the documented follow-up — see module docstring.
    """

    def __init__(
        self,
        provider: BaseProvider,
        config: AuxiliaryConfig | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or AuxiliaryConfig()

    def model_for(self, task: TaskKind) -> str:
        """Resolve the model name for a task (config override → default)."""
        cfg = self.config
        override: str | None = None
        if task == "summary":
            override = cfg.summary_model
        elif task == "classify":
            override = cfg.classify_model
        elif task == "extract":
            override = cfg.extract_model
        elif task == "title":
            override = cfg.title_model
        return override or DEFAULT_MODEL_BY_TASK[task]

    async def _complete(
        self,
        *,
        task: TaskKind,
        messages: list[Message],
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        """Internal: run the provider with task-appropriate defaults."""
        model = self.model_for(task)
        temp = temperature if temperature is not None else self.config.temperature
        try:
            resp = await self.provider.complete(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temp,
            )
        except Exception:
            logger.exception("auxiliary task %r failed on model %r", task, model)
            raise
        return (resp.message.content or "").strip()

    # ─── task-typed entrypoints ─────────────────────────────────────

    async def complete_summary(
        self, text: str, *, max_tokens: int = 1024
    ) -> str:
        """Summarize ``text`` into ~300 words plain prose.

        Used by ``CompactionEngine._summarize`` (after the follow-up wires
        it). Behaviour matches the existing prompt — facts, decisions,
        commands run, file paths preserved.
        """
        msgs = [
            Message(
                role="user",
                content=(
                    f"{text}\n\n"
                    "Summarize the above conversation history tightly. "
                    "Keep facts, decisions, file paths, and any commands "
                    "run. Output plain prose, no markdown headers. Target "
                    "~300 words."
                ),
            )
        ]
        return await self._complete(task="summary", messages=msgs, max_tokens=max_tokens)

    async def complete_classify(
        self, prompt: str, *, max_tokens: int = 64
    ) -> str:
        """Classify ``prompt`` — caller embeds the candidate set in the prompt.

        Returns a short string. Caller is responsible for parsing it (a
        single-word verdict, a JSON literal, etc.). Aux model is fast
        but small — keep prompts deterministic and outputs short.
        """
        return await self._complete(
            task="classify",
            messages=[Message(role="user", content=prompt)],
            max_tokens=max_tokens,
            temperature=0.0,  # classification — no creativity wanted
        )

    async def complete_extract(
        self, prompt: str, *, max_tokens: int = 512
    ) -> str:
        """Extract structured facts from ``prompt``.

        Used by memory-extraction paths that pull motifs out of recent
        turns. The prompt is the caller's responsibility — usually a
        JSON-shaped extraction request.
        """
        return await self._complete(
            task="extract",
            messages=[Message(role="user", content=prompt)],
            max_tokens=max_tokens,
            temperature=0.0,
        )

    async def complete_title(
        self, conversation_excerpt: str, *, max_tokens: int = 32
    ) -> str:
        """Generate a session title.

        Hooks in once archit-2's T6 (``opencomputer/agent/title_generator.py``)
        lands. The prompt is the caller's responsibility; this method is
        the call surface they'll route through.
        """
        prompt = (
            "Title this conversation in <= 6 words. No quotes, no period, "
            "title case. Output the title only.\n\n"
            f"{conversation_excerpt}"
        )
        return await self._complete(
            task="title",
            messages=[Message(role="user", content=prompt)],
            max_tokens=max_tokens,
            temperature=0.4,  # a little creativity for titles
        )

    # ─── first-turn cheap-route helper ─────────────────────────────

    @staticmethod
    def cheap_for_first_turn(user_message: str) -> bool:
        """Re-export of the existing first-turn cheap-route heuristic.

        Subsumes the public surface from ``opencomputer.agent.cheap_route``
        so callers can ask one place — ``AuxiliaryClient`` — instead of
        knowing about a parallel module. The implementation lives in
        ``cheap_route.should_route_cheap`` (kept there to avoid breaking
        existing imports); this is just a forwarding alias.
        """
        from opencomputer.agent.cheap_route import should_route_cheap

        return should_route_cheap(user_message)


__all__ = [
    "AuxiliaryClient",
    "AuxiliaryConfig",
    "DEFAULT_MODEL_BY_TASK",
    "TaskKind",
]
