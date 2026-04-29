"""``/debug`` — sanitized diagnostic dump for bug reports.

Outputs a markdown block with:
  - Python + OS + opencomputer versions
  - Active profile + sticky setting
  - Loaded plugins (id + kind)
  - Configured provider + model
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


def build_debug_dump() -> str:
    """Return a markdown block of sanitized diagnostics."""
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

    # Configured model
    lines.append("")
    lines.append("=== Config ===")
    try:
        from opencomputer.agent.config_store import load_config

        cfg = load_config()
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
