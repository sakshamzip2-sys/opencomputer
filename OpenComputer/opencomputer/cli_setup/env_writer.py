"""Read/write env-var values to ``~/.opencomputer/.env`` (or per-profile).

Used by the wizard's API-key entry flow. Keeps perms 0600 on every
write. Preserves comments + unrelated lines on update.

Modeled after Hermes's ``hermes_cli/auth.py::save_env_value`` /
``get_env_value`` pair — independently re-implemented for OC's
config layout.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path


def default_env_file() -> Path:
    """Return path to the active profile's .env file.

    OPENCOMPUTER_HOME wins when set (per-profile setup); else falls
    back to ``~/.opencomputer/.env``.
    """
    home = os.environ.get("OPENCOMPUTER_HOME")
    if home:
        return Path(home) / ".env"
    return Path.home() / ".opencomputer" / ".env"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-style file into a dict.

    Handles ``KEY=value``, ``KEY="quoted"``, ``KEY='single'``. Skips
    blank lines and comments. Strips trailing whitespace.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, _, value = s.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip matching quote pair
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        out[key] = value
    return out


def read_env_value(name: str, env_file: Path | None = None) -> str | None:
    """Return the value of ``name`` — checking ``os.environ`` first,
    then ``env_file`` (defaults to ``default_env_file()``).

    Returns None when the key is absent in both.
    """
    shell_value = os.environ.get(name)
    if shell_value:
        return shell_value
    path = env_file if env_file is not None else default_env_file()
    parsed = _parse_env_file(path)
    return parsed.get(name)


def _format_value(value: str) -> str:
    """Quote a value if it contains whitespace or special characters."""
    if not value:
        return ""
    needs_quote = any(c in value for c in (" ", "\t", "#", "\n"))
    if needs_quote:
        if "'" in value:
            return shlex.quote(value)
        return f'"{value}"'
    return value


def write_env_value(name: str, value: str, env_file: Path | None = None) -> None:
    """Write ``name=value`` to ``env_file`` (default ``~/.opencomputer/.env``).

    Behavior:
      - Creates the file (and parent directories) if absent.
      - Updates the existing line in place if the key is already there.
      - Appends a new line if the key is absent.
      - Preserves comments + unrelated lines + trailing newline.
      - Sets file mode to ``0o600`` after every write.
    """
    path = env_file if env_file is not None else default_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)

    formatted = _format_value(value)
    new_line = f"{name}={formatted}"

    if not path.exists():
        path.write_text(new_line + "\n", encoding="utf-8")
        path.chmod(0o600)
        return

    lines = path.read_text(encoding="utf-8").splitlines(keepends=False)
    found = False
    new_lines: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            existing_key = s.split("=", 1)[0].strip()
            if existing_key == name:
                new_lines.append(new_line)
                found = True
                continue
        new_lines.append(line)

    if not found:
        new_lines.append(new_line)

    text = "\n".join(new_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


__all__ = ["default_env_file", "read_env_value", "write_env_value"]
