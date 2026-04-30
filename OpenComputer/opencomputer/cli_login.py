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


_PROVIDER_PRETTY: dict[str, str] = {
    "anthropic":  "Anthropic",
    "openai":     "OpenAI",
    "groq":       "Groq",
    "openrouter": "OpenRouter",
    "google":     "Google",
    "gemini":     "Google",
    "deepseek":   "DeepSeek",
    "mistral":    "Mistral",
}


def _resolve_active_provider() -> str | None:
    """Return the currently configured provider name (None if cfg unloadable)."""
    try:
        from opencomputer.agent.config_store import load_config
        return load_config().model.provider
    except Exception:  # noqa: BLE001
        return None


def login(provider: str) -> None:
    """Prompt for an API key and store it in ``~/.opencomputer/<profile>/.env``.

    Hermes-exact prompt copy: ``"<Provider> API key (or Enter to cancel): "``
    via ``getpass.getpass`` (echo off). On empty/cancel: ``"Cancelled."``.
    On success: ``"API key saved."``. No test API call (matches Hermes —
    validation deferred to first real LLM call).
    """
    name = provider.strip().lower()
    if name not in PROVIDER_ENV_MAP:
        valid = ", ".join(sorted(PROVIDER_ENV_MAP.keys()))
        console.print(f"Unknown provider: {provider}")
        console.print(f"Valid providers: {valid}")
        raise typer.Exit(1)

    env_var = PROVIDER_ENV_MAP[name]
    pretty = _PROVIDER_PRETTY.get(name, name.capitalize())

    # Hermes uses getpass directly (`hermes_cli/auth.py:2029`) for
    # zero-echo input. typer.prompt(hide_input=True) is the same on
    # POSIX, but getpass is the canonical Hermes call.
    import getpass
    try:
        key = getpass.getpass(f"{pretty} API key (or Enter to cancel): ")
    except (EOFError, KeyboardInterrupt):
        console.print("\nCancelled.")
        raise typer.Exit(0) from None

    if not key.strip():
        console.print("Cancelled.")
        raise typer.Exit(0)

    home = _profile_home()
    home.mkdir(parents=True, exist_ok=True)
    env_path = home / ".env"
    _upsert_env_var(env_path, env_var, key.strip())

    console.print("API key saved.")
    console.print()


def logout(provider: str | None = None) -> None:
    """Clear the stored API key for ``provider`` from ``.env``.

    Hermes-exact behaviour: when called with no argument, derives the
    target from the active config's provider field (``hermes_cli/auth.py:3488``).
    Output: ``Logged out of <ProviderName>.`` plus a follow-up hint
    telling the user how to restore inference.
    """
    if provider is None or not provider.strip():
        active = _resolve_active_provider()
        if not active:
            console.print("No provider is currently logged in.")
            raise typer.Exit(0)
        provider = active

    name = provider.strip().lower()
    if name not in PROVIDER_ENV_MAP:
        console.print(f"Unknown provider: {provider}")
        raise typer.Exit(1)

    env_var = PROVIDER_ENV_MAP[name]
    pretty = _PROVIDER_PRETTY.get(name, name.capitalize())
    home = _profile_home()
    env_path = home / ".env"

    if not env_path.exists():
        console.print(f"No auth state found for {pretty}.")
        raise typer.Exit(0)

    removed = _remove_env_var(env_path, env_var)
    if not removed:
        console.print(f"No auth state found for {pretty}.")
        raise typer.Exit(0)

    console.print(f"Logged out of {pretty}.")
    # Hermes prints a follow-up hint based on whether OPENROUTER_API_KEY
    # is still present in the environment (auth.py:3585).
    import os as _os
    if _os.environ.get("OPENROUTER_API_KEY"):
        console.print("OpenComputer will use OpenRouter for inference.")
    else:
        console.print("Run `oc model` or configure an API key to use OpenComputer.")


__all__ = [
    "PROVIDER_ENV_MAP",
    "login",
    "logout",
    "_upsert_env_var",
    "_remove_env_var",
]
