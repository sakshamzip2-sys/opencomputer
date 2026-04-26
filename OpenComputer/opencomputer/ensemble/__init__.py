"""Ensemble mode — intra-session persona switching (Phase 7 of catch-up plan).

OpenComputer's profile system already covers ~95% of what
OpenClaw/Hermes call "personas":

- Per-profile ``SOUL.md`` (identity)
- Per-profile ``MEMORY.md`` (declarative memory)
- Per-profile skills, secrets, channels
- Subprocess HOME isolation

The remaining 5% is intra-session switching — the user wants to talk
to a different persona without restarting the CLI. This module ships
**Phase 7.A**: a manual ``/persona <name>`` slash command that swaps
the active persona at turn boundaries.

Phase 7.B (auto-router that picks the persona based on user message)
and Phase 7.C (``--ensemble`` CLI flag) are deferred until 7.A sees
real use in dogfooding. Premature automation would burn cache and
add cost without proven demand.
"""

from __future__ import annotations

from opencomputer.ensemble.persona_command import PersonaSlashCommand
from opencomputer.ensemble.switcher import PersonaNotFound, PersonaSwitcher

__all__ = ["PersonaNotFound", "PersonaSlashCommand", "PersonaSwitcher"]
