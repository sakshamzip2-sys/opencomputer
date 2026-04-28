"""Passive education — surfaces OC capabilities indirectly.

Public API
----------

:func:`select_reveal`
    Called from the agent loop post-turn. Returns the formatted
    reveal clause to append to the assistant's response, or ``None``.

:func:`maybe_seed_returning_user`
    Called once at loop init. Seeds the persistence file as
    "all moments fired" if the user has prior sessions — prevents a
    noise burst when the file first appears on an established account.

Architecture: see
``docs/superpowers/specs/2026-04-28-passive-education-design.md``
"""
from opencomputer.awareness.learning_moments.engine import (
    maybe_seed_returning_user,
    select_reveal,
)
from opencomputer.awareness.learning_moments.registry import (
    Context,
    LearningMoment,
    Severity,
    Surface,
    all_moments,
)

__all__ = [
    "Context",
    "LearningMoment",
    "Severity",
    "Surface",
    "all_moments",
    "maybe_seed_returning_user",
    "select_reveal",
]
