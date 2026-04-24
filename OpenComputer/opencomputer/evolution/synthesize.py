"""Skill synthesizer stub for OpenComputer Evolution.

``SkillSynthesizer`` writes synthesized skills to the evolution quarantine
namespace.  The implementation logic lands in B2; this B1 stub establishes the
public API surface so callers can be wired against a stable contract today.

Design reference: OpenComputer/docs/evolution/design.md §8.
"""

from __future__ import annotations

from pathlib import Path

from opencomputer.evolution.reflect import Insight


class SkillSynthesizer:
    """Writes synthesized skills to the evolution quarantine namespace.

    B1: stub — ``synthesize()`` raises NotImplementedError.  The constructor
    accepts the destination directory so B2 can swap implementations cleanly.
    """

    def __init__(self, *, dest_dir: Path | None = None) -> None:
        """Initialise the synthesizer.

        Args:
            dest_dir: Destination directory for synthesized skill trees.  If
                ``None``, defaults to ``evolution_home() / "skills"`` — resolved
                lazily inside ``synthesize()`` to keep imports light.
        """
        self._dest_dir = dest_dir

    def synthesize(self, insight: Insight) -> Path:
        """Write a SKILL.md (+ references/ + examples/) tree from an Insight.

        Returns the path to the written skill directory.

        B1: NotImplementedError.  B2 implementation: III.4 hierarchical layout,
        atomic write via tmp dir + os.replace, slug-collision handling
        (-2, -3 suffixes).

        Raises:
            NotImplementedError: always in B1.
        """
        raise NotImplementedError("SkillSynthesizer.synthesize() lands in B2 — see plan §B2")
