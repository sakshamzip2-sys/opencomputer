"""Hardened ``.env``-style file loader (Round 2B P-16 sub-item b).

Reads ``KEY=value``-formatted dotenv files with two safety properties
that ``python-dotenv`` does NOT enforce by default:

* **UTF-8 BOM tolerance.** A leading ``﻿`` byte (BOM) — easy to
  introduce when an editor re-saves a file on Windows — is silently
  stripped before parsing so the first key isn't shadowed by an
  invisible prefix.
* **Permission fail-closed.** ``os.stat().st_mode & 0o077`` must be
  zero (i.e. group + other have NO read or write permission). If any
  of those bits are set, :func:`load_env_file` refuses to load and
  raises :class:`LoosePermissionError`. CLI / chat surfaces the
  refusal as a typed error; programmatic callers can pass
  ``allow_loose_perms=True`` (with a warning emitted at every call) to
  bypass.

This module exists because OpenComputer's per-profile credential
storage (``~/.opencomputer/<profile>/secrets/*.token`` and the
forthcoming ``.env``) sits next to source-tree files that may be
backed up, indexed by Spotlight, or scanned by IDE plugins. Refusing
to load a world-readable secrets file by default is a much safer
posture than silently warning.

Public API::

    from opencomputer.security.env_loader import load_env_file
    env = load_env_file(Path.home() / ".opencomputer" / "default" / ".env")
    # env: dict[str, str]

CLI override::

    opencomputer ... --allow-loose-env-perms
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger("opencomputer.security.env_loader")

#: The bit mask checked against ``stat().st_mode``. ``0o077`` covers the
#: full group + other read/write/execute set; any non-zero value here
#: means the file is broader than ``-rw-------`` (mode 0600). Owner
#: bits are intentionally NOT checked — files owned by the current
#: user with mode 0644 fail; files owned by the current user with mode
#: 0600 pass.
LOOSE_PERMS_MASK = 0o077

#: UTF-8 BOM that we silently strip from the start of the file.
_BOM = "﻿"

#: Process-wide override flag set by the CLI ``--allow-loose-env-perms``
#: handler. When ``True``, :func:`load_env_file` proceeds despite
#: group/other-readable bits being set (still emitting a WARNING). Kept
#: as a module-level state instead of being threaded through every
#: caller because the env-loader is reached from many subsystems
#: (channels, MCP env injection, plugin-discovery secrets) that all
#: should honor a single global toggle.
_PROCESS_ALLOW_LOOSE_PERMS: bool = False


def set_process_allow_loose_perms(value: bool) -> None:
    """Set the process-wide ``--allow-loose-env-perms`` override.

    Called by ``opencomputer.cli._apply_loose_env_perms_flag`` after
    intercepting the CLI flag from ``sys.argv``. Tests should reset
    via ``set_process_allow_loose_perms(False)`` in tear-down.
    """
    global _PROCESS_ALLOW_LOOSE_PERMS
    _PROCESS_ALLOW_LOOSE_PERMS = bool(value)


def get_process_allow_loose_perms() -> bool:
    """Return the current process-wide loose-perms override."""
    return _PROCESS_ALLOW_LOOSE_PERMS


class LoosePermissionError(PermissionError):
    """Raised when a ``.env`` file's permissions allow group/other access.

    Carries the offending path and the actual mode bits so the CLI
    surface can render an actionable message ("run ``chmod 600
    <path>`` to fix"). Subclasses :class:`PermissionError` so
    existing handlers that swallow generic permission issues still
    catch it.
    """

    def __init__(self, path: Path, mode: int) -> None:
        self.path = path
        self.mode = mode
        super().__init__(
            f"refusing to load {path}: mode {mode:04o} grants group/other access "
            f"(secrets file must be mode 0600 or stricter; "
            f"run `chmod 600 {path}` to fix, or pass --allow-loose-env-perms "
            f"to override)"
        )


def _check_permissions(path: Path) -> None:
    """Raise :class:`LoosePermissionError` if the file is readable beyond owner.

    Skipped silently on Windows where ``os.stat().st_mode``'s POSIX
    permission bits are not meaningful.
    """
    if os.name == "nt":
        # Windows POSIX-permission emulation always reports 0o666 even
        # when ACLs lock the file down — checking would generate
        # unactionable false positives. Document it and move on.
        return
    st = path.stat()
    bad_bits = stat.S_IMODE(st.st_mode) & LOOSE_PERMS_MASK
    if bad_bits:
        raise LoosePermissionError(path, stat.S_IMODE(st.st_mode))


def _parse(text: str) -> dict[str, str]:
    """Parse a ``.env``-format string into a dict.

    Supported syntax (intentionally minimal — we are NOT a full
    python-dotenv replacement):

    * ``KEY=value`` lines. Whitespace around ``=`` is stripped.
    * ``# comment`` lines and blank lines are skipped.
    * ``export KEY=value`` is tolerated (the ``export `` prefix is
      stripped).
    * Single or double quotes around the value are stripped.
    * Variable interpolation (``${OTHER}``) is intentionally NOT done
      — keep the loader inert so a malicious value cannot expand to
      something that escapes the file.
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Strip matching quotes — common dotenv syntax.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            continue
        out[key] = value
    return out


def load_env_file(
    path: Path | str,
    *,
    allow_loose_perms: bool | None = None,
) -> dict[str, str]:
    """Read a ``.env``-style file with BOM + permission hardening.

    Parameters
    ----------
    path:
        Filesystem path to the dotenv file. ``str`` is accepted for
        callers that haven't converted to :class:`Path` yet.
    allow_loose_perms:
        When ``True``, group/other readable files are loaded with a
        WARNING log entry (one per call). When ``False``, loose perms
        raise :class:`LoosePermissionError`. ``None`` (default) defers
        to the process-wide flag set by the CLI's
        ``--allow-loose-env-perms`` handler — programmatic callers can
        force either behavior by passing the bool explicitly.

    Returns
    -------
    dict[str, str]
        Parsed ``KEY=value`` pairs. Empty dict when the file does not
        exist (callers can probe without try/except).

    Raises
    ------
    LoosePermissionError
        File mode grants any group or other bit and
        ``allow_loose_perms`` resolves to ``False``. Skipped on Windows.
    OSError
        Underlying filesystem error other than missing file.
    """
    if allow_loose_perms is None:
        allow_loose_perms = _PROCESS_ALLOW_LOOSE_PERMS
    p = Path(path)
    if not p.exists():
        return {}
    if allow_loose_perms:
        try:
            st = p.stat()
            bad_bits = stat.S_IMODE(st.st_mode) & LOOSE_PERMS_MASK
            if bad_bits:
                logger.warning(
                    "loading %s with --allow-loose-env-perms — mode %04o "
                    "exposes secrets to group/other (run `chmod 600 %s` to fix)",
                    p,
                    stat.S_IMODE(st.st_mode),
                    p,
                )
        except OSError as exc:
            logger.warning("permission check stat() failed for %s: %s", p, exc)
    else:
        _check_permissions(p)
    text = p.read_text(encoding="utf-8")
    if text.startswith(_BOM):
        text = text[len(_BOM) :]
    return _parse(text)


__all__ = [
    "LOOSE_PERMS_MASK",
    "LoosePermissionError",
    "get_process_allow_loose_perms",
    "load_env_file",
    "set_process_allow_loose_perms",
]
