"""v1.1 plan-3 M10.4 — `oc routing test/list` operator dry-run CLI.

Lets the operator inspect their `routing:` config without spinning up
the gateway. Two subcommands:

* ``oc routing list`` — print the parsed rule table from the active
  profile's config in priority order.
* ``oc routing test <platform> <chat_id>`` — print which rule matches
  a synthetic inbound event, which agent template fires, and which
  profile (if any rebind) — answers the operator question "what would
  happen if user X messaged me on platform Y right now?".

The actual routing wiring (M10.2 — dispatcher reads rules; M10.3 —
profile rebind) is a separate PR. This module ONLY reads the parsed
:class:`RoutingConfig` and runs the same resolver the dispatcher will
eventually use, so the dry-run is faithful by construction.
"""

from __future__ import annotations

import json as _json
from typing import Any

import typer

from opencomputer.agent.config_store import load_config
from opencomputer.agent.routing import (
    _match_specificity,
    resolve_routing_rule_by_fields,
)

routing_app = typer.Typer(
    name="routing",
    help="Inspect per-channel routing rules (M10.4 — dry-run only).",
    no_args_is_help=True,
)


@routing_app.command("list")
def list_rules(
    output: str = typer.Option(
        "text", "--output", "-o", help="text | json"
    ),
) -> None:
    """Print the routing rule table from the active profile, in priority
    order (most-specific first)."""
    cfg = load_config()
    routing = cfg.routing
    rules = routing.rules

    if output == "json":
        payload = {
            "rules": [
                {
                    "specificity": _match_specificity(r.match),
                    "match": {
                        k: getattr(r.match, k)
                        for k in (
                            "platform", "chat_id", "peer", "channel",
                            "guild", "team", "account", "role",
                        )
                        if getattr(r.match, k)
                    },
                    "agent": r.agent,
                    "profile": r.profile,
                }
                for r in rules
            ],
            "default": {
                "agent": routing.default.agent,
                "profile": routing.default.profile,
            },
        }
        typer.echo(_json.dumps(payload, indent=2))
        return

    if not rules:
        typer.echo("No routing rules configured.")
    else:
        typer.echo(f"Routing rules ({len(rules)} — most-specific first):")
        typer.echo("")
        for i, r in enumerate(rules, 1):
            spec = _match_specificity(r.match)
            match_parts = [
                f"{k}={getattr(r.match, k)!r}"
                for k in (
                    "platform", "chat_id", "peer", "channel",
                    "guild", "team", "account", "role",
                )
                if getattr(r.match, k)
            ]
            match_str = ", ".join(match_parts) if match_parts else "(any)"
            line = f"  {i}. [spec={spec}] {match_str} → agent={r.agent!r}"
            if r.profile:
                line += f", profile={r.profile!r}"
            typer.echo(line)
        typer.echo("")
    typer.echo(
        f"Default: agent={routing.default.agent!r}"
        + (f", profile={routing.default.profile!r}" if routing.default.profile else "")
    )


@routing_app.command("test")
def test_match(
    platform: str = typer.Argument(..., help="Platform id, e.g. slack, telegram, discord"),
    chat_id: str = typer.Argument(..., help="Inbound chat / peer / channel id"),
    channel: str = typer.Option("", "--channel", help="Channel name (e.g. '#security-alerts')"),
    guild: str = typer.Option("", "--guild", help="Discord guild / Slack workspace id"),
    team: str = typer.Option("", "--team", help="Slack team / Matrix room namespace"),
    account: str = typer.Option("", "--account", help="Bot/account id (multi-bot daemons)"),
    role: str = typer.Option("", "--role", help="Sender's role (Discord member role)"),
    output: str = typer.Option("text", "--output", "-o", help="text | json"),
) -> None:
    """Print which rule matches a synthetic inbound event.

    Example::

        oc routing test slack U123 --channel security-alerts
        oc routing test discord U123 --guild myguild --role admin
        oc routing test telegram 12345
    """
    cfg = load_config()
    fields: dict[str, str] = {
        "platform": platform,
        "chat_id": chat_id,
        "peer": chat_id,
        "channel": channel.lstrip("#"),
        "guild": guild,
        "team": team,
        "account": account,
        "role": role,
    }
    outcome = resolve_routing_rule_by_fields(cfg.routing, fields)

    if output == "json":
        payload: dict[str, Any] = {
            "input": {k: v for k, v in fields.items() if k != "peer" and v}
                     | ({"peer": fields["peer"]} if fields["peer"] else {}),
            "agent": outcome.agent,
            "profile": outcome.profile,
            "matched_default": outcome.matched_default,
        }
        if outcome.rule is not None:
            payload["rule"] = {
                "match": {
                    k: getattr(outcome.rule.match, k)
                    for k in (
                        "platform", "chat_id", "peer", "channel",
                        "guild", "team", "account", "role",
                    )
                    if getattr(outcome.rule.match, k)
                },
                "agent": outcome.rule.agent,
                "profile": outcome.rule.profile,
            }
        typer.echo(_json.dumps(payload, indent=2))
        return

    set_dims = ", ".join(f"{k}={v!r}" for k, v in fields.items() if k != "peer" and v)
    typer.echo(f"Input: {set_dims}")
    if outcome.matched_default:
        typer.echo(
            f"→ DEFAULT (no rule matched). agent={outcome.agent!r}"
            + (f", profile={outcome.profile!r}" if outcome.profile else "")
        )
    else:
        typer.echo(
            f"→ Matched rule (specificity={_match_specificity(outcome.rule.match)}): "
            f"agent={outcome.agent!r}"
            + (f", profile={outcome.profile!r}" if outcome.profile else "")
        )


__all__ = ["routing_app"]
