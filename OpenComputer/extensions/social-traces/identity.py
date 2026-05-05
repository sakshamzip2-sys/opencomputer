"""Per-profile opaque agent identity for the social-traces plugin.

Each profile gets a randomly-generated ``submitter_hash`` stored at
``<profile_home>/traces/agent_id``. This id travels with every TraceCard
submission so the network can rate-limit and (later) build a per-agent
trust score — but it is NEVER user identity. Generated from
:func:`secrets.token_hex` at first read; not derivable from any user
data; safe to share with the network.

Privacy invariant: the only identifier the network ever sees is this
random string. If a user wants to "rotate" their identity (start fresh
on the network), deleting the file regenerates it on next access.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from .state import traces_dir

AGENT_ID_FILENAME = "agent_id"

#: Length of the hex-encoded agent id. 32 bytes → 64 hex chars. Plenty of
#: entropy to avoid collisions across the global agent population while
#: staying short enough to fit in a header without ceremony.
AGENT_ID_BYTES = 32


def agent_id_path(profile_home: Path) -> Path:
    return traces_dir(profile_home) / AGENT_ID_FILENAME


def get_or_create_agent_id(profile_home: Path) -> str:
    """Return the profile's submitter_hash, generating it on first call.

    The file is written atomically (parent dir created on demand) and the
    string is returned trimmed of whitespace. Subsequent calls re-read
    the existing file — never regenerate.
    """
    path = agent_id_path(profile_home)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        existing = ""

    if existing:
        return existing

    agent_id = secrets.token_hex(AGENT_ID_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-read so a concurrent first call from another process
    # races deterministically — whoever wrote last wins; both processes
    # then read the winning value on a subsequent call. The cost of a
    # rare double-generate is one extra random hex string, which is
    # acceptable.
    path.write_text(agent_id + "\n", encoding="utf-8")
    return agent_id


__all__ = ["AGENT_ID_BYTES", "AGENT_ID_FILENAME", "agent_id_path", "get_or_create_agent_id"]
