"""Slot 4b — always-on skill body injection in ``base.j2``.

Plan: docs/superpowers/specs/2026-05-16-using-superpowers-injection/PLAN.md
Milestone 2 (T2.1 / T2.2 / T2.3 / T2.4).

Slot 4b renders the bodies of all skills where ``always_on=True``,
alphabetically by name, after the existing Slot 4 skill-list. When no
skill opts in, the entire Slot 4b section is omitted (no orphan header,
no whitespace cruft, no prompt-cache key thrash).

These tests cover:
  - Single always-on skill renders its body after Slot 4.
  - Multiple always-on skills render in alphabetical order.
  - No always-on skill → Slot 4b absent entirely.
  - Jinja-conflicting body content (``<EXTREMELY-IMPORTANT>``, ``{``
    chars, ``{{`` and ``{%`` sequences from the canonical
    ``using-superpowers/SKILL.md`` dot-graph block) round-trips
    verbatim — no Jinja syntax error, no HTML escaping.
  - A skill that's in the list but ``always_on=False`` does NOT have
    its body injected (only its description shows up in Slot 4).
"""

from __future__ import annotations

from pathlib import Path

from opencomputer.agent.memory import SkillMeta
from opencomputer.agent.prompt_builder import PromptBuilder

# Slot 4b sentinel — the header text used by the template. Tests use
# this as a presence check; if the team renames the header in the
# template, this constant must be updated to match.
SLOT_4B_HEADER = "# Standing skill instructions"


def _make_skill(
    tmp_path: Path,
    *,
    name: str,
    body: str,
    always_on: bool = True,
    description: str = "test skill",
) -> SkillMeta:
    """Construct a SkillMeta whose path points at a real SKILL.md on disk.

    The renderer reads the body straight from the path, so the on-disk
    file MUST exist (a SkillMeta with a phantom path would surface as
    an empty body, masking the test).
    """
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    skill_md = d / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}"
    )
    return SkillMeta(
        id=name,
        name=name,
        description=description,
        path=skill_md,
        always_on=always_on,
    )


# ─── single skill renders ────────────────────────────────────────────


def test_slot_4b_renders_always_on_skill_body(tmp_path):
    s = _make_skill(
        tmp_path,
        name="solo-on",
        body="STANDING_RULE_VERY_LOAD_BEARING\nMore body text.",
    )
    out = PromptBuilder().build(skills=[s])
    assert SLOT_4B_HEADER in out
    assert "STANDING_RULE_VERY_LOAD_BEARING" in out
    # Body must appear AFTER the Slot 4 skill-list — Slot 4b's purpose
    # is to be the standing instruction the model sees after the menu.
    list_idx = out.find("**solo-on**")
    body_idx = out.find("STANDING_RULE_VERY_LOAD_BEARING")
    assert list_idx != -1 and body_idx != -1
    assert body_idx > list_idx, (
        f"Slot 4b body (at {body_idx}) must follow Slot 4 list "
        f"(at {list_idx}) for the prompt to read naturally."
    )


def test_slot_4b_includes_skill_name_label(tmp_path):
    """Each always-on body is labelled with the skill name so the model
    can attribute the standing rule to its source."""
    s = _make_skill(tmp_path, name="labelled", body="rules go here")
    out = PromptBuilder().build(skills=[s])
    # The name appears alongside the body so the model knows which
    # standing skill is speaking.
    assert "labelled" in out
    assert "rules go here" in out


# ─── multiple skills — alphabetical order ────────────────────────────


def test_slot_4b_renders_multiple_skills_alphabetically(tmp_path):
    a = _make_skill(tmp_path, name="alpha-rules", body="BODY_OF_ALPHA")
    z = _make_skill(tmp_path, name="zeta-rules", body="BODY_OF_ZETA")
    # Pass in REVERSED order to prove the renderer sorts, not the caller.
    out = PromptBuilder().build(skills=[z, a])
    a_idx = out.find("BODY_OF_ALPHA")
    z_idx = out.find("BODY_OF_ZETA")
    assert a_idx != -1 and z_idx != -1
    assert a_idx < z_idx, (
        "Slot 4b must order always-on bodies alphabetically by skill "
        "name for prompt-cache stability across sessions."
    )


# ─── empty / opt-out cases ────────────────────────────────────────────


def test_slot_4b_absent_when_no_always_on_skill(tmp_path):
    """When no skill opts in, Slot 4b is fully omitted — no orphan
    header, no blank section."""
    off = _make_skill(
        tmp_path, name="dormant", body="never seen", always_on=False
    )
    out = PromptBuilder().build(skills=[off])
    assert SLOT_4B_HEADER not in out
    assert "never seen" not in out
    # The skill still shows in Slot 4 (the list), so the test isn't
    # accidentally checking an empty-skills branch.
    assert "**dormant**" in out


def test_slot_4b_absent_when_skills_list_empty():
    out = PromptBuilder().build(skills=[])
    assert SLOT_4B_HEADER not in out


def test_slot_4b_renders_only_always_on_bodies(tmp_path):
    """Mixing always-on and dormant skills: only opt-in bodies render."""
    on = _make_skill(tmp_path, name="loud", body="OPT_IN_BODY")
    off = _make_skill(
        tmp_path, name="quiet", body="OPT_OUT_BODY", always_on=False
    )
    out = PromptBuilder().build(skills=[on, off])
    assert "OPT_IN_BODY" in out
    assert "OPT_OUT_BODY" not in out
    # Both skills' descriptions still show up in Slot 4 (the list).
    assert "**loud**" in out
    assert "**quiet**" in out


# ─── Jinja-conflict round-trip ───────────────────────────────────────


def test_slot_4b_handles_jinja_braces_in_body(tmp_path):
    """A body with raw ``{{`` / ``}}`` / ``{%`` / ``%}`` sequences must
    not error and must render verbatim — the renderer must NOT re-
    evaluate the body as a Jinja template.

    Real-world trigger: the canonical ``using-superpowers/SKILL.md`` has
    a ``digraph { ... }`` block whose braces would mangle if treated as
    Jinja syntax.
    """
    nasty = (
        "Here is a digraph:\n\n"
        "```\n"
        "digraph {\n"
        '    "node a" -> "node b" [label="{{ template-syntax }}"];\n'
        "    {% if foo %}{{ x }}{% endif %}\n"
        "}\n"
        "```\n"
    )
    s = _make_skill(tmp_path, name="brace-trip", body=nasty)
    out = PromptBuilder().build(skills=[s])
    # The whole body shows up verbatim, brace-for-brace.
    assert "digraph {" in out
    assert "{{ template-syntax }}" in out
    assert "{% if foo %}{{ x }}{% endif %}" in out


def test_slot_4b_handles_html_ish_tags_in_body(tmp_path):
    """``<EXTREMELY-IMPORTANT>`` and similar HTML-ish tags in markdown
    must NOT be HTML-escaped (no ``&lt;EXTREMELY-IMPORTANT&gt;``)."""
    body = (
        "<EXTREMELY-IMPORTANT>\n"
        "You MUST do X.\n"
        "</EXTREMELY-IMPORTANT>\n"
    )
    s = _make_skill(tmp_path, name="tag-trip", body=body)
    out = PromptBuilder().build(skills=[s])
    assert "<EXTREMELY-IMPORTANT>" in out
    assert "</EXTREMELY-IMPORTANT>" in out
    # The escaped form is NOT present — autoescape is off for .j2.
    assert "&lt;EXTREMELY-IMPORTANT&gt;" not in out


# ─── frontmatter stripping ────────────────────────────────────────────


def test_slot_4b_strips_frontmatter_from_body(tmp_path):
    """The frontmatter (``---`` block) must NOT appear in the rendered
    body — only the post-frontmatter content reaches the prompt."""
    s = _make_skill(tmp_path, name="stripme", body="THE_REAL_BODY")
    out = PromptBuilder().build(skills=[s])
    assert "THE_REAL_BODY" in out
    # Frontmatter delimiters must not leak.
    # (Other slots may legitimately contain ``---``, so we check that
    # the skill's own frontmatter field doesn't appear in the body
    # region — by searching for the ``name:`` line specifically.)
    assert "name: stripme" not in out
