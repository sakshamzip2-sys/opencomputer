"""Resolve a personality name to its body, with custom override."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from .builtins import BUILTINS

logger = logging.getLogger("opencomputer.agent.personality")

DEFAULT_NAME = "helpful"


@dataclass(frozen=True, slots=True)
class Personality:
    name: str
    body: str


def resolve(name: str, *, custom: Mapping[str, object]) -> Personality:
    """Resolve a personality name to a Personality.

    Resolution order:
      1. ``custom[name]`` if it is a non-empty string (override builtins)
      2. ``BUILTINS[name]``
      3. fall back to ``BUILTINS[DEFAULT_NAME]``

    Never raises. Malformed custom entries (non-string, empty) are
    skipped with a one-shot warning.
    """
    key = (name or "").strip().lower() or DEFAULT_NAME

    custom_body = custom.get(key)
    if custom_body is not None:
        if isinstance(custom_body, str) and custom_body.strip():
            return Personality(name=key, body=custom_body.strip())
        logger.warning(
            "personality: custom entry %r is %s — falling back",
            key,
            type(custom_body).__name__,
        )

    body = BUILTINS.get(key)
    if body is None:
        return Personality(name=DEFAULT_NAME, body=BUILTINS[DEFAULT_NAME])
    return Personality(name=key, body=body)


__all__ = ["DEFAULT_NAME", "Personality", "resolve"]
