"""Service-label generation for systemd / launchd / schtasks backends.

Single-install (canonical OPENCOMPUTER_HOME + 'default' profile) keeps the
historical ``opencomputer-gateway`` label so existing service files don't
need re-installing. Multi-install (non-canonical HOME OR a named profile)
appends a sha256[:8] hash so two daemons can coexist on one host without
unit-name collisions.

Hermes-parity reference: ``hermes-gateway-<hash>`` pattern from
``hermes_cli/gateway.py``.

Hash properties:
- 32 bits of entropy → 1 collision per ~65K installs (acceptable risk).
- ``install`` callers can detect collisions before writing systemd / launchd
  files and recommend a different ``--profile`` name.

Public API:
    service_label(profile="default") -> str
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

_CANONICAL_LABEL = "opencomputer-gateway"
_DEFAULT_HOME = str(Path.home() / ".opencomputer")


def _resolved_home() -> str:
    """Return the resolved ``OPENCOMPUTER_HOME`` (env override or default)."""
    return os.environ.get("OPENCOMPUTER_HOME") or _DEFAULT_HOME


def _hash_label_suffix(home: str, profile: str) -> str:
    """Deterministic 8-char hex digest of ``<home>|<profile>``."""
    digest = hashlib.sha256(f"{home}|{profile}".encode()).hexdigest()
    return digest[:8]


def service_label(profile: str = "default") -> str:
    """Return the service label for ``profile`` under the active home.

    Backwards compatible: canonical home + ``default`` profile returns the
    historical ``opencomputer-gateway`` label so existing service files keep
    working untouched. Any non-canonical home OR a named profile receives a
    sha256[:8] hash suffix so two installs can coexist on one host.
    """
    home = _resolved_home()
    if home == _DEFAULT_HOME and profile == "default":
        return _CANONICAL_LABEL
    suffix = _hash_label_suffix(home, profile)
    return f"{_CANONICAL_LABEL}-{suffix}"


__all__ = ["service_label"]
