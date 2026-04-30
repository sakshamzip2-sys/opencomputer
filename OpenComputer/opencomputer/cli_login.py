"""``oc login`` / ``oc logout`` — interactive credential capture into ``.env``.

Hermes-parity Tier S (2026-04-30). Hermes ships ``hermes login`` and
``hermes logout``; OpenComputer's ``oc auth`` was read-only. These two
commands close the gap by writing/clearing API keys into the active
profile's ``~/.opencomputer/<profile>/.env`` (already loaded by Phase
14.F per CLAUDE.md), with file mode ``0600`` so the keys aren't world-
readable.

Provider names map to a fixed env-var allowlist — unknown providers
exit with a usage error rather than write arbitrary keys.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import typer
from rich.console import Console

console = Console()


# Provider name (typed by user) → environment variable name we manage.
# Add new entries when a provider plugin lands.
PROVIDER_ENV_MAP: dict[str, str] = {
    "anthropic":  "ANTHROPIC_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "groq":       "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "google":     "GOOGLE_API_KEY",
    "gemini":     "GOOGLE_API_KEY",         # alias
    "deepseek":   "DEEPSEEK_API_KEY",
    "mistral":    "MISTRAL_API_KEY",
}


def _profile_home() -> Path:
    """Resolve the active-profile home dir for ``.env`` placement."""
    from opencomputer.agent.config import _home as _resolve_home
    return _resolve_home()


def _upsert_env_var(env_path: Path, var_name: str, value: str) -> None:
    """Write ``var_name=value`` into ``.env``. Replace existing line, or
    append. Preserves comments and other variables. Atomic via temp-file
    swap so a partial write can't corrupt the file.
    """
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    new_lines: list[str] = []
    replaced = False
    prefix = f"{var_name}="
    for line in existing_lines:
        # Match ``VAR=...`` (with or without ``export`` prefix), preserving
        # case-sensitive variable name. Lines that don't match are kept.
        stripped = line.lstrip()
        if stripped.startswith(prefix) or stripped.startswith(f"export {prefix}"):
            new_lines.append(f"{var_name}={value}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"{var_name}={value}")

    body = "\n".join(new_lines) + "\n"
    # Atomic swap: write to temp file in same dir, then rename.
    fd, tmp = tempfile.mkstemp(
        prefix=".env.", suffix=".tmp", dir=str(env_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _remove_env_var(env_path: Path, var_name: str) -> bool:
    """Remove the matching line from ``.env``. Returns True if a line was
    removed, False if the variable wasn't present."""
    if not env_path.exists():
        return False
    existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    new_lines: list[str] = []
    removed = False
    prefix = f"{var_name}="
    for line in existing_lines:
        stripped = line.lstrip()
        if stripped.startswith(prefix) or stripped.startswith(f"export {prefix}"):
            removed = True
            continue
        new_lines.append(line)

    if not removed:
        return False

    body = "\n".join(new_lines) + ("\n" if new_lines else "")
    fd, tmp = tempfile.mkstemp(
        prefix=".env.", suffix=".tmp", dir=str(env_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def login(provider: str) -> None:
    """Prompt for an API key and store it in ``~/.opencomputer/<profile>/.env``."""
    name = provider.strip().lower()
    if name not in PROVIDER_ENV_MAP:
        valid = ", ".join(sorted(PROVIDER_ENV_MAP.keys()))
        console.print(
            f"[bold red]Unknown provider:[/bold red] {provider!r}.\n"
            f"Valid providers: {valid}"
        )
        raise typer.Exit(2)

    env_var = PROVIDER_ENV_MAP[name]
    key = typer.prompt(
        f"Enter {name} API key (won't echo)",
        hide_input=True,
        default="",
        show_default=False,
    )
    if not key.strip():
        console.print("[red]Empty key — aborting.[/red]")
        raise typer.Exit(1)

    home = _profile_home()
    home.mkdir(parents=True, exist_ok=True)
    env_path = home / ".env"
    _upsert_env_var(env_path, env_var, key.strip())

    console.print(f"[green]✓[/green] Stored {env_var} in {env_path}")


def logout(provider: str) -> None:
    """Clear the stored API key for ``provider`` from ``.env``."""
    name = provider.strip().lower()
    if name not in PROVIDER_ENV_MAP:
        valid = ", ".join(sorted(PROVIDER_ENV_MAP.keys()))
        console.print(
            f"[bold red]Unknown provider:[/bold red] {provider!r}.\n"
            f"Valid providers: {valid}"
        )
        raise typer.Exit(2)

    env_var = PROVIDER_ENV_MAP[name]
    home = _profile_home()
    env_path = home / ".env"

    if not env_path.exists():
        console.print("[dim]No credentials stored.[/dim]")
        raise typer.Exit(0)

    if _remove_env_var(env_path, env_var):
        console.print(f"[green]✓[/green] Cleared {env_var} from {env_path}")
    else:
        console.print(f"[dim]({env_var} was not stored)[/dim]")


__all__ = [
    "PROVIDER_ENV_MAP",
    "login",
    "logout",
    "_upsert_env_var",
    "_remove_env_var",
]
