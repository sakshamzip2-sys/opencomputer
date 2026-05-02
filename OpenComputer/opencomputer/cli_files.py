"""`oc files` CLI subcommand group — manage Anthropic Files API uploads.

Wraps :class:`AnthropicFilesClient` from
``extensions/anthropic-provider/files_client.py``.

All operations are FREE per Anthropic docs; only message tokens cost.
Workspace-scoped: all API keys in your workspace see each other's files.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

# Lazy-load the FilesClient module from the extension dir.
# The Anthropic provider lives in extensions/ which isn't on sys.path,
# so import via spec_from_file_location.
_REPO_ROOT = Path(__file__).parent.parent
_FILES_CLIENT_PATH = (
    _REPO_ROOT / "extensions" / "anthropic-provider" / "files_client.py"
)
_SYNTHETIC_MODULE_NAME = "extensions_anthropic_provider_files_client"


def _load_files_client_module():
    """Load the Anthropic files_client.py module under a stable synthetic name.

    The extension dir isn't on sys.path so we use spec_from_file_location.
    Registering the module under a stable name in sys.modules lets tests
    import ``FileMetadata`` etc. directly via that synthetic module name.
    """
    existing = sys.modules.get(_SYNTHETIC_MODULE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        _SYNTHETIC_MODULE_NAME,
        _FILES_CLIENT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_module = _load_files_client_module()
AnthropicFilesClient = _module.AnthropicFilesClient
FilesAPIError = _module.FilesAPIError
FileMetadata = _module.FileMetadata


files_app = typer.Typer(
    help="Manage Anthropic Files API uploads (upload/list/delete/download/info).",
    no_args_is_help=True,
)
console = Console()


def _resolve_api_key() -> str:
    """Get the Anthropic API key from env. Exits with message if absent."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        typer.echo(
            "error: ANTHROPIC_API_KEY not set. Set it or configure your "
            "Anthropic provider before using `oc files`.",
            err=True,
        )
        raise typer.Exit(code=1)
    return key


def _client():
    return AnthropicFilesClient(api_key=_resolve_api_key())


def _format_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _run(coro):
    """Run an async coroutine; surface FilesAPIError cleanly."""
    try:
        return asyncio.run(coro)
    except FilesAPIError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@files_app.command("list")
def cmd_list(
    limit: int = typer.Option(50, "--limit", "-n", help="Max files to show."),
) -> None:
    """List uploaded files in this workspace."""
    files = _run(_client().list(limit=limit))
    if not files:
        typer.echo("(no files uploaded)")
        return
    table = Table(title=f"Anthropic Files ({len(files)})")
    table.add_column("ID")
    table.add_column("Filename")
    table.add_column("MIME")
    table.add_column("Size")
    table.add_column("Created")
    table.add_column("Downloadable")
    for f in files:
        table.add_row(
            f.id,
            f.filename,
            f.mime_type,
            _format_size(f.size_bytes),
            f.created_at.strftime("%Y-%m-%d %H:%M"),
            "yes" if f.downloadable else "no",
        )
    console.print(table)


@files_app.command("upload")
def cmd_upload(
    path: Path = typer.Argument(..., help="Local file path to upload."),
) -> None:
    """Upload a file; prints the resulting file_id."""
    meta = _run(_client().upload(path))
    typer.echo(
        f"uploaded: {meta.id} ({meta.filename}, {_format_size(meta.size_bytes)})"
    )


@files_app.command("delete")
def cmd_delete(
    file_id: str = typer.Argument(..., help="File ID to delete."),
) -> None:
    """Delete a file from the workspace."""
    _run(_client().delete(file_id))
    typer.echo(f"deleted: {file_id}")


@files_app.command("download")
def cmd_download(
    file_id: str = typer.Argument(..., help="File ID (must be model-created)."),
    output: Path = typer.Argument(..., help="Local output path."),
) -> None:
    """Download a model-created file (skills / code-execution outputs).

    User-uploaded files cannot be downloaded — Anthropic API restriction.
    """
    bytes_written = _run(_client().download(file_id, output))
    typer.echo(f"downloaded: {file_id} -> {output} ({bytes_written} bytes)")


@files_app.command("info")
def cmd_info(
    file_id: str = typer.Argument(..., help="File ID."),
) -> None:
    """Show metadata for a single file."""
    meta = _run(_client().get_metadata(file_id))
    typer.echo(f"id:           {meta.id}")
    typer.echo(f"filename:     {meta.filename}")
    typer.echo(f"mime_type:    {meta.mime_type}")
    typer.echo(f"size_bytes:   {meta.size_bytes} ({_format_size(meta.size_bytes)})")
    typer.echo(f"created_at:   {meta.created_at.isoformat()}")
    typer.echo(f"downloadable: {'yes' if meta.downloadable else 'no'}")
