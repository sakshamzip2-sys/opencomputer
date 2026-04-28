"""``opencomputer telegram`` CLI — manage Telegram DM Topics (Hermes PR 5.4).

Subcommands:

    opencomputer telegram topic-create <label> --chat <chat_id> [--skill X] [--system "..."]
        Create a Telegram forum topic (Bot API ``createForumTopic``) and
        persist its metadata locally so the adapter auto-loads
        the bound skill / system prompt for runs in that topic.

    opencomputer telegram topic-list
        Print all known DM topics (label / skill / system_prompt /
        parent_chat_id) from ``<profile_home>/telegram_dm_topics.json``.

    opencomputer telegram topic-remove <topic_id>
        Drop a topic entry from the local registry. Does NOT call
        Telegram's ``deleteForumTopic`` — operators decide whether the
        forum-side topic should also go.

The CLI reads ``TELEGRAM_BOT_TOKEN`` from the environment for the
``createForumTopic`` API call. ``--chat`` accepts the parent chat id
(typically a supergroup with forum-mode on); the spec is inherited
from Telegram Bot API 9.4.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from extensions.telegram.dm_topics import DMTopicManager

telegram_app = typer.Typer(
    name="telegram",
    help="Manage Telegram channel state (DM Topics).",
    no_args_is_help=True,
)
_console = Console()


def _profile_home() -> Path:
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME")
    if env:
        return Path(env)
    from opencomputer.agent.config import _home

    return _home()


def _bot_api_create_topic(
    *, token: str, chat_id: str, name: str
) -> dict[str, object]:
    """Call Telegram's ``createForumTopic`` Bot API.

    Synchronous httpx so the CLI doesn't need an event loop spin-up.
    Returns the parsed JSON ``result`` field on success; raises
    ``typer.Exit`` with a friendly message on transport / API error.
    """
    import httpx

    url = f"https://api.telegram.org/bot{token}/createForumTopic"
    payload = {"chat_id": chat_id, "name": name}
    try:
        resp = httpx.post(url, json=payload, timeout=15.0)
    except httpx.HTTPError as exc:
        _console.print(f"[red]network error contacting Telegram:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    try:
        body = resp.json()
    except json.JSONDecodeError as exc:
        _console.print(
            f"[red]Telegram returned non-JSON (HTTP {resp.status_code}):[/red] "
            f"{resp.text[:200]}"
        )
        raise typer.Exit(code=1) from exc
    if not body.get("ok"):
        _console.print(
            f"[red]Telegram createForumTopic failed:[/red] "
            f"{body.get('description', body)}"
        )
        raise typer.Exit(code=1)
    return body.get("result") or {}


@telegram_app.command("topic-create")
def topic_create(
    label: Annotated[
        str,
        typer.Argument(
            help="Human-readable topic name (e.g. 'Trading')."
        ),
    ],
    chat: Annotated[
        str | None,
        typer.Option(
            "--chat",
            help=(
                "Parent chat id (forum-mode supergroup). "
                "Required unless --no-create is given."
            ),
        ),
    ] = None,
    skill: Annotated[
        str | None,
        typer.Option(
            "--skill",
            help="Skill id auto-loaded for runs in this topic.",
        ),
    ] = None,
    system: Annotated[
        str | None,
        typer.Option(
            "--system",
            help="Per-topic ephemeral system prompt.",
        ),
    ] = None,
    topic_id: Annotated[
        str | None,
        typer.Option(
            "--topic-id",
            help=(
                "When --no-create is set, register an existing "
                "message_thread_id without calling the Bot API."
            ),
        ),
    ] = None,
    no_create: Annotated[
        bool,
        typer.Option(
            "--no-create",
            help=(
                "Skip the createForumTopic API call. Use with --topic-id "
                "to register a topic that already exists on Telegram."
            ),
        ),
    ] = False,
) -> None:
    """Create a Telegram forum topic + persist its skill/prompt binding."""
    if no_create:
        if not topic_id:
            _console.print(
                "[red]--no-create requires --topic-id <message_thread_id>[/red]"
            )
            raise typer.Exit(code=1)
        resolved_topic_id = str(topic_id)
        parent_chat = chat
    else:
        if not chat:
            _console.print(
                "[red]--chat <chat_id> is required to call createForumTopic.[/red]"
            )
            raise typer.Exit(code=1)
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            _console.print(
                "[red]TELEGRAM_BOT_TOKEN is not set — "
                "create the topic manually and re-run with --no-create --topic-id.[/red]"
            )
            raise typer.Exit(code=1)
        result = _bot_api_create_topic(token=token, chat_id=chat, name=label)
        thread_id = result.get("message_thread_id")
        if thread_id is None:
            _console.print(
                f"[red]Telegram response missing message_thread_id:[/red] {result}"
            )
            raise typer.Exit(code=1)
        resolved_topic_id = str(thread_id)
        parent_chat = chat

    home = _profile_home()
    home.mkdir(parents=True, exist_ok=True)
    mgr = DMTopicManager(home)
    mgr.register_topic(
        resolved_topic_id,
        label=label,
        skill=skill,
        system_prompt=system,
        parent_chat_id=parent_chat,
    )
    _console.print(
        f"[green]registered[/green] topic_id={resolved_topic_id} "
        f"label={label!r}"
        + (f" skill={skill!r}" if skill else "")
        + (f" system={system[:60]!r}…" if system else "")
    )


@telegram_app.command("topic-list")
def topic_list() -> None:
    """List all configured Telegram DM topics."""
    home = _profile_home()
    mgr = DMTopicManager(home)
    rows = mgr.list_topics()
    if not rows:
        _console.print(
            f"[dim]no DM topics registered at[/dim] "
            f"{home / 'telegram_dm_topics.json'}"
        )
        return

    table = Table(title="Telegram DM topics", title_style="bold")
    table.add_column("Topic ID", style="cyan", no_wrap=True)
    table.add_column("Label", style="white")
    table.add_column("Skill", style="magenta")
    table.add_column("Parent chat", style="dim", no_wrap=True)
    table.add_column("System prompt", style="green")

    for row in rows:
        prompt = row.get("system_prompt") or ""
        if prompt and len(prompt) > 50:
            prompt = prompt[:47] + "..."
        table.add_row(
            str(row.get("topic_id", "")),
            str(row.get("label") or ""),
            str(row.get("skill") or "—"),
            str(row.get("parent_chat_id") or "—"),
            prompt or "—",
        )
    _console.print(table)


@telegram_app.command("topic-remove")
def topic_remove(
    topic_id: Annotated[
        str, typer.Argument(help="Topic id to drop from the local registry.")
    ],
) -> None:
    """Remove a topic entry. Does NOT delete the Telegram-side forum topic."""
    home = _profile_home()
    mgr = DMTopicManager(home)
    if mgr.remove_topic(topic_id):
        _console.print(f"[green]removed[/green] topic_id={topic_id}")
    else:
        _console.print(
            f"[yellow]no entry for topic_id={topic_id} (nothing to remove)[/yellow]"
        )
        raise typer.Exit(code=1)


__all__ = ["telegram_app"]
