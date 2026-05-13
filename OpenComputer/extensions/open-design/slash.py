"""``/design [open|status|start|stop|url]`` — chat slash command.

Lightweight wrapper around :mod:`lifecycle`. Runs in-process so the
agent can drive the daemon from a chat session (e.g. "open the design
tab") without shelling out to ``oc design …``.

Crashes are intentionally swallowed per OC convention — a broken slash
command must never wedge the chat loop.
"""

from __future__ import annotations

import logging

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

# Plugin-loader puts the plugin root on sys.path[0]; flat import works.
# The hyphenated dir name (extensions/open-design/) cannot be imported
# as a Python package, so there is no fallback path — tests import via
# importlib + spec_from_file_location instead.
from lifecycle import (  # noqa: E402 — sys.path[0] populated by loader
    DaemonAlreadyRunningError,
    OpenDesignNotInstalledError,
    restart as lifecycle_restart,
    start as lifecycle_start,
    status as lifecycle_status,
    stop as lifecycle_stop,
)

_log = logging.getLogger("opencomputer.open_design.slash")

_HELP = (
    "/design — manage Open Design sidecar\n"
    "  /design status     show daemon health + URL (default)\n"
    "  /design start      spawn daemon (default port 7456)\n"
    "  /design stop       terminate daemon\n"
    "  /design restart    stop + start\n"
    "  /design open       alias for status — returns URL for the Design tab\n"
    "  /design url        print only the URL\n"
)


def _status_line() -> str:
    snap = lifecycle_status()
    state = "running" if snap.running else "stopped"
    pid = snap.pid if snap.pid is not None else "—"
    return f"open-design: {state} · url {snap.url} · pid {pid}"


class DesignCommand(SlashCommand):
    name = "design"
    description = "Manage Open Design sidecar (start/stop/status/url)."

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower() or "status"

        if sub in {"help", "?"}:
            return SlashCommandResult(output=_HELP, handled=True)

        if sub in {"status", "open"}:
            return SlashCommandResult(output=_status_line(), handled=True)

        if sub == "url":
            snap = lifecycle_status()
            return SlashCommandResult(output=snap.url, handled=True)

        if sub == "start":
            try:
                snap = lifecycle_start()
            except DaemonAlreadyRunningError as exc:
                return SlashCommandResult(output=f"already running — {exc}", handled=True)
            except OpenDesignNotInstalledError as exc:
                return SlashCommandResult(output=f"open-design not installed: {exc}", handled=True)
            except Exception as exc:  # noqa: BLE001 — slash must never raise
                _log.warning("/design start crashed: %s", exc)
                return SlashCommandResult(output=f"start failed: {exc}", handled=True)
            return SlashCommandResult(
                output=(
                    f"open-design: started at {snap.url} (pid={snap.pid})"
                    if snap.running
                    else f"open-design: failed to come up — see {snap.log_path}"
                ),
                handled=True,
            )

        if sub == "stop":
            try:
                snap = lifecycle_stop()
            except Exception as exc:  # noqa: BLE001
                _log.warning("/design stop crashed: %s", exc)
                return SlashCommandResult(output=f"stop failed: {exc}", handled=True)
            return SlashCommandResult(
                output="open-design: stopped" if not snap.running else "still running",
                handled=True,
            )

        if sub == "restart":
            try:
                snap = lifecycle_restart()
            except OpenDesignNotInstalledError as exc:
                return SlashCommandResult(output=f"open-design not installed: {exc}", handled=True)
            except Exception as exc:  # noqa: BLE001
                _log.warning("/design restart crashed: %s", exc)
                return SlashCommandResult(output=f"restart failed: {exc}", handled=True)
            return SlashCommandResult(
                output=(
                    f"open-design: restarted at {snap.url} (pid={snap.pid})"
                    if snap.running
                    else f"restart did not become healthy — see {snap.log_path}"
                ),
                handled=True,
            )

        return SlashCommandResult(
            output=f"unknown subcommand: /design {sub}\n{_HELP}",
            handled=True,
        )


__all__ = ["DesignCommand"]
