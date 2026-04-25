"""``opencomputer adapter`` CLI — discoverable alias for channel-plugin scaffolding.

The underlying machinery lives in ``opencomputer/cli_plugin_scaffold.py``
(Sub-project B). This module just provides a more discoverable surface:

    opencomputer adapter new <name>          → plugin new <name> --kind channel
    opencomputer adapter capabilities        → list ChannelCapabilities flags

Channel adapters are the most common plugin authors write (1 channel ≈
1 platform integration), so giving them a top-level command is worth it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from plugin_sdk import ChannelCapabilities

adapter_app = typer.Typer(
    name="adapter",
    help="Scaffold and inspect channel adapters.",
    no_args_is_help=True,
)
_console = Console()


@adapter_app.command("new")
def adapter_new(
    name: Annotated[str, typer.Argument(help="Adapter id (lowercase, hyphens allowed).")],
    path: Annotated[
        Path | None,
        typer.Option("--path", "-p", help="Output directory (default: ~/.opencomputer/plugins/)."),
    ] = None,
    description: Annotated[
        str, typer.Option("--description", "-d", help="Free-form plugin description.")
    ] = "",
    author: Annotated[str, typer.Option("--author", "-a", help="Author string for the manifest.")] = "",
    force: Annotated[bool, typer.Option("--force", "-f", help="Overwrite existing dir.")] = False,
) -> None:
    """Scaffold a new channel adapter plugin.

    Equivalent to ``opencomputer plugin new <name> --kind channel`` but more
    discoverable. The generated adapter declares ``ChannelCapabilities.NONE``
    by default with commented-out stubs for every optional capability —
    uncomment what your platform supports and update the flag.

    Reference implementations included in OC:

    - ``extensions/telegram/`` — full capability set (typing, reactions,
      photo/document/voice in+out, edit, delete).
    - ``extensions/discord/`` — typing only.
    - ``extensions/webhook/`` — inbound-only HTTP listener.
    """
    # Delegate to the plugin scaffolder
    from opencomputer.cli_plugin import plugin_new

    plugin_new(
        name=name,
        kind="channel",
        path=path,
        force=force,
        description=description,
        author=author,
        no_smoke=False,
    )


@adapter_app.command("capabilities")
def adapter_capabilities() -> None:
    """List all ``ChannelCapabilities`` flags with one-line descriptions.

    Useful when authoring an adapter to know what your platform might
    support and which method to override for each.
    """
    descriptions: dict[str, str] = {
        "TYPING": "Send typing indicators (heartbeat while agent thinks).",
        "REACTIONS": "Emoji reactions on messages (e.g. 👀 / ✅ / ⚠️).",
        "VOICE_OUT": "Send voice / audio messages outbound.",
        "VOICE_IN": "Receive voice messages inbound.",
        "PHOTO_OUT": "Send images / photos outbound.",
        "PHOTO_IN": "Receive photos inbound.",
        "DOCUMENT_OUT": "Send arbitrary file documents (PDF / ZIP / etc.) outbound.",
        "DOCUMENT_IN": "Receive arbitrary file documents inbound.",
        "EDIT_MESSAGE": "Edit a previously-sent text message in place.",
        "DELETE_MESSAGE": "Delete a previously-sent message.",
        "THREADS": "Threaded replies (Discord threads, Slack threads, Matrix replies).",
    }

    table = Table(title="ChannelCapabilities flags")
    table.add_column("Flag", style="cyan", no_wrap=True)
    table.add_column("Method to override", style="yellow")
    table.add_column("What it enables")

    method_map: dict[str, str] = {
        "TYPING": "send_typing",
        "REACTIONS": "send_reaction",
        "VOICE_OUT": "send_voice",
        "VOICE_IN": "download_attachment",
        "PHOTO_OUT": "send_photo",
        "PHOTO_IN": "download_attachment",
        "DOCUMENT_OUT": "send_document",
        "DOCUMENT_IN": "download_attachment",
        "EDIT_MESSAGE": "edit_message",
        "DELETE_MESSAGE": "delete_message",
        "THREADS": "(metadata only; no method)",
    }
    for cap_name in [c.name for c in ChannelCapabilities if c.name and c.name != "NONE"]:
        table.add_row(cap_name, method_map.get(cap_name or "", "?"), descriptions.get(cap_name or "", "?"))
    _console.print(table)
    _console.print(
        "\n[dim]See ``plugin_sdk/channel_contract.py`` for the canonical method "
        "signatures and ``extensions/telegram/adapter.py`` for a reference impl.[/dim]"
    )


__all__ = ["adapter_app"]
