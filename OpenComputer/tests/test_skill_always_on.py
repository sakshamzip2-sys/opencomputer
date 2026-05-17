"""``always_on: true`` skill-frontmatter flag — schema + loader wiring.

Plan: docs/superpowers/specs/2026-05-16-using-superpowers-injection/PLAN.md
Milestone 1 (T1.2 / T1.3 / T1.4).

A skill that declares ``always_on: true`` opts its (frontmatter-stripped)
body into auto-injection in every system prompt. The schema lives on
:class:`SkillMeta`; the renderer (Slot 4b) is Milestone 2.

These tests cover:
  - Field default + override on the dataclass (T1.2).
  - The CC §7 extras parser reads ``always_on`` (snake_case + dashed).
  - The 16 KB body cap when ``always_on=True`` (T1.3) — oversize bodies
    flip the flag OFF with a WARN, but the skill still loads (parser
    tolerance posture).
  - The loader wires ``_parse_skill_extras`` so a real SKILL.md with
    ``always_on: true`` round-trips through ``MemoryManager.list_skills``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from opencomputer.agent.memory import (
    ALWAYS_ON_BODY_CAP_BYTES,
    MemoryManager,
    SkillMeta,
    _parse_skill_extras,
)

# ─── helpers ──────────────────────────────────────────────────────────


def _write_skill_with_body(
    skills_dir: Path,
    skill_id: str,
    frontmatter: dict,
    body: str = "default body\n",
) -> Path:
    """Write a SKILL.md with the given frontmatter dict and body text."""
    d = skills_dir / skill_id
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {skill_id}", "description: t"]
    for key, val in frontmatter.items():
        if isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        elif isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {val}")
    lines.append("---")
    lines.append(body)
    path = d / "SKILL.md"
    path.write_text("\n".join(lines))
    return path


# ─── T1.2 — SkillMeta has the field ──────────────────────────────────


def test_skill_meta_has_always_on_field_default_false():
    """SkillMeta gains ``always_on: bool = False`` with permissive default."""
    m = SkillMeta(id="s", name="s", description="d", path=Path("/tmp/s"))
    assert m.always_on is False


def test_skill_meta_always_on_settable_true():
    m = SkillMeta(
        id="s", name="s", description="d", path=Path("/tmp/s"), always_on=True
    )
    assert m.always_on is True


# ─── T1.2 — parser reads the new key ─────────────────────────────────


def test_parser_reads_always_on_true():
    extras = _parse_skill_extras({"always_on": True})
    assert extras["always_on"] is True


def test_parser_always_on_defaults_false():
    """Missing key → False (existing skills unaffected)."""
    extras = _parse_skill_extras({})
    assert extras["always_on"] is False


def test_parser_always_on_explicit_false():
    extras = _parse_skill_extras({"always_on": False})
    assert extras["always_on"] is False


def test_parser_always_on_dashed_key_accepted():
    """Claude Code dashed form — `always-on: true` — also works."""
    extras = _parse_skill_extras({"always-on": True})
    assert extras["always_on"] is True


def test_parser_always_on_non_bool_value_drops_to_default():
    """Malformed value (string ``"yes"``, int 1) → defaults False; permissive."""
    assert _parse_skill_extras({"always_on": "yes"})["always_on"] is False
    assert _parse_skill_extras({"always_on": 1})["always_on"] is False
    assert _parse_skill_extras({"always_on": None})["always_on"] is False


# ─── T1.2 — list_skills wires the parser (integration) ───────────────


def test_list_skills_wires_always_on_true(tmp_path):
    """A SKILL.md with ``always_on: true`` round-trips through list_skills."""
    skills_dir = tmp_path / "skills"
    _write_skill_with_body(
        skills_dir, "ao-skill", {"always_on": True}, body="hello world body"
    )
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills_dir,
        bundled_skills_paths=[],
    )
    skills = mm.list_skills()
    ao = [s for s in skills if s.id == "ao-skill"]
    assert ao, "ao-skill should be loaded"
    assert ao[0].always_on is True


def test_list_skills_default_always_on_false(tmp_path):
    """A SKILL.md WITHOUT ``always_on`` field defaults to False."""
    skills_dir = tmp_path / "skills"
    _write_skill_with_body(skills_dir, "plain-skill", {}, body="plain body")
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills_dir,
        bundled_skills_paths=[],
    )
    skills = mm.list_skills()
    plain = [s for s in skills if s.id == "plain-skill"]
    assert plain[0].always_on is False


def test_list_skills_also_wires_other_extras(tmp_path):
    """Wiring `_parse_skill_extras` into list_skills also fixes the
    pre-existing latent bug where ``disable_model_invocation`` /
    ``user_invocable`` / ``paths`` were silently ignored.

    This guards against regressing the wiring at the constructor splat.
    """
    skills_dir = tmp_path / "skills"
    _write_skill_with_body(
        skills_dir,
        "kitchen-sink",
        {
            "always_on": True,
            "disable_model_invocation": True,
            "user_invocable": False,
            "argument_hint": "<arg>",
            "paths": ["src/**/*.py"],
        },
        body="body",
    )
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills_dir,
        bundled_skills_paths=[],
    )
    sks = {s.id: s for s in mm.list_skills()}
    s = sks["kitchen-sink"]
    assert s.always_on is True
    assert s.disable_model_invocation is True
    assert s.user_invocable is False
    assert s.argument_hint == "<arg>"
    assert s.paths == ("src/**/*.py",)


# ─── T1.3 — 16 KB body cap on always_on skills ───────────────────────


def test_always_on_body_cap_constant_is_16_kb():
    """The cap is exported as a module-level constant for renderer reuse."""
    assert ALWAYS_ON_BODY_CAP_BYTES == 16 * 1024


def test_always_on_oversize_body_flips_flag_to_false(tmp_path, caplog):
    """``always_on: true`` with a body > 16 KB → loader flips it to False
    and logs a WARNING. Skill still loads (permissive)."""
    skills_dir = tmp_path / "skills"
    big_body = "X" * (ALWAYS_ON_BODY_CAP_BYTES + 1)
    _write_skill_with_body(
        skills_dir, "oversize", {"always_on": True}, body=big_body
    )
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills_dir,
        bundled_skills_paths=[],
    )
    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.memory"):
        skills = mm.list_skills()
    s = next(s for s in skills if s.id == "oversize")
    # Flag flipped off; skill itself still present
    assert s.always_on is False
    # WARN log mentions the skill id and the cap
    msgs = [r.getMessage() for r in caplog.records]
    assert any("oversize" in m and "always_on" in m for m in msgs), msgs


def test_always_on_at_or_below_cap_is_fine(tmp_path):
    """Exactly-cap body keeps ``always_on=True`` (boundary check)."""
    skills_dir = tmp_path / "skills"
    body = "Y" * ALWAYS_ON_BODY_CAP_BYTES
    _write_skill_with_body(
        skills_dir, "boundary", {"always_on": True}, body=body
    )
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills_dir,
        bundled_skills_paths=[],
    )
    s = next(s for s in mm.list_skills() if s.id == "boundary")
    assert s.always_on is True


def test_non_always_on_skill_with_huge_body_is_fine(tmp_path, caplog):
    """A regular skill (always_on omitted / false) with a 100 KB body is
    unaffected by the cap — the cap targets always_on opt-in only."""
    skills_dir = tmp_path / "skills"
    body = "Z" * (100 * 1024)
    _write_skill_with_body(skills_dir, "fatlazy", {}, body=body)
    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=skills_dir,
        bundled_skills_paths=[],
    )
    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.memory"):
        skills = mm.list_skills()
    s = next(s for s in skills if s.id == "fatlazy")
    assert s.always_on is False
    # No warning for non-always_on skills
    assert not any("fatlazy" in r.getMessage() and "cap" in r.getMessage()
                   for r in caplog.records)


# ─── Sanity: existing-extras parser surface is untouched ─────────────


def test_existing_extras_unaffected_by_new_field():
    """Adding ``always_on`` to the parser must not perturb existing fields.

    Regression guard for the parser surface — every existing default
    stays exactly what it was, and the existing dashed forms still parse.
    """
    extras = _parse_skill_extras({})
    assert extras["disable_model_invocation"] is False
    assert extras["user_invocable"] is True
    assert extras["argument_hint"] == ""
    assert extras["paths"] == ()
    assert extras["skill_model"] == ""
    assert extras["allowed_tools"] == ()
    # New field also present with permissive default
    assert extras["always_on"] is False
