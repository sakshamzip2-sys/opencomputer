"""T69 — auth.json + Claude Code credentials discovery.

Two complementary surfaces for finding API credentials:

1. **``$OPENCOMPUTER_HOME/auth/auth.json``** — Hermes-doc-shaped explicit
   credentials file. Per-provider sub-objects with env-indirection
   (e.g. ``"api_key": "${ANTHROPIC_API_KEY}"``) so the file is safe to
   commit while the actual secret stays in the environment.

2. **``~/.claude/.credentials.json``** — drop-in convenience: when the
   user already has Claude Code installed and authenticated, OC reads
   the same credential file rather than asking them to re-export their
   API key.

Resolution order for any given provider:
    env var > auth.json > Claude Code creds

This keeps explicit configuration (env / auth.json) authoritative
while making first-run UX painless when Claude Code is already set up.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_ENV_INTERP_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _opencomputer_home() -> Path:
    """Resolve the active OC home directory."""
    raw = os.environ.get("OPENCOMPUTER_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".opencomputer"


def _interpolate(value: Any) -> Any:
    """Resolve ``${VAR}`` references in strings against the environment.

    Unset variables stay as the literal ``${VAR}`` so callers can detect
    a missing-cred situation rather than silently falling back to ``""``.
    """
    if not isinstance(value, str):
        return value

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_val = os.environ.get(var_name)
        return env_val if env_val else match.group(0)

    return _ENV_INTERP_PATTERN.sub(_replace, value)


def _interpolate_dict(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {k: _interpolate_dict(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_interpolate_dict(v) for v in payload]
    return _interpolate(payload)


def load_auth_json() -> dict[str, Any]:
    """Read ``$OPENCOMPUTER_HOME/auth/auth.json`` and resolve env-indirection.

    Returns an empty dict when the file is missing or unreadable. JSON
    parse errors are logged at WARNING and treated as "no creds" rather
    than crashing — auth-json is optional convenience config.
    """
    path = _opencomputer_home() / "auth" / "auth.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("auth.json unreadable at %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return _interpolate_dict(raw)


def _read_claude_code_creds() -> dict[str, Any] | None:
    """Read ``~/.claude/.credentials.json`` if present.

    Claude Code stores OAuth tokens under ``claudeAiOauth.accessToken``
    on macOS / Linux. Other shapes (older versions, MCP-style) are
    ignored — we only look for the canonical key.
    """
    path = Path(os.environ.get("HOME", "~")).expanduser() / ".claude" / ".credentials.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("claude-code creds unreadable at %s: %s", path, exc)
        return None


def discover_anthropic_credential() -> dict[str, str] | None:
    """Return the best-available Anthropic credential or ``None``.

    Returns a dict with ``api_key`` + ``source`` keys (source is one of
    ``env`` / ``auth.json`` / ``claude-code``) so callers can tell where
    the key came from for logging / setup wizard UX. Returns ``None``
    when no credential is discoverable, prompting the caller to ask the
    user to set one up explicitly.
    """
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return {"api_key": env_key, "source": "env"}

    auth_json = load_auth_json()
    anth = auth_json.get("anthropic")
    if isinstance(anth, dict):
        api_key = anth.get("api_key")
        if isinstance(api_key, str) and api_key and not api_key.startswith("${"):
            return {"api_key": api_key, "source": "auth.json"}

    cc = _read_claude_code_creds()
    if isinstance(cc, dict):
        oauth = cc.get("claudeAiOauth")
        if isinstance(oauth, dict):
            token = oauth.get("accessToken")
            if isinstance(token, str) and token:
                return {"api_key": token, "source": "claude-code"}

    return None


__all__ = [
    "discover_anthropic_credential",
    "load_auth_json",
]
