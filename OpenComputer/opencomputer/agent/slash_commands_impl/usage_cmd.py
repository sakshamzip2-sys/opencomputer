"""``/usage`` — show in-loop token + rate-limit status.

Tier 2.A.15 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Reads from ``runtime.custom`` keys that the agent loop / provider plugins
update each turn:

- ``session_tokens_in``  — input tokens used this session
- ``session_tokens_out`` — output tokens used this session
- ``session_cost_usd``   — accumulated cost this session
- ``rate_limit_reset_at`` — UNIX timestamp when current rate limit resets
- ``rate_limit_remaining`` — requests remaining in window

Plugins/loop integration ships them as available; this slash command
just renders what's there. Missing keys → "(not tracked)".
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


def _fmt_tokens(n) -> str:
    if not isinstance(n, int):
        return "(not tracked)"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_cost(c) -> str:
    if not isinstance(c, (int, float)):
        return "(not tracked)"
    if c < 0.01:
        return f"<${0.01}"
    return f"${c:.2f}"


class UsageCommand(SlashCommand):
    name = "usage"
    description = "Show session token usage + rate-limit state"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        in_t = runtime.custom.get("session_tokens_in")
        out_t = runtime.custom.get("session_tokens_out")
        cost = runtime.custom.get("session_cost_usd")
        rl_remaining = runtime.custom.get("rate_limit_remaining")
        rl_reset = runtime.custom.get("rate_limit_reset_at")

        lines = ["## Session usage"]
        lines.append(f"  input tokens:  {_fmt_tokens(in_t)}")
        lines.append(f"  output tokens: {_fmt_tokens(out_t)}")
        lines.append(f"  cost (est):    {_fmt_cost(cost)}")

        if rl_remaining is not None or rl_reset is not None:
            lines.append("\n## Rate limit")
            if rl_remaining is not None:
                lines.append(f"  remaining: {rl_remaining}")
            if rl_reset is not None:
                lines.append(f"  resets at: {rl_reset}")

        return SlashCommandResult(output="\n".join(lines), handled=True)


__all__ = ["UsageCommand"]
