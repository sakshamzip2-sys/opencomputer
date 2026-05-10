"""Task II.3 — ``opencomputer channels`` CLI subcommand group.

A thin Rich-table viewer over ``opencomputer/gateway/channel_directory.py``.

Commands:

  opencomputer channels list
    Print the directory at ``~/.opencomputer/channel_directory.json`` as a
    Rich table sorted by most-recent ``last_seen``.
  opencomputer channels status
    Pairing-status diagnostic — for each installed channel-kind extension,
    show whether credentials are configured. Closes the audit gap where
    "84 extensions installed, only Telegram paired" had no surface.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

channels_app = typer.Typer(
    name="channels",
    help="Inspect the cached channel directory + per-channel pairing status.",
    no_args_is_help=True,
)
_console = Console()


# 2026-05-10 — Channel auth-env-var map. Built from `grep -rn "os.environ.get"
# extensions/<channel>/plugin.py`. Each entry: (channel_id, [required env vars],
# [optional env vars]). Channels without env-var auth (feishu, dingtalk, irc)
# are flagged as "config-driven" — they need YAML config rather than env vars.
_CHANNEL_ENV: dict[str, dict[str, list[str]]] = {
    "telegram": {
        "required": ["TELEGRAM_BOT_TOKEN"],
        "optional": ["TELEGRAM_ADMIN_CHAT_ID"],
    },
    "discord": {"required": ["DISCORD_BOT_TOKEN"], "optional": []},
    "slack": {"required": ["SLACK_BOT_TOKEN"], "optional": []},
    "matrix": {
        "required": ["MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN"],
        "optional": ["MATRIX_CONSENT_CHAT_ID"],
    },
    "mattermost": {
        "required": ["MATTERMOST_URL", "MATTERMOST_TOKEN"],
        "optional": [],
    },
    "signal": {
        "required": ["SIGNAL_CLI_URL", "SIGNAL_PHONE_NUMBER"],
        "optional": [],
    },
    "whatsapp": {
        "required": ["WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"],
        "optional": [],
    },
    "email": {
        "required": [
            "EMAIL_IMAP_HOST",
            "EMAIL_USERNAME",
            "EMAIL_PASSWORD",
        ],
        "optional": [
            "EMAIL_IMAP_PORT",
            "EMAIL_SMTP_HOST",
            "EMAIL_SMTP_PORT",
            "EMAIL_FROM_ADDRESS",
            "EMAIL_MAILBOX",
            "EMAIL_POLL_INTERVAL",
            "EMAIL_ALLOWED_SENDERS",
        ],
    },
    "webhook": {
        "required": [],
        "optional": ["WEBHOOK_HOST", "WEBHOOK_PORT"],
    },
    "homeassistant": {
        "required": ["HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN"],
        "optional": ["HASS_COOLDOWN_SECONDS", "HASS_WATCH_ALL"],
    },
    "sms": {
        "required": [
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_PHONE_NUMBER",
        ],
        "optional": [
            "SMS_WEBHOOK_HOST",
            "SMS_WEBHOOK_PORT",
            "SMS_WEBHOOK_URL",
        ],
    },
    "imessage": {
        "required": ["BLUEBUBBLES_URL", "BLUEBUBBLES_PASSWORD"],
        "optional": ["BLUEBUBBLES_POLL_INTERVAL"],
    },
}


def _resolve_extensions_dir() -> Path | None:
    """Locate the bundled extensions/ directory (matches plugin discovery)."""
    try:
        import opencomputer

        pkg_path = Path(opencomputer.__file__).resolve().parent
        # extensions/ is a sibling of opencomputer/ in the repo
        ext_dir = pkg_path.parent / "extensions"
        if ext_dir.exists() and ext_dir.is_dir():
            return ext_dir
    except Exception:  # noqa: BLE001
        pass
    return None


def _check_channel_credentials(channel: str) -> tuple[str, str]:
    """Return (status, detail) for one channel's credential state.

    Status one of: "paired" (all required env vars set), "missing" (some
    required env vars missing), "config-driven" (no env vars defined —
    user must consult plugin.py), or "unknown" (channel id not in the
    env map).
    """
    spec = _CHANNEL_ENV.get(channel)
    if spec is None:
        return ("unknown", "channel not in env map; consult plugin.py")

    required = spec["required"]
    optional = spec["optional"]

    if not required and not optional:
        return ("config-driven", "no env vars defined — see plugin.py")

    missing = [v for v in required if not os.environ.get(v, "").strip()]
    if missing:
        hint = "; ".join(missing)
        return ("missing", f"set: {hint}")

    if not required:
        # All-optional channel (e.g., webhook): "paired" when at least
        # one optional is set, otherwise "config-driven" (defaults).
        any_optional = any(os.environ.get(v, "").strip() for v in optional)
        if any_optional:
            return ("paired", "running on optional env config")
        return ("config-driven", "all env vars optional; using defaults")

    return ("paired", "all required env vars present")


@channels_app.command("list")
def channels_list() -> None:
    """Print all known channels, sorted by most-recent activity first."""
    from opencomputer.gateway.channel_directory import ChannelDirectory

    directory = ChannelDirectory()
    entries = directory.list_all()

    if not entries:
        _console.print(
            f"[dim]no channels recorded yet at[/dim] {directory.path}"
        )
        return

    table = Table(title="OpenComputer channel directory", title_style="bold")
    table.add_column("Platform", style="cyan", no_wrap=True)
    table.add_column("Chat ID", style="magenta", no_wrap=True)
    table.add_column("Display name", style="white")
    table.add_column("Last seen (UTC)", style="dim", no_wrap=True)

    for entry in entries:
        seen = (
            datetime.fromtimestamp(entry.last_seen, tz=UTC)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        table.add_row(
            entry.platform,
            entry.chat_id,
            entry.display_name or "[dim](none)[/dim]",
            seen,
        )

    _console.print(table)
    _console.print(f"[dim]source: {directory.path}[/dim]")


@channels_app.command("status")
def channels_status() -> None:
    """Per-channel pairing status — closes the audit gap.

    For each installed channel-kind extension, show whether credentials
    are configured. The user audit (2026-05-10) found "84 extensions
    installed, only Telegram paired" with no diagnostic surface for
    operators to see WHY each channel isn't paired. This command lists
    every channel found in extensions/ alongside required env vars and
    fix hints.

    Status legend:
      * paired         — all required env vars present
      * missing        — required env vars not set (lists which ones)
      * config-driven  — no env vars defined; user edits plugin/yaml
      * not-installed  — channel directory not present in extensions/
    """
    ext_dir = _resolve_extensions_dir()
    if ext_dir is None:
        _console.print(
            "[yellow]warning:[/yellow] extensions/ directory not found "
            "(running from a non-source install?). Status check unavailable."
        )
        return

    table = Table(title="Channel pairing status", title_style="bold")
    table.add_column("Channel", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail", overflow="fold")

    style = {
        "paired": "green",
        "missing": "yellow",
        "config-driven": "dim",
        "not-installed": "red",
        "unknown": "magenta",
    }

    counts = {"paired": 0, "missing": 0, "config-driven": 0, "not-installed": 0}

    for channel in sorted(_CHANNEL_ENV.keys()):
        ext_path = ext_dir / channel
        if not ext_path.exists():
            status, detail = "not-installed", f"no extensions/{channel}/ directory"
        else:
            status, detail = _check_channel_credentials(channel)
        counts[status] = counts.get(status, 0) + 1
        table.add_row(
            channel, f"[{style.get(status, 'white')}]{status}[/]", detail
        )

    _console.print(table)
    summary = " · ".join(f"{n} {k}" for k, n in counts.items() if n > 0)
    _console.print(f"[dim]{summary}[/dim]")


__all__ = ["channels_app"]
