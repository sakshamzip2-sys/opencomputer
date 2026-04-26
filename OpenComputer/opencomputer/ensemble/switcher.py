"""Persona switching for ensemble mode (Phase 7.A: manual /persona).

Tracks the active persona within a single session. Episodic memory
(the session DB) stays shared across personas — that's the whole
point of intra-session switching. Declarative memory (``MEMORY.md``)
and ``SOUL.md`` are loaded per-persona on every switch.

The switch event is delivered via an optional callback so hook
subscribers (e.g. agent-loop prompt refresh) can react.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


class PersonaNotFound(LookupError):  # noqa: N818 — public name is the load-bearing one (no Error suffix per project style)
    """Raised when a requested persona doesn't exist as a profile dir."""


@dataclass
class PersonaSwitcher:
    """In-session persona state.

    Parameters
    ----------
    profiles_root:
        Directory holding per-profile subdirs (each with ``SOUL.md``
        + ``MEMORY.md``). Typically ``~/.opencomputer/profiles``.
    current:
        Current persona name. Must be one of the listed profiles or
        ``"default"`` (which uses ``profiles_root / "default" /``).
    on_switch:
        Optional callback invoked with ``{"from": str, "to": str}``
        on every successful switch. Use this to refresh the agent's
        system prompt at turn boundaries.
    """

    profiles_root: Path
    current: str
    on_switch: Callable[[dict[str, str]], None] | None = None
    _switch_count: int = field(default=0, init=False)

    @property
    def switch_count(self) -> int:
        """Number of switches this session. Useful for prompt-cache invalidation."""
        return self._switch_count

    def known_profiles(self) -> list[str]:
        """Return sorted list of available persona names."""
        if not self.profiles_root.is_dir():
            return []
        return sorted(p.name for p in self.profiles_root.iterdir() if p.is_dir())

    def switch_to(self, name: str) -> None:
        """Switch to ``name``. Raises ``PersonaNotFound`` on unknown name.

        No-op if ``name`` already matches ``current`` (callback not fired).
        """
        if name == self.current:
            return
        if name not in self.known_profiles():
            raise PersonaNotFound(
                f"persona {name!r} not found. Available: "
                f"{', '.join(self.known_profiles()) or '(none)'}"
            )
        prev = self.current
        self.current = name
        self._switch_count += 1
        if self.on_switch is not None:
            try:
                self.on_switch({"from": prev, "to": name})
            except Exception:  # noqa: BLE001 — callback errors are not switch failures
                # Logged at the callback's discretion. We don't want a
                # bad callback to roll back the user-visible switch.
                pass

    def _read_or_empty(self, filename: str) -> str:
        path = self.profiles_root / self.current / filename
        return path.read_text() if path.exists() else ""

    def active_soul(self) -> str:
        """Return SOUL.md contents for the current persona, or empty string."""
        return self._read_or_empty("SOUL.md")

    def active_memory(self) -> str:
        """Return MEMORY.md contents for the current persona, or empty string."""
        return self._read_or_empty("MEMORY.md")
