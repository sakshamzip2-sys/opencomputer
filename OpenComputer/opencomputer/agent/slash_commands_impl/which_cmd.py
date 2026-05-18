"""``/which`` — show how this chat resolved (D1, gateway-vs-CLI parity).

Bindings and routing rules pick a chat's profile / model silently — the
M3 ``↪ routed:`` badge only fires on a routing-rule match, once. ``/which``
returns the full resolution chain on demand so a connector user can see
which profile / model / session is actually answering them.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class WhichCommand(SlashCommand):
    name = "which"
    description = "Show how this chat resolved — platform, profile, model, session"
    gateway_safe = True

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        custom = runtime.custom or {}
        profile = (
            custom.get("active_profile_id")
            or custom.get("profile_id")
            or "default"
        )
        lines = [
            "## Resolution for this chat",
            f"  platform:  {custom.get('platform') or '(none)'}",
            f"  chat_id:   {custom.get('chat_id') or '(none)'}",
            f"  profile:   {profile}",
            f"  model:     {custom.get('model') or '(unknown)'}",
            f"  session:   {custom.get('session_id') or '(none)'}",
        ]
        return SlashCommandResult(output="\n".join(lines))


__all__ = ["WhichCommand"]
