"""Personality registry: built-in registers + custom override.

Personality is a *register* overlay. It does not replace the agent's
identity (which lives in SOUL.md and the base prompt). It adjusts how
the agent talks. Resolution is name → Personality(name, body); the
body is rendered into slot #7 of the system prompt.
"""
from __future__ import annotations

from .builtins import BUILTINS
from .loader import DEFAULT_NAME, Personality, resolve

__all__ = ["BUILTINS", "DEFAULT_NAME", "Personality", "resolve"]
