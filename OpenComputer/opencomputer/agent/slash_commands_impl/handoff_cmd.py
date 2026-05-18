"""``/handoff <target-profile>`` — manual profile swap with handoff payload.

Triggers the SAME pipeline as the classifier-driven auto-swap: generate
a handoff per protocol v2.0 → write to target profile's inbox → queue
the pending profile swap. The new profile reads the handoff on its
first turn after the swap (via :class:`HandoffInjectionProvider`).

Usage:
    /handoff stocks            # swap to "stocks" profile with handoff
    /handoff                   # list available profiles
    /handoff --no-content stocks   # swap without generating a handoff (cheap)

The ``--no-content`` form is the fallback for users who explicitly want
the swap without burning an LLM call on the handoff — equivalent to the
existing ``/profile-swap`` behaviour but routed through the same
auditing path.
"""
from __future__ import annotations

import logging
from pathlib import Path

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_log = logging.getLogger("opencomputer.agent.slash_commands.handoff")


class HandoffCommand(SlashCommand):
    name = "handoff"
    description = (
        "Swap profile with a handoff document — `/handoff <target>` "
        "(use --no-content to skip the handoff)"
    )
    aliases: tuple[str, ...] = ("profile-handoff",)
    # A8 (gateway-vs-CLI parity) — the profile-rebind plumbing already
    # handles every cross-cutting state swap, so /handoff is safe to run
    # inline on Telegram/Discord. bypass_running_guard lets a swap be
    # requested even while a turn is in flight (the swap itself lands on
    # the next turn).
    gateway_safe = True
    bypass_running_guard = True

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        raw = (args or "").strip()
        if not raw:
            return self._render_listing()

        skip_handoff, target = _parse_args(raw)

        try:
            from opencomputer.profiles import list_profiles
            available = sorted(list_profiles())
        except Exception as e:  # noqa: BLE001 — defensive
            _log.warning("could not list profiles: %s", e)
            return SlashCommandResult(
                output=f"Could not list profiles: {e}",
                handled=True,
            )

        if target not in available and target != "default":
            return SlashCommandResult(
                output=(
                    f"Profile {target!r} not found. Available: "
                    f"{', '.join(['default', *available]) if available else 'default'}"
                ),
                handled=True,
            )

        current = (
            runtime.custom.get("active_profile_id") or "default"
        )
        if target == current:
            return SlashCommandResult(
                output=f"Already in profile {target!r} — nothing to swap.",
                handled=True,
            )

        # Resolve target profile home for the handoff write.
        try:
            from opencomputer.profiles import get_profile_dir
            target_home = _profile_home(target, get_profile_dir)
        except Exception as e:  # noqa: BLE001
            return SlashCommandResult(
                output=f"Could not resolve target profile home: {e}",
                handled=True,
            )

        if skip_handoff:
            return self._queue_swap_no_handoff(runtime, target)

        # Full path: generate + write + queue.
        try:
            handoff_path = await self._generate_and_write(
                runtime=runtime,
                source_profile=current,
                target_profile=target,
                target_home=target_home,
            )
        except _ManualHandoffError as e:
            return SlashCommandResult(
                output=f"Handoff generation failed: {e}. Swap aborted.",
                handled=True,
            )

        runtime.custom["pending_profile_id"] = target

        if handoff_path is None:
            note = (
                f"Swap queued to {target!r}. Handoff was not warranted "
                "(short / completed session). The new profile will "
                "start fresh."
            )
        else:
            note = (
                f"Swap queued to {target!r}. Handoff written to "
                f"{handoff_path.name}."
            )
        return SlashCommandResult(output=note, handled=True)

    # ─── helpers ─────────────────────────────────────────────────────

    def _render_listing(self) -> SlashCommandResult:
        try:
            from opencomputer.profiles import list_profiles
            names = sorted(list_profiles())
        except Exception as e:  # noqa: BLE001
            return SlashCommandResult(
                output=f"Could not list profiles: {e}",
                handled=True,
            )
        usage = (
            "Usage: /handoff <target-profile> [--no-content]\n"
            "       /handoff --no-content <target-profile>\n\n"
            "Available profiles:\n"
        )
        body = "\n".join(f"  - {n}" for n in ["default", *names]) or "  (none)"
        return SlashCommandResult(output=usage + body, handled=True)

    def _queue_swap_no_handoff(
        self, runtime: RuntimeContext, target: str,
    ) -> SlashCommandResult:
        runtime.custom["pending_profile_id"] = target
        return SlashCommandResult(
            output=f"Swap queued to {target!r} (no handoff generated).",
            handled=True,
        )

    async def _generate_and_write(
        self,
        *,
        runtime: RuntimeContext,
        source_profile: str,
        target_profile: str,
        target_home: Path,
    ) -> Path | None:
        """Run generator + write inbox; return path or None if not warranted.

        Raises :class:`_ManualHandoffError` for any unrecoverable failure
        (no provider in runtime, generation failed, inbox unwritable).
        """
        from opencomputer.agent.handoff.generator import (
            GeneratorInput,
            HandoffGenerationError,
            HandoffGenerator,
            collect_recent_messages,
        )
        from opencomputer.agent.handoff.inbox import HandoffInbox, InboxIOError

        # Source the provider adapter from runtime — the loop plumbs it
        # in alongside the rest of the per-session state.
        provider_adapter = runtime.custom.get("_handoff_provider_adapter")
        if provider_adapter is None:
            raise _ManualHandoffError(
                "no provider adapter available in this runtime — manual "
                "handoff is unsupported on this surface"
            )

        sid = runtime.custom.get("session_id") or ""
        # Read message history directly from SessionDB rather than relying
        # on a per-turn loop plumb of ``_handoff_recent_messages`` — this
        # method is called from a slash command, which dispatches BEFORE
        # the loop's per-turn classifier hook runs. Reading from DB is
        # the same pattern ``/title`` and ``/history`` use.
        messages: list = []
        db = runtime.custom.get("session_db")
        if db is not None and sid:
            try:
                messages = db.get_messages(sid)
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "manual /handoff: cannot read session history: %s", e,
                )
                messages = []
        if not messages:
            # Fall back to whatever the loop has plumbed (covers test
            # runtimes that don't set up SessionDB).
            messages = list(runtime.custom.get("_handoff_recent_messages", ()))
        users, assistants = collect_recent_messages(messages)

        generator = HandoffGenerator(provider_adapter)
        try:
            doc = await generator.generate(
                GeneratorInput(
                    source_profile=source_profile,
                    target_profile=target_profile,
                    source_session_id=sid,
                    recent_user_messages=users,
                    recent_assistant_messages=assistants,
                    trigger="manual",
                ),
            )
        except HandoffGenerationError as e:
            raise _ManualHandoffError(str(e)) from e

        if doc is None:
            return None

        inbox = HandoffInbox(target_home)
        try:
            return inbox.write(doc)
        except InboxIOError as e:
            raise _ManualHandoffError(f"inbox write failed: {e}") from e


def _parse_args(raw: str) -> tuple[bool, str]:
    """Return (skip_handoff, target_profile). Order-insensitive flag."""
    tokens = raw.split()
    skip_handoff = False
    target_tokens: list[str] = []
    for t in tokens:
        if t in ("--no-content", "-n"):
            skip_handoff = True
        else:
            target_tokens.append(t)
    target = " ".join(target_tokens).strip()
    return skip_handoff, target


def _profile_home(profile_id: str, get_profile_dir) -> Path:  # noqa: ANN001
    """Resolve a profile id to its home directory."""
    profile_root = get_profile_dir(None if profile_id == "default" else profile_id)
    return profile_root / "home"


class _ManualHandoffError(RuntimeError):
    """Internal error type — surfaced as user-facing text in execute()."""


__all__ = ["HandoffCommand"]
