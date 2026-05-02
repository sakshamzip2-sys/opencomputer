"""GitHub Copilot ACP provider — auth/discovery scaffold.

Status: **scaffold shipped, ACP transport pending**.

ACP (Agent Communication Protocol) is GitHub's JSON-RPC-over-stdio shape.
The Copilot CLI (``copilot --acp --stdio``) spawns as a long-lived
subprocess and exchanges bidirectional JSON-RPC messages on stdin/stdout —
no HTTP, no OpenAI compat layer.

This plugin currently:

  - registers a provider entry so the wizard can discover ``copilot-acp``
  - resolves and validates the ``copilot`` CLI binary path at construction
    (env overrides honored: ``OPENCOMPUTER_COPILOT_ACP_COMMAND`` and
    legacy ``COPILOT_CLI_PATH``)
  - raises a clear ``NotImplementedError`` on inference, with actionable
    guidance pointing at the REST-shaped ``copilot`` provider as today's
    workaround

To set up:

  1. Install the GitHub Copilot CLI (``gh extension install github/gh-copilot``
     or the standalone ``copilot`` binary).
  2. Run ``copilot login`` once to obtain a GitHub OAuth session.
  3. Wait for the ACP transport adapter (tracked in the onboarding roadmap).

For full Copilot inference today: install the ``copilot`` plugin (PR #331)
which uses GitHub OAuth tokens against ``api.githubcopilot.com`` over the
OpenAI-compatible REST shape.

Env vars:
  OPENCOMPUTER_COPILOT_ACP_COMMAND  — override the binary path
  OPENCOMPUTER_COPILOT_ACP_ARGS     — override the args (default '--acp --stdio')
  COPILOT_CLI_PATH                  — Hermes-compat alias for the path
"""
from __future__ import annotations

import importlib.util as _importlib_util
import os
import shlex
import shutil
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"

# Load extensions/openai-provider/provider.py under a unique module name
# to avoid sys.modules['provider'] collision when multiple
# OpenAI-compat providers are loaded in the same process
# (PR #353 fix for zai-provider/openrouter-provider, extended here).
_spec = _importlib_util.spec_from_file_location(
    "_oai_base_for_copilot_acp", str(_OPENAI_PROVIDER_DIR / "provider.py")
)
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
OpenAIProvider = _mod.OpenAIProvider

# Marker URL — keeps the OpenAI HTTP shape from accidentally firing
# against any real endpoint. The base_url isn't actually used by the
# subprocess transport; it just signals "ACP transport, not HTTP".
DEFAULT_COPILOT_ACP_BASE_URL = "acp://copilot"

DEFAULT_ARGS = ["--acp", "--stdio"]


def resolve_acp_command() -> tuple[str, list[str]]:
    """Resolve the (command, args) pair to spawn the Copilot ACP subprocess.

    Resolution order for command:
      1. ``OPENCOMPUTER_COPILOT_ACP_COMMAND`` env var (preferred)
      2. ``COPILOT_CLI_PATH`` env var (Hermes-compat alias)
      3. ``copilot`` binary on PATH

    Args default to ``["--acp", "--stdio"]``; override via
    ``OPENCOMPUTER_COPILOT_ACP_ARGS`` (shell-quoted).

    Raises ``RuntimeError`` with a helpful message if the binary isn't
    found, mentioning the install command.
    """
    command = (
        os.environ.get("OPENCOMPUTER_COPILOT_ACP_COMMAND", "").strip()
        or os.environ.get("COPILOT_CLI_PATH", "").strip()
        or "copilot"
    )
    resolved = shutil.which(command)
    if not resolved:
        raise RuntimeError(
            f"GitHub Copilot CLI not found (looked for '{command}'). "
            "Install via `gh extension install github/gh-copilot` or "
            "download the standalone binary, then run `copilot login`. "
            "Override the path with OPENCOMPUTER_COPILOT_ACP_COMMAND."
        )

    args_raw = os.environ.get("OPENCOMPUTER_COPILOT_ACP_ARGS", "").strip()
    args = shlex.split(args_raw) if args_raw else list(DEFAULT_ARGS)
    return resolved, args


class CopilotACPProvider(OpenAIProvider):
    """GitHub Copilot ACP provider — scaffold; full transport is a follow-up."""

    name = "copilot-acp"
    default_model = "claude-3-5-sonnet"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        # Validate the CLI is installed up-front so misconfiguration fails
        # at provider-construction time, not at first inference.
        self._command, self._args = resolve_acp_command()
        # OpenAIProvider needs an api_key string; "copilot-acp" is a sentinel.
        super().__init__(
            api_key=api_key or "copilot-acp",
            base_url=base_url or DEFAULT_COPILOT_ACP_BASE_URL,
        )

    async def _post(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError(
            "GitHub Copilot ACP uses the JSON-RPC-over-stdio Agent "
            "Communication Protocol — not OpenAI-compatible HTTP. The ACP "
            "transport adapter is a focused follow-up. For full Copilot "
            "inference today, install the 'copilot' provider plugin which "
            "uses GitHub OAuth against api.githubcopilot.com over OpenAI "
            "REST shape."
        )

    async def complete(self, *args, **kwargs):  # type: ignore[override]
        return await self._post()

    async def stream_complete(self, *args, **kwargs):  # type: ignore[override]
        await self._post()
        if False:  # pragma: no cover - unreachable
            yield None


__all__ = [
    "DEFAULT_COPILOT_ACP_BASE_URL",
    "CopilotACPProvider",
    "resolve_acp_command",
]
