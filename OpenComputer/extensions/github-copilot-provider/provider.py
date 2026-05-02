"""GitHub Copilot provider — thin subclass of OpenAIProvider.

GitHub Copilot's API is OpenAI-compatible at api.githubcopilot.com.
Authentication uses a GitHub user token — Hermes accepts three
synonymous env vars (COPILOT_GITHUB_TOKEN, GH_TOKEN, GITHUB_TOKEN) plus
falls back to reading the ``gh`` CLI's stored token.

Verified against Hermes auth.py:
  inference_base_url="https://api.githubcopilot.com",
  api_key_env_vars=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),

Token resolution order (highest precedence first):
  1. ``COPILOT_GITHUB_TOKEN`` env var
  2. ``GH_TOKEN`` env var
  3. ``GITHUB_TOKEN`` env var
  4. ``gh`` CLI's stored token (``~/.config/gh/hosts.yml``)
  5. Raise — provider unusable

Env vars:
  COPILOT_GITHUB_TOKEN — preferred; GitHub PAT or OAuth token with
                         ``copilot`` scope
  GH_TOKEN              — gh CLI's standard token env var
  GITHUB_TOKEN          — generic GitHub Actions / app token
  COPILOT_API_BASE_URL  — optional override (default: api.githubcopilot.com)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_COPILOT_BASE_URL = "https://api.githubcopilot.com"


def _read_gh_cli_token() -> str | None:
    """Best-effort read of the gh CLI's stored token from ``~/.config/gh/hosts.yml``.

    Returns the token for ``github.com`` if found, else None. Never
    raises — gh may not be installed.
    """
    hosts_path = Path.home() / ".config" / "gh" / "hosts.yml"
    if not hosts_path.exists():
        return None
    try:
        text = hosts_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    # Minimal yaml-ish parser — gh hosts.yml is well-defined enough that
    # we can grep for ``oauth_token: <value>`` under ``github.com:`` block.
    in_github_block = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line == "github.com:":
            in_github_block = True
            continue
        if in_github_block:
            stripped = line.lstrip()
            if not line.startswith(" ") and not line.startswith("\t") and stripped:
                # Left the github.com block (next host)
                in_github_block = False
                continue
            if stripped.startswith("oauth_token:"):
                value = stripped.split(":", 1)[1].strip()
                # Strip surrounding quotes if present
                if value and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                return value or None
    return None


class GitHubCopilotProvider(OpenAIProvider):
    name = "copilot"
    default_model = "gpt-4o"
    _api_key_env: str = "COPILOT_GITHUB_TOKEN"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        # Token resolution order: explicit api_key → 3 env vars → gh CLI
        if not api_key:
            for env in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
                api_key = os.environ.get(env)
                if api_key:
                    break
        if not api_key:
            api_key = _read_gh_cli_token()
        if not api_key:
            raise RuntimeError(
                "GitHub Copilot needs a GitHub token. Set "
                "COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN, or run "
                "`gh auth login` to store one in the gh CLI. "
                "Token must have `copilot` scope; see "
                "https://docs.github.com/en/copilot."
            )

        resolved_base = (
            base_url
            or os.environ.get("COPILOT_API_BASE_URL")
            or DEFAULT_COPILOT_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)
