"""
Filesystem security checks for plugin discovery (I.1).

Mirrors OpenClaw's ``checkSourceEscapesRoot`` + ``checkPathStatAndPermissions``
(sources/openclaw/src/plugins/discovery.ts:152-307). Called from
``discover()`` BEFORE parsing each candidate's manifest.

Three classes of attack we defend against:

1. **Symlink escape** — a plugin directory (or its resolved target) that
   sits OUTSIDE the declared search root. Without this check an attacker
   who can write ``~/.opencomputer/plugins/foo`` as a symlink pointing
   at, say, ``/tmp/malicious-plugin`` can load arbitrary code through the
   trusted plugin path.

2. **World-writable directory** — permissions ``0o002`` on the plugin
   root. Another local user can edit the plugin's entry module between
   discovery and load. User-installed plugins fail closed; bundled
   plugins log a warning only (some package managers widen bundled dirs
   during install and tightening happens at startup — see I.1 docstring
   below for the rationale mismatch with OpenClaw's ``chmod`` repair
   path, which we deliberately do NOT do here).

3. **Owner UID mismatch** — plugin directory owned by a different user
   than the effective uid. Matches OpenClaw's ``path_suspicious_ownership``
   and catches cases where another user's plugin is masquerading as one
   of ours. User-installed plugins fail closed; bundled plugins log a
   warning only. Skipped on Windows (no POSIX owner semantics).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("opencomputer.plugins.security")

# Bit-mask for the POSIX "others write" permission bit (the trailing
# ``0o002`` in an octal mode). Kept as a named constant so the checks
# below read as intent, not as magic arithmetic.
_WORLD_WRITABLE_BIT = 0o002


@dataclass(frozen=True, slots=True)
class SecurityCheckResult:
    """Outcome of ``validate_plugin_root``.

    On success ``ok=True`` and ``reason`` is ``None`` (or a short note
    explaining a skipped check, e.g. on Windows). On failure ``ok=False``
    and ``reason`` is a human-readable string that the caller can log
    verbatim.
    """

    ok: bool
    reason: str | None = None


def _path_is_inside(child: Path, parent: Path) -> bool:
    """Return True iff ``child`` is ``parent`` or a descendant.

    Both arguments must be already-resolved absolute paths. Uses
    ``Path.is_relative_to`` (3.9+) which handles the parent boundary
    correctly without the ``startswith`` string-compare pitfall where
    e.g. ``/tmp/plugins-evil`` looks like it starts with ``/tmp/plugins``.
    """
    try:
        return child == parent or child.is_relative_to(parent)
    except ValueError:
        # Different drives on Windows → definitely not inside.
        return False


def validate_plugin_root(
    path: Path,
    search_root: Path,
    *,
    is_bundled: bool = False,
) -> SecurityCheckResult:
    """Validate a candidate plugin directory against common attacks.

    Called by ``discover()`` before reading the candidate's manifest.
    ``path`` is the plugin's root directory (``<search_root>/<plugin-id>``);
    ``search_root`` is the search-path root that turned it up.

    ``is_bundled`` selects the policy:

    - ``False`` (user-installed, default) — world-writable and
      owner-UID mismatch both fail closed. The user's own
      ``~/.opencomputer/plugins/`` tree must be tight.
    - ``True``  (bundled, under ``extensions/``) — world-writable and
      owner-UID mismatch both log a WARNING but still return ``ok=True``.
      Some package managers write bundled dirs with relaxed permissions
      during install; we don't want a working CLI to refuse to start
      because of the install tooling's mode bits.

    Symlink-escape is a fail-closed check for both cases — no amount of
    bundling excuses a plugin whose real path lives outside the declared
    search root.
    """
    # --- 1. Symlink escape. Resolve both sides and require containment.
    try:
        resolved_path = path.resolve()
        resolved_root = search_root.resolve()
    except OSError as e:  # e.g. dangling symlink, permission denied
        return SecurityCheckResult(
            ok=False, reason=f"cannot resolve plugin path ({path}): {e}"
        )

    if not _path_is_inside(resolved_path, resolved_root):
        return SecurityCheckResult(
            ok=False,
            reason=(
                f"plugin path escapes search root: "
                f"{path} -> {resolved_path} "
                f"(root={search_root} -> {resolved_root})"
            ),
        )

    # --- 2 & 3. POSIX-only checks: world-writable + owner UID mismatch.
    if not hasattr(os, "geteuid"):
        # Windows (or any platform without POSIX uid semantics). Skip
        # quietly — the symlink escape check above still ran and gives
        # us the minimum viable defence.
        return SecurityCheckResult(ok=True, reason="posix-only checks skipped")

    try:
        stat_result = resolved_path.stat()
    except OSError as e:
        return SecurityCheckResult(
            ok=False, reason=f"cannot stat plugin path ({path}): {e}"
        )

    mode_bits = stat_result.st_mode & 0o777

    if mode_bits & _WORLD_WRITABLE_BIT:
        message = (
            f"plugin path is world-writable: {path} "
            f"(mode={oct(mode_bits)})"
        )
        if is_bundled:
            logger.warning("%s (bundled plugin — loading anyway)", message)
        else:
            return SecurityCheckResult(ok=False, reason=message)

    expected_uid = os.geteuid()
    found_uid = stat_result.st_uid
    # Root-owned paths are always acceptable — matches OpenClaw's
    # ``stat.uid !== 0`` escape hatch. A bundled install script that runs
    # under sudo will legitimately leave root-owned files in our tree.
    if found_uid != expected_uid and found_uid != 0:
        message = (
            f"plugin path has suspicious ownership: {path} "
            f"(owner uid={found_uid}, expected {expected_uid} or 0)"
        )
        if is_bundled:
            logger.warning("%s (bundled plugin — loading anyway)", message)
        else:
            return SecurityCheckResult(ok=False, reason=message)

    return SecurityCheckResult(ok=True)


__all__ = ["SecurityCheckResult", "validate_plugin_root"]
