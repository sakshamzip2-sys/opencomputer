"""session_bootstrap — SessionStart hook that primes the harness state dir.

Creates `~/.opencomputer/harness/<session_id>/{rewind,state}/` if missing and
records the session start time in session_state so other hooks can reason
about recency.
"""

from __future__ import annotations

from datetime import UTC, datetime

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec


def build_session_bootstrap_hook_spec(*, harness_ctx) -> HookSpec:
    async def handler(ctx: HookContext) -> HookDecision | None:
        # The RewindStore + SessionStateStore ctor already mkdir-p'd the dirs,
        # so there's no new dir work to do — we just record the bootstrap.
        harness_ctx.session_state.set(
            "session_started_at", datetime.now(UTC).isoformat()
        )
        harness_ctx.session_state.set(
            "edited_files", harness_ctx.session_state.get("edited_files", [])
        )
        return None

    return HookSpec(
        event=HookEvent.SESSION_START,
        handler=handler,
        matcher=None,
        fire_and_forget=True,
    )


__all__ = ["build_session_bootstrap_hook_spec"]
