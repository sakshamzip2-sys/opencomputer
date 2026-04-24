"""SlashCommand contract for the coding-harness — subclass of plugin_sdk's.

Phase 12b.6 Task D8: the SDK now ships ``plugin_sdk.SlashCommand`` as the
formal base. The harness used to ship a duck-typed shim here while the
SDK was catching up; now we subclass the real thing, and each bundled
command takes a ``HarnessContext`` at construction time. The public
``execute(args, runtime) -> SlashCommandResult`` signature matches the
SDK contract the core dispatcher expects.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from plugin_sdk.slash_command import SlashCommand as _CoreSlashCommand
from plugin_sdk.slash_command import SlashCommandResult


class SlashCommand(_CoreSlashCommand):
    """Harness-internal base — binds a shared HarnessContext at init.

    The core ``plugin_sdk.SlashCommand.execute(args, runtime)`` signature
    doesn't carry ``harness_ctx`` — it's captured here at construction
    instead, so the public signature matches what the core dispatcher
    calls.
    """

    def __init__(self, harness_ctx: Any) -> None:
        self.harness_ctx = harness_ctx

    @abstractmethod
    async def execute(
        self, args: str, runtime: Any
    ) -> SlashCommandResult:  # pragma: no cover - abstract
        ...


__all__ = ["SlashCommand", "SlashCommandResult"]
