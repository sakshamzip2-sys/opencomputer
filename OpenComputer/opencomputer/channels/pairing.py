"""Channel pairing — guided credential setup for chat channels.

Phase 1.3 of the catch-up plan (real-gui-velvet-lemur). Closes the
"copy this env var into your shell rc" gap for first-time users.

What `opencomputer pair <platform>` does:

1. Prompts (or accepts via flag) for the platform's secret.
2. Validates the secret format with a strict regex (no shell metachars,
   never logged to stdout in full).
3. Optionally calls the platform's identity endpoint as a live check
   (Telegram: ``getMe``; skipped for Discord/Slack which don't have a
   cheap public probe — format validation + later usage is the gate).
4. Writes the secret to ``~/.opencomputer/<profile>/secrets/<n>.token``
   (chmod 0600). This is the canonical store; future Phase 14.F
   credential isolation will read from here.
5. Auto-grants the matching ``channel.send.<platform>`` consent
   capability at EXPLICIT tier (so the user doesn't have to run a
   second ``opencomputer consent grant`` afterwards).
6. Prints the env-var export the user can run today (until Phase 14.F
   per-profile env loading lands), so the existing channel adapters
   keep working unchanged.

Secret storage rationale: writing to a file under the profile dir is
deliberate. The future Phase 14.F per-profile ``.env`` loader will read
from a file just like this; this commit doesn't ship the loader (out of
scope) but it's safe to land the file format because no adapter reads
from it yet — adapters still use env vars.

This module is the *registry* of supported pairers. The CLI shim lives
in ``opencomputer/cli_pair.py``.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Pairer:
    """Definition of how to pair a single chat channel."""

    platform: str
    """Lowercase platform id ('telegram', 'discord', 'slack', ...)."""

    secret_filename: str
    """Filename under <profile>/secrets/. Includes the extension; we use
    `.token` for tokens and `.secret` for compound credentials."""

    env_var_name: str
    """Existing env var the channel adapter reads. We don't change the
    adapter — pair just stores the secret + tells the user the name."""

    capability_id: str
    """The consent capability auto-granted on successful pair."""

    format_regex: re.Pattern[str]
    """Strict format validator. Run before any IO."""

    live_check: Callable[[str], bool] | None = None
    """Optional API probe. Return True if reachable, False otherwise.
    Format-valid-but-unreachable is still accepted (network may be down,
    proxy may block) — we record but don't refuse."""

    instructions: str = ""
    """Human hint about where to get the secret."""

    def validate_format(self, secret: str) -> None:
        if not secret:
            raise ValueError(f"{self.platform}: secret is empty")
        if not self.format_regex.fullmatch(secret):
            raise ValueError(
                f"{self.platform}: secret does not match expected format"
            )


# ---------- Per-platform live-check helpers ----------


def _telegram_get_me(token: str) -> bool:
    """Hit Telegram getMe to confirm the token is valid + the bot exists."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 — fixed scheme
            data = json.load(r)
            return bool(data.get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------- Concrete pairers ----------


PAIRERS: dict[str, Pairer] = {
    "telegram": Pairer(
        platform="telegram",
        secret_filename="telegram.token",
        env_var_name="TELEGRAM_BOT_TOKEN",
        capability_id="channel.send.telegram",
        # Telegram bot tokens: <numeric-bot-id>:<35-46 alphanumeric chars + _ ->
        format_regex=re.compile(r"\d+:[A-Za-z0-9_-]+"),
        live_check=_telegram_get_me,
        instructions=(
            "Get a token from @BotFather on Telegram: /newbot or /token. "
            "Format: 1234567890:ABCdef..."
        ),
    ),
    "discord": Pairer(
        platform="discord",
        secret_filename="discord.token",
        env_var_name="DISCORD_BOT_TOKEN",
        capability_id="channel.send.discord",
        # Discord bot tokens are base64-ish: 3 dot-separated segments,
        # ~24 / 6 / 27+ chars, but Discord has rotated formats so we just
        # require dot-separated tokens with non-trivial length.
        format_regex=re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}"),
        live_check=None,  # no cheap public probe
        instructions=(
            "Get a token from https://discord.com/developers/applications → "
            "your app → Bot → Reset Token. Enable 'message_content' intent."
        ),
    ),
    "slack": Pairer(
        platform="slack",
        secret_filename="slack.token",
        env_var_name="SLACK_BOT_TOKEN",
        capability_id="channel.send.slack",
        # Slack bot tokens start with xoxb- followed by 50+ chars
        format_regex=re.compile(r"xoxb-[A-Za-z0-9-]{20,}"),
        live_check=None,  # auth.test would work but needs network; skip for v1
        instructions=(
            "Create a Slack app at https://api.slack.com/apps, install it to "
            "your workspace, copy the 'Bot User OAuth Token' (starts xoxb-)."
        ),
    ),
}


def write_secret(home: Path, pairer: Pairer, secret: str) -> Path:
    """Write `secret` to `<home>/secrets/<pairer.secret_filename>` chmod 0600.

    Returns the absolute path written. The parent directory is created
    if missing (also chmod 0700 for the dir itself).
    """
    secrets_dir = home / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    try:
        secrets_dir.chmod(0o700)
    except (OSError, NotImplementedError):
        # On Windows or odd FSes chmod may not work; secrets dir is best-effort.
        pass
    path = secrets_dir / pairer.secret_filename
    path.write_text(secret + "\n")
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass
    return path
