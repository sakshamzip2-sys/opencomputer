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


def _bootstrap_adapter_runner_namespace() -> None:
    """Make ``extensions.adapter_runner`` importable from the hyphenated dir.

    Mirrors the helper in ``extensions/browser-control/_tool.py`` —
    kept self-contained so the CLI works even when the adapter-runner
    plugin hasn't been loaded yet.
    """
    import sys
    import types
    from pathlib import Path

    extensions_root = Path(__file__).resolve().parent.parent / "extensions"
    plugin_root = extensions_root / "adapter-runner"
    if not plugin_root.is_dir():
        return

    extensions_root_str = str(extensions_root)
    if extensions_root_str not in sys.path:
        sys.path.insert(0, extensions_root_str)
    if "extensions" not in sys.modules:
        parent = types.ModuleType("extensions")
        parent.__path__ = [extensions_root_str]
        parent.__package__ = "extensions"
        sys.modules["extensions"] = parent
    pkg = sys.modules.get("extensions.adapter_runner")
    if pkg is None:
        pkg = types.ModuleType("extensions.adapter_runner")
        pkg.__path__ = [str(plugin_root)]
        pkg.__package__ = "extensions.adapter_runner"
        sys.modules["extensions.adapter_runner"] = pkg
        sys.modules["extensions"].adapter_runner = pkg  # type: ignore[attr-defined]
    if not hasattr(pkg, "adapter"):
        init_file = plugin_root / "__init__.py"
        if init_file.is_file():
            try:
                source = init_file.read_text(encoding="utf-8")
                code = compile(source, str(init_file), "exec")
                exec(code, pkg.__dict__)
            except Exception:
                pass  # Discovery will surface specifics below


@adapter_app.command("list")
def adapter_list() -> None:
    """List discovered synthetic-tool adapters (browser-control + adapter-pack plugins) (M1.B4).

    Adapters here are the @adapter decorator's discovered functions
    (extensions/adapter-runner/), not channel adapters. For channel
    adapters, see ``opencomputer plugins`` filtered by ``kind=channel``.
    """
    _bootstrap_adapter_runner_namespace()
    try:
        from extensions.adapter_runner._discovery import (
            discover_adapters,  # type: ignore[import-not-found]
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"adapter-runner not available: {exc}")
        raise typer.Exit(1) from exc

    result = discover_adapters()
    if not result.specs:
        typer.echo("No adapters discovered.")
        if result.errors:
            typer.echo("\nDiscovery errors:")
            for e in result.errors:
                typer.echo(f"  - {e}")
        return

    table = Table(title=f"Discovered adapters ({len(result.specs)})")
    table.add_column("Tool", style="cyan", no_wrap=True)
    table.add_column("Site", style="yellow")
    table.add_column("Strategy")
    table.add_column("Description")

    for spec in result.specs:
        table.add_row(
            spec.tool_name,
            spec.site,
            getattr(spec.strategy, "name", str(spec.strategy)),
            (spec.description or "—")[:80],
        )
    _console.print(table)
    if result.errors:
        _console.print(f"\n[dim red]{len(result.errors)} discovery error(s) — re-run with `oc doctor` for details[/dim red]")


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
