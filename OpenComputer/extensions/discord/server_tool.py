"""Discord server introspection + management tool — Hermes parity port (2026-05-01).

Single OC ``BaseTool`` (`DiscordServerTool`) that dispatches to one of 14 actions
via an ``action`` enum parameter, mirroring Hermes ``discord_server`` exactly.

Two gates filter the schema the model sees:

1. **Privileged intents** detected from ``GET /applications/@me`` (cached
   per-process). When the bot lacks ``GUILD_MEMBERS``, the ``search_members``
   and ``member_info`` actions are hidden. ``MESSAGE_CONTENT`` is detected
   too — when missing, ``fetch_messages`` and ``list_pins`` keep their slot
   but the description warns that ``content`` will be empty.

2. **User config allowlist** at ``discord.server_actions`` in
   ``~/.opencomputer/config.yaml`` (string or list). When set, only the
   listed actions appear in the schema. Empty/unset → all intent-available
   actions are exposed.

Per-guild permissions (``MANAGE_ROLES`` etc.) are NOT pre-checked — Discord
returns 403 at call time and ``_enrich_403`` maps it to actionable hints
the agent can relay to the user.

Capability claims:

* **Read-only** actions (list_guilds, server_info, list_channels, channel_info,
  list_roles, member_info, search_members, fetch_messages, list_pins) →
  ``discord.read`` IMPLICIT tier.
* **Mutating** actions (pin_message, unpin_message, create_thread, add_role,
  remove_role) → ``discord.manage`` EXPLICIT tier.

The tool declares both — F1's ConsentGate enforces the right tier at dispatch.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger("opencomputer.discord.server_tool")

DISCORD_API_BASE = "https://discord.com/api/v10"

_FLAG_GATEWAY_GUILD_MEMBERS = 1 << 14
_FLAG_GATEWAY_GUILD_MEMBERS_LIMITED = 1 << 15
_FLAG_GATEWAY_MESSAGE_CONTENT = 1 << 18
_FLAG_GATEWAY_MESSAGE_CONTENT_LIMITED = 1 << 19


class DiscordAPIError(Exception):
    """Raised when a Discord REST call returns non-2xx."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Discord API error {status}: {body}")


def _get_bot_token() -> str | None:
    return os.environ.get("DISCORD_BOT_TOKEN", "").strip() or None


def _discord_request(
    method: str,
    path: str,
    token: str,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 15,
) -> Any:
    """Issue a request against the Discord REST API."""
    url = f"{DISCORD_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "OpenComputer (https://github.com/opencomputer)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status == 204:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise DiscordAPIError(e.code, body_text) from e


_CHANNEL_TYPE_NAMES = {
    0: "text", 2: "voice", 4: "category", 5: "announcement",
    10: "announcement_thread", 11: "public_thread", 12: "private_thread",
    13: "stage", 15: "forum", 16: "media",
}


def _channel_type_name(type_id: int) -> str:
    return _CHANNEL_TYPE_NAMES.get(type_id, f"unknown({type_id})")


# ─── Capability detection ────────────────────────────────────────

_capability_cache: dict[str, Any] | None = None


def _detect_capabilities(token: str, *, force: bool = False) -> dict[str, Any]:
    """Detect privileged intents via GET /applications/@me. Cached."""
    global _capability_cache
    if _capability_cache is not None and not force:
        return _capability_cache

    caps: dict[str, Any] = {
        "has_members_intent": True,
        "has_message_content": True,
        "detected": False,
    }
    try:
        app = _discord_request("GET", "/applications/@me", token, timeout=5)
        flags = int(app.get("flags", 0) or 0)
        caps["has_members_intent"] = bool(
            flags & (_FLAG_GATEWAY_GUILD_MEMBERS | _FLAG_GATEWAY_GUILD_MEMBERS_LIMITED)
        )
        caps["has_message_content"] = bool(
            flags & (_FLAG_GATEWAY_MESSAGE_CONTENT | _FLAG_GATEWAY_MESSAGE_CONTENT_LIMITED)
        )
        caps["detected"] = True
    except Exception as exc:  # noqa: BLE001
        _log.info("Discord capability detection failed (%s); exposing all actions.", exc)

    _capability_cache = caps
    return caps


def _reset_capability_cache() -> None:
    """Test hook: clear the detection cache."""
    global _capability_cache
    _capability_cache = None


# ─── Action implementations ──────────────────────────────────────

def _list_guilds(token: str, **_kw: Any) -> str:
    guilds = _discord_request("GET", "/users/@me/guilds", token)
    out = [
        {
            "id": g["id"], "name": g["name"], "icon": g.get("icon"),
            "owner": g.get("owner", False), "permissions": g.get("permissions"),
        }
        for g in guilds
    ]
    return json.dumps({"guilds": out, "count": len(out)})


def _server_info(token: str, *, guild_id: str, **_kw: Any) -> str:
    g = _discord_request("GET", f"/guilds/{guild_id}", token, params={"with_counts": "true"})
    return json.dumps({
        "id": g["id"], "name": g["name"],
        "description": g.get("description"), "icon": g.get("icon"),
        "owner_id": g.get("owner_id"),
        "member_count": g.get("approximate_member_count"),
        "online_count": g.get("approximate_presence_count"),
        "features": g.get("features", []),
        "premium_tier": g.get("premium_tier"),
        "premium_subscription_count": g.get("premium_subscription_count"),
        "verification_level": g.get("verification_level"),
    })


def _list_channels(token: str, *, guild_id: str, **_kw: Any) -> str:
    channels = _discord_request("GET", f"/guilds/{guild_id}/channels", token)
    categories: dict[str, dict[str, Any]] = {}
    uncategorized: list[dict[str, Any]] = []

    for ch in channels:
        if ch["type"] == 4:
            categories[ch["id"]] = {
                "id": ch["id"], "name": ch["name"],
                "position": ch.get("position", 0), "channels": [],
            }

    for ch in channels:
        if ch["type"] == 4:
            continue
        entry = {
            "id": ch["id"], "name": ch.get("name", ""),
            "type": _channel_type_name(ch["type"]),
            "position": ch.get("position", 0),
            "topic": ch.get("topic"), "nsfw": ch.get("nsfw", False),
        }
        parent = ch.get("parent_id")
        if parent and parent in categories:
            categories[parent]["channels"].append(entry)
        else:
            uncategorized.append(entry)

    sorted_cats = sorted(categories.values(), key=lambda c: c["position"])
    for cat in sorted_cats:
        cat["channels"].sort(key=lambda c: c["position"])
    uncategorized.sort(key=lambda c: c["position"])

    result: list[dict[str, Any]] = []
    if uncategorized:
        result.append({"category": None, "channels": uncategorized})
    for cat in sorted_cats:
        result.append({
            "category": {"id": cat["id"], "name": cat["name"]},
            "channels": cat["channels"],
        })

    total = sum(len(g["channels"]) for g in result)
    return json.dumps({"channel_groups": result, "total_channels": total})


def _channel_info(token: str, *, channel_id: str, **_kw: Any) -> str:
    ch = _discord_request("GET", f"/channels/{channel_id}", token)
    return json.dumps({
        "id": ch["id"], "name": ch.get("name"),
        "type": _channel_type_name(ch["type"]),
        "guild_id": ch.get("guild_id"),
        "topic": ch.get("topic"), "nsfw": ch.get("nsfw", False),
        "position": ch.get("position"), "parent_id": ch.get("parent_id"),
        "rate_limit_per_user": ch.get("rate_limit_per_user", 0),
        "last_message_id": ch.get("last_message_id"),
    })


def _list_roles(token: str, *, guild_id: str, **_kw: Any) -> str:
    roles = _discord_request("GET", f"/guilds/{guild_id}/roles", token)
    out = []
    for r in sorted(roles, key=lambda r: r.get("position", 0), reverse=True):
        out.append({
            "id": r["id"], "name": r["name"],
            "color": f"#{r.get('color', 0):06x}" if r.get("color") else None,
            "position": r.get("position", 0),
            "mentionable": r.get("mentionable", False),
            "managed": r.get("managed", False),
            "member_count": r.get("member_count"),
            "hoist": r.get("hoist", False),
        })
    return json.dumps({"roles": out, "count": len(out)})


def _member_info(token: str, *, guild_id: str, user_id: str, **_kw: Any) -> str:
    m = _discord_request("GET", f"/guilds/{guild_id}/members/{user_id}", token)
    user = m.get("user", {})
    return json.dumps({
        "user_id": user.get("id"),
        "username": user.get("username"),
        "display_name": user.get("global_name"),
        "nickname": m.get("nick"),
        "avatar": user.get("avatar"),
        "bot": user.get("bot", False),
        "roles": m.get("roles", []),
        "joined_at": m.get("joined_at"),
        "premium_since": m.get("premium_since"),
    })


def _search_members(
    token: str, *, guild_id: str, query: str, limit: int = 20, **_kw: Any,
) -> str:
    params = {"query": query, "limit": str(min(limit, 100))}
    members = _discord_request("GET", f"/guilds/{guild_id}/members/search", token, params=params)
    out = []
    for m in members:
        user = m.get("user", {})
        out.append({
            "user_id": user.get("id"),
            "username": user.get("username"),
            "display_name": user.get("global_name"),
            "nickname": m.get("nick"),
            "bot": user.get("bot", False),
            "roles": m.get("roles", []),
        })
    return json.dumps({"members": out, "count": len(out)})


def _fetch_messages(
    token: str, *, channel_id: str, limit: int = 50,
    before: str = "", after: str = "", **_kw: Any,
) -> str:
    params: dict[str, str] = {"limit": str(min(limit, 100))}
    if before:
        params["before"] = before
    if after:
        params["after"] = after
    messages = _discord_request("GET", f"/channels/{channel_id}/messages", token, params=params)
    out = []
    for msg in messages:
        author = msg.get("author", {})
        out.append({
            "id": msg["id"],
            "content": msg.get("content", ""),
            "author": {
                "id": author.get("id"),
                "username": author.get("username"),
                "display_name": author.get("global_name"),
                "bot": author.get("bot", False),
            },
            "timestamp": msg.get("timestamp"),
            "edited_timestamp": msg.get("edited_timestamp"),
            "attachments": [
                {"filename": a.get("filename"), "url": a.get("url"), "size": a.get("size")}
                for a in msg.get("attachments", [])
            ],
            "reactions": [
                {"emoji": r.get("emoji", {}).get("name"), "count": r.get("count", 0)}
                for r in msg.get("reactions", [])
            ] if msg.get("reactions") else [],
            "pinned": msg.get("pinned", False),
        })
    return json.dumps({"messages": out, "count": len(out)})


def _list_pins(token: str, *, channel_id: str, **_kw: Any) -> str:
    messages = _discord_request("GET", f"/channels/{channel_id}/pins", token)
    out = []
    for msg in messages:
        author = msg.get("author", {})
        out.append({
            "id": msg["id"],
            "content": msg.get("content", "")[:200],
            "author": author.get("username"),
            "timestamp": msg.get("timestamp"),
        })
    return json.dumps({"pinned_messages": out, "count": len(out)})


def _pin_message(token: str, *, channel_id: str, message_id: str, **_kw: Any) -> str:
    _discord_request("PUT", f"/channels/{channel_id}/pins/{message_id}", token)
    return json.dumps({"success": True, "message": f"Message {message_id} pinned."})


def _unpin_message(token: str, *, channel_id: str, message_id: str, **_kw: Any) -> str:
    _discord_request("DELETE", f"/channels/{channel_id}/pins/{message_id}", token)
    return json.dumps({"success": True, "message": f"Message {message_id} unpinned."})


def _create_thread(
    token: str, *, channel_id: str, name: str,
    message_id: str = "", auto_archive_duration: int = 1440, **_kw: Any,
) -> str:
    if message_id:
        path = f"/channels/{channel_id}/messages/{message_id}/threads"
        body: dict[str, Any] = {"name": name, "auto_archive_duration": auto_archive_duration}
    else:
        path = f"/channels/{channel_id}/threads"
        body = {
            "name": name, "auto_archive_duration": auto_archive_duration,
            "type": 11,  # PUBLIC_THREAD
        }
    thread = _discord_request("POST", path, token, body=body)
    return json.dumps({
        "success": True,
        "thread_id": thread["id"],
        "name": thread.get("name"),
    })


def _add_role(token: str, *, guild_id: str, user_id: str, role_id: str, **_kw: Any) -> str:
    _discord_request("PUT", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}", token)
    return json.dumps({"success": True, "message": f"Role {role_id} added to user {user_id}."})


def _remove_role(token: str, *, guild_id: str, user_id: str, role_id: str, **_kw: Any) -> str:
    _discord_request("DELETE", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}", token)
    return json.dumps({"success": True, "message": f"Role {role_id} removed from user {user_id}."})


# ─── Action manifest + dispatch ─────────────────────────────────

_ACTIONS = {
    "list_guilds": _list_guilds,
    "server_info": _server_info,
    "list_channels": _list_channels,
    "channel_info": _channel_info,
    "list_roles": _list_roles,
    "member_info": _member_info,
    "search_members": _search_members,
    "fetch_messages": _fetch_messages,
    "list_pins": _list_pins,
    "pin_message": _pin_message,
    "unpin_message": _unpin_message,
    "create_thread": _create_thread,
    "add_role": _add_role,
    "remove_role": _remove_role,
}

_ACTION_MANIFEST: list[tuple[str, str, str]] = [
    ("list_guilds", "()", "list servers the bot is in"),
    ("server_info", "(guild_id)", "server details + member counts"),
    ("list_channels", "(guild_id)", "all channels grouped by category"),
    ("channel_info", "(channel_id)", "single channel details"),
    ("list_roles", "(guild_id)", "roles sorted by position"),
    ("member_info", "(guild_id, user_id)", "lookup a specific member"),
    ("search_members", "(guild_id, query)", "find members by name prefix"),
    ("fetch_messages", "(channel_id)", "recent messages; optional before/after snowflakes"),
    ("list_pins", "(channel_id)", "pinned messages in a channel"),
    ("pin_message", "(channel_id, message_id)", "pin a message"),
    ("unpin_message", "(channel_id, message_id)", "unpin a message"),
    ("create_thread", "(channel_id, name)", "create a public thread; optional message_id anchor"),
    ("add_role", "(guild_id, user_id, role_id)", "assign a role"),
    ("remove_role", "(guild_id, user_id, role_id)", "remove a role"),
]

_INTENT_GATED_MEMBERS = frozenset({"member_info", "search_members"})

_READ_ACTIONS = frozenset({
    "list_guilds", "server_info", "list_channels", "channel_info", "list_roles",
    "member_info", "search_members", "fetch_messages", "list_pins",
})

_WRITE_ACTIONS = frozenset({
    "pin_message", "unpin_message", "create_thread", "add_role", "remove_role",
})

_REQUIRED_PARAMS: dict[str, list[str]] = {
    "server_info": ["guild_id"],
    "list_channels": ["guild_id"],
    "list_roles": ["guild_id"],
    "member_info": ["guild_id", "user_id"],
    "search_members": ["guild_id", "query"],
    "channel_info": ["channel_id"],
    "fetch_messages": ["channel_id"],
    "list_pins": ["channel_id"],
    "pin_message": ["channel_id", "message_id"],
    "unpin_message": ["channel_id", "message_id"],
    "create_thread": ["channel_id", "name"],
    "add_role": ["guild_id", "user_id", "role_id"],
    "remove_role": ["guild_id", "user_id", "role_id"],
}


# ─── Config-driven allowlist ────────────────────────────────────

def _load_allowed_actions_config() -> list[str] | None:
    """Read ``discord.server_actions`` from OC config.

    Returns a list of action names, or None if unset (= all allowed).
    Reads raw YAML to avoid forcing a Discord field into the Config dataclass.
    """
    try:
        from opencomputer.agent.config_store import config_file_path
        cfg_path = config_file_path()
        if not cfg_path.exists():
            return None
        import yaml
        raw = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        _log.debug("discord_server: could not load config (%s); allowing all actions.", exc)
        return None

    discord_block = raw.get("discord") or {}
    actions = discord_block.get("server_actions")
    if actions is None or actions == "":
        return None

    if isinstance(actions, str):
        names = [n.strip() for n in actions.split(",") if n.strip()]
    elif isinstance(actions, (list, tuple)):
        names = [str(n).strip() for n in actions if str(n).strip()]
    else:
        _log.warning(
            "discord.server_actions: unexpected type %s; ignoring.",
            type(actions).__name__,
        )
        return None

    valid = [n for n in names if n in _ACTIONS]
    invalid = [n for n in names if n not in _ACTIONS]
    if invalid:
        _log.warning(
            "discord.server_actions: unknown action(s) ignored: %s. Known: %s",
            ", ".join(invalid), ", ".join(_ACTIONS.keys()),
        )
    return valid


def _available_actions(
    caps: dict[str, Any], allowlist: list[str] | None,
) -> list[str]:
    actions: list[str] = []
    for name in _ACTIONS:
        if not caps.get("has_members_intent", True) and name in _INTENT_GATED_MEMBERS:
            continue
        if allowlist is not None and name not in allowlist:
            continue
        actions.append(name)
    return actions


# ─── Schema construction ────────────────────────────────────────

def _build_description(actions: list[str], caps: dict[str, Any]) -> str:
    manifest_lines = [
        f"  {name}{sig}  — {desc}"
        for name, sig, desc in _ACTION_MANIFEST
        if name in actions
    ]
    manifest_block = "\n".join(manifest_lines)

    content_note = ""
    if caps.get("detected") and caps.get("has_message_content") is False:
        content_note = (
            "\n\nNOTE: Bot does NOT have the MESSAGE_CONTENT privileged intent. "
            "fetch_messages and list_pins return metadata (author, timestamps, "
            "attachments, reactions, pin state) but `content` will be empty for "
            "messages not addressed to the bot or in DMs."
        )

    return (
        "Query and manage a Discord server via the REST API.\n\n"
        "Available actions:\n"
        f"{manifest_block}\n\n"
        "Call list_guilds first to discover guild_ids, then list_channels "
        "for channel_ids. Runtime errors will tell you if the bot lacks a "
        "specific per-guild permission (e.g. MANAGE_ROLES for add_role)."
        f"{content_note}"
    )


def _build_parameters(actions: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": actions or list(_ACTIONS)},
            "guild_id": {"type": "string", "description": "Discord server (guild) ID."},
            "channel_id": {"type": "string", "description": "Discord channel ID."},
            "user_id": {"type": "string", "description": "Discord user ID."},
            "role_id": {"type": "string", "description": "Discord role ID."},
            "message_id": {"type": "string", "description": "Discord message ID."},
            "query": {"type": "string", "description": "Member name prefix (search_members)."},
            "name": {"type": "string", "description": "New thread name (create_thread)."},
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 100,
                "description": "Max results (default 50). Applies to fetch_messages, search_members.",
            },
            "before": {"type": "string", "description": "Snowflake for reverse pagination (fetch_messages)."},
            "after": {"type": "string", "description": "Snowflake for forward pagination (fetch_messages)."},
            "auto_archive_duration": {
                "type": "integer", "enum": [60, 1440, 4320, 10080],
                "description": "Thread archive duration minutes (create_thread, default 1440).",
            },
        },
        "required": ["action"],
    }


# ─── 403 error enrichment ───────────────────────────────────────

_ACTION_403_HINT: dict[str, str] = {
    "pin_message": (
        "Bot lacks MANAGE_MESSAGES permission in this channel. "
        "Ask the server admin to grant the bot a role with MANAGE_MESSAGES."
    ),
    "unpin_message": "Bot lacks MANAGE_MESSAGES permission in this channel.",
    "create_thread": (
        "Bot lacks CREATE_PUBLIC_THREADS in this channel, or cannot view it."
    ),
    "add_role": (
        "Either the bot lacks MANAGE_ROLES, or the target role sits higher "
        "than the bot's highest role."
    ),
    "remove_role": (
        "Either the bot lacks MANAGE_ROLES, or the target role sits higher "
        "than the bot's highest role."
    ),
    "fetch_messages": (
        "Bot cannot view this channel (missing VIEW_CHANNEL or READ_MESSAGE_HISTORY)."
    ),
    "list_pins": (
        "Bot cannot view this channel (missing VIEW_CHANNEL or READ_MESSAGE_HISTORY)."
    ),
    "channel_info": "Bot cannot view this channel (missing VIEW_CHANNEL).",
    "search_members": (
        "Likely missing the Server Members privileged intent — enable it in the "
        "Discord Developer Portal."
    ),
    "member_info": (
        "Bot cannot see this guild member (missing Server Members intent or "
        "insufficient permissions)."
    ),
}


def _enrich_403(action: str, body: str) -> str:
    base = f"Discord API 403 (forbidden) on '{action}'."
    hint = _ACTION_403_HINT.get(action)
    if hint:
        return f"{base} {hint} (Raw: {body})"
    return f"{base} (Raw: {body})"


# ─── BaseTool ───────────────────────────────────────────────────

class DiscordServerTool(BaseTool):
    """Discord server introspection + management — single dispatch tool.

    14 actions across read (list, info) + write (pin, role, thread). Both
    capability claims are declared so F1 can route each action to its
    appropriate consent tier.
    """

    consent_tier: int = 2  # default to write tier; reads still allowed at tier 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="discord.read",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Read Discord server, channel, role, and message metadata.",
        ),
        CapabilityClaim(
            capability_id="discord.manage",
            tier_required=ConsentTier.EXPLICIT,
            human_description="Mutate Discord server state (pin, threads, role assignment).",
        ),
    )

    def __init__(
        self, *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        token = _get_bot_token()
        if token:
            caps = _detect_capabilities(token)
            allowlist = _load_allowed_actions_config()
            actions = _available_actions(caps, allowlist) or list(_ACTIONS)
        else:
            caps = {"detected": False}
            actions = list(_ACTIONS)
        return ToolSchema(
            name="discord_server",
            description=_build_description(actions, caps),
            parameters=_build_parameters(actions),
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        action = args.get("action", "")
        token = _get_bot_token()

        if not token:
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({"error": "DISCORD_BOT_TOKEN not configured."}),
                is_error=True,
            )

        action_fn = _ACTIONS.get(action)
        if not action_fn:
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({
                    "error": f"Unknown action: {action!r}",
                    "available_actions": list(_ACTIONS),
                }),
                is_error=True,
            )

        # Defense-in-depth: re-check allowlist + intents at dispatch time.
        caps = _detect_capabilities(token)
        allowlist = _load_allowed_actions_config()
        if action not in _available_actions(caps, allowlist):
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({
                    "error": (
                        f"Action '{action}' not available in this session "
                        "(disabled by config or missing privileged intent)."
                    ),
                }),
                is_error=True,
            )

        missing = [p for p in _REQUIRED_PARAMS.get(action, []) if not args.get(p)]
        if missing:
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({
                    "error": f"Missing required parameters for '{action}': {', '.join(missing)}",
                }),
                is_error=True,
            )

        try:
            content = action_fn(
                token=token,
                guild_id=args.get("guild_id", ""),
                channel_id=args.get("channel_id", ""),
                user_id=args.get("user_id", ""),
                role_id=args.get("role_id", ""),
                message_id=args.get("message_id", ""),
                query=args.get("query", ""),
                name=args.get("name", ""),
                limit=int(args.get("limit") or 50),
                before=args.get("before", ""),
                after=args.get("after", ""),
                auto_archive_duration=int(args.get("auto_archive_duration") or 1440),
            )
            return ToolResult(tool_call_id=call.id, content=content)
        except DiscordAPIError as e:
            _log.warning("Discord API error in '%s': %s", action, e)
            payload = {"error": _enrich_403(action, e.body) if e.status == 403 else str(e)}
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps(payload),
                is_error=True,
            )
        except Exception as e:  # noqa: BLE001
            _log.exception("Unexpected error in discord_server '%s'", action)
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({"error": f"Unexpected error: {e}"}),
                is_error=True,
            )


__all__ = [
    "DiscordAPIError",
    "DiscordServerTool",
    "_ACTIONS",
    "_READ_ACTIONS",
    "_WRITE_ACTIONS",
    "_available_actions",
    "_build_description",
    "_build_parameters",
    "_detect_capabilities",
    "_enrich_403",
    "_load_allowed_actions_config",
    "_reset_capability_cache",
]
