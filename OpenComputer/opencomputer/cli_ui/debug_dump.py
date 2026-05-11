"""``/debug`` — sanitized diagnostic dump for bug reports.

Outputs a markdown block with:
  - Python + OS + opencomputer versions
  - Active profile + sticky setting
  - Loaded plugins (id + kind)
  - Configured provider + model (LIVE when called from in-session slash;
    on-disk when called from outside any session)
  - Env-var presence (set/unset only — NEVER the value)
  - Recent log lines (last 20 from ``<profile_home>/agent.log`` if present)

Redaction is positive-list — env vars appear by name only, never value.
"""
from __future__ import annotations

import os
import platform
import sys
from collections import deque
from pathlib import Path

# Env-var names users typically have set; we only report set/unset, never values.
_TRACKED_ENV_VARS: tuple[str, ...] = (
    # Provider keys
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENROUTER_BASE_URL",
    # Channel adapters
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    # Search-tool keys
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    # Audio / inference
    "GROQ_API_KEY",
    # Memory provider
    "HONCHO_API_KEY",
    "HONCHO_BASE_URL",
    # OC environment
    "OPENCOMPUTER_PROFILE",
    "OPENCOMPUTER_HOME",
)


def _tail_lines(path: Path, n: int = 20) -> list[str]:
    """Bounded tail — never reads more than the last n lines into memory."""
    if not path.exists():
        return []
    try:
        with path.open("r", errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=n)]
    except OSError:
        return []


def build_debug_dump(
    live_active_model_info: tuple[str, str] | None = None,
) -> str:
    """Return a markdown block of sanitized diagnostics.

    Args:
        live_active_model_info: Optional ``(model_id, provider_name)`` tuple
            from the running AgentLoop. When provided, the Config section
            uses it (so post-``/model`` swap dumps show the LIVE model
            rather than the on-disk YAML default). When ``None``, falls
            back to ``load_config()`` from disk.

            The slash dispatcher (``cli_ui/slash_handlers.py::_handle_debug``)
            passes the value from ``ctx.get_active_model_info()`` which
            production wires to ``loop.config.model.{model,provider}``.
            Out-of-session callers (e.g. a future ``oc debug`` Typer
            command) leave it ``None`` to get on-disk state — the right
            semantic when there's no running loop to read from.
    """
    lines: list[str] = []
    lines.append("```")
    lines.append("=== OpenComputer Diagnostic ===")

    # Versions
    lines.append(f"python: {sys.version.split()[0]}")
    try:
        from opencomputer import __version__ as oc_version
    except Exception:
        oc_version = "?"
    lines.append(f"opencomputer: {oc_version}")
    lines.append(f"platform: {platform.platform()}")

    # Profile
    lines.append("")
    lines.append("=== Profile ===")
    try:
        from opencomputer.profiles import read_active_profile

        active = read_active_profile() or "default"
    except Exception:
        active = "?"
    lines.append(f"active_profile: {active}")

    # Plugins (best-effort — don't crash if loader unavailable)
    lines.append("")
    lines.append("=== Plugins ===")
    try:
        from opencomputer.plugins.registry import registry

        for name in sorted(registry.providers.keys()):
            lines.append(f"  provider: {name}")
        for name in sorted(registry.channels.keys()):
            lines.append(f"  channel: {name}")
    except Exception as exc:
        lines.append(f"  (plugin registry unavailable: {exc})")

    # Configured model — LIVE when caller passed live_active_model_info
    # (in-session /debug slash), ON-DISK YAML otherwise (no running loop).
    lines.append("")
    lines.append("=== Config ===")
    try:
        from opencomputer.agent.config_store import load_config

        cfg = load_config()
        if live_active_model_info is not None:
            live_model, live_provider = live_active_model_info
            lines.append(f"  provider: {live_provider}  (live)")
            lines.append(f"  model: {live_model}  (live)")
            # When live differs from disk, surface the on-disk default
            # too — useful for "did /model swap stick to YAML?" triage.
            if (
                live_model != cfg.model.model
                or live_provider != cfg.model.provider
            ):
                lines.append(
                    f"  on-disk default: {cfg.model.provider} / {cfg.model.model}"
                )
        else:
            lines.append(f"  provider: {cfg.model.provider}")
            lines.append(f"  model: {cfg.model.model}")
        if cfg.model.fallback_models:
            lines.append(f"  fallback: {', '.join(cfg.model.fallback_models)}")
        aliases = getattr(cfg.model, "model_aliases", None)
        if aliases:
            lines.append(f"  aliases: {sorted(aliases.keys())}")
    except Exception as exc:
        lines.append(f"  (config unloadable: {exc})")

    # Env vars — set/unset only
    lines.append("")
    lines.append("=== Env vars ===")
    for var in _TRACKED_ENV_VARS:
        state = "set" if os.environ.get(var) else "unset"
        lines.append(f"  {var}: {state}")

    # Recent log lines — agent.log lives at <profile_home>/agent.log
    lines.append("")
    lines.append("=== Recent log (last 20 lines) ===")
    try:
        from opencomputer.agent.config import default_config

        cfg = default_config()
        # System-control log path is the canonical agent.log location
        log_path = getattr(getattr(cfg, "system_control", None), "log_path", None)
        if log_path is None:
            from opencomputer.profiles import profile_home

            log_path = profile_home() / "agent.log"
        tail = _tail_lines(Path(log_path), n=20)
        if tail:
            lines.extend(f"  {t}" for t in tail)
        else:
            # Don't print the resolved path — it can embed OPENCOMPUTER_HOME
            # or other env-controlled prefixes; the report is set/unset only.
            lines.append("  (no log present)")
    except Exception as exc:
        lines.append(f"  (log unreadable: {exc})")

    lines.append("```")
    return "\n".join(lines)
