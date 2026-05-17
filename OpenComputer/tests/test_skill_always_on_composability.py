"""``always_on`` composability with other skill-frontmatter fields.

Plan: docs/superpowers/specs/2026-05-16-using-superpowers-injection/PLAN.md
Milestone 3 (T3.1 / T3.2 / T3.3).

Tests cover the documented composability matrix:

  - ``always_on=True`` + ``paths=[non-matching]`` → body NOT injected
    (paths wins; the author opted into cwd-gated activation by
    declaring ``paths:`` in frontmatter — see plan §5).
  - ``always_on=True`` + ``paths=[matching]`` → body IS injected.
  - ``always_on=True`` + ``context: fork`` orthogonality → body still
    injects (fork affects invocation lifetime, not prompt presence).
  - ``always_on=True`` + ``disable_model_invocation: true`` →
    body injects, model can't call the skill via the tool surface.
  - ``always_on=True`` + ``user_invocable: false`` → body still injects
    even if the skill is hidden from the slash menu.
"""

from __future__ import annotations

from pathlib import Path

from opencomputer.agent.memory import SkillMeta
from opencomputer.agent.prompt_builder import (
    PromptBuilder,
    _collect_always_on_bodies,
)

SLOT_4B_HEADER = "# Standing skill instructions"


def _skill_at(
    tmp_path: Path,
    *,
    name: str,
    body: str,
    always_on: bool = True,
    paths: tuple[str, ...] = (),
    disable_model_invocation: bool = False,
    user_invocable: bool = True,
    argument_hint: str = "",
) -> SkillMeta:
    """Build a SkillMeta with a real on-disk SKILL.md so the body
    loader has something to read."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    skill_md = d / "SKILL.md"
    skill_md.write_text(f"---\nname: {name}\ndescription: t\n---\n{body}")
    return SkillMeta(
        id=name,
        name=name,
        description="t",
        path=skill_md,
        always_on=always_on,
        paths=paths,
        disable_model_invocation=disable_model_invocation,
        user_invocable=user_invocable,
        argument_hint=argument_hint,
    )


# ─── T3.1 — paths gating wins over always_on ─────────────────────────


def test_always_on_with_non_matching_paths_not_injected(tmp_path):
    """A skill restricted to ``paths: ["uniqueprojectroot_xyz/**"]``
    MUST NOT inject its body when cwd is unrelated — paths wins per
    the plan's M3.T3.1 resolution.

    Pattern is intentionally unique-prefixed: ``skill_matches_cwd``
    walks up to filesystem root trying the glob at every ancestor, so
    common patterns like ``src/**`` can spuriously match in ancestors
    we don't control. The unique prefix keeps the test isolated.
    """
    cwd_unrelated = tmp_path / "unrelated"
    cwd_unrelated.mkdir()
    (cwd_unrelated / "readme.md").write_text("")
    s = _skill_at(
        tmp_path,
        name="project-only",
        body="ONLY_FOR_MY_PROJECT",
        paths=("uniqueprojectroot_xyzabc987/**/*.go",),
    )
    pairs = _collect_always_on_bodies([s], cwd=cwd_unrelated)
    assert pairs == []


def test_always_on_with_matching_paths_injected(tmp_path):
    """Same skill in a cwd whose contents satisfy the glob → body IS
    injected. Sanity check that the gating actually fires both ways."""
    project = tmp_path / "uniqueprojectroot_xyzabc987"
    project.mkdir()
    (project / "main.go").write_text("")
    cwd_inside = project / "sub"
    cwd_inside.mkdir()
    s = _skill_at(
        tmp_path,
        name="project-only",
        body="ONLY_FOR_MY_PROJECT",
        paths=("uniqueprojectroot_xyzabc987/**/*.go",),
    )
    pairs = _collect_always_on_bodies([s], cwd=cwd_inside)
    assert len(pairs) == 1
    assert pairs[0][0] == "project-only"
    assert "ONLY_FOR_MY_PROJECT" in pairs[0][1]


def test_always_on_with_empty_paths_universally_injected(tmp_path):
    """Empty ``paths`` (default) = universal — body injects in any cwd,
    matches the existing ``skill_matches_cwd`` behaviour."""
    s = _skill_at(tmp_path, name="universal", body="ANYWHERE", paths=())
    pairs = _collect_always_on_bodies([s], cwd=tmp_path / "anything")
    assert len(pairs) == 1


def test_always_on_with_paths_and_no_cwd_passed_skips_gating(tmp_path):
    """``cwd=None`` (legacy call shape) skips paths gating entirely —
    the helper behaves universally to preserve backwards compat for
    callers that haven't threaded cwd through yet."""
    s = _skill_at(
        tmp_path,
        name="anywhere-no-cwd",
        body="LEGACY_PATH",
        paths=("uniqueprojectroot_xyzabc987/never/exists",),
    )
    pairs = _collect_always_on_bodies([s], cwd=None)
    # Even with non-matching paths, no cwd means no gating.
    assert len(pairs) == 1


def test_paths_gating_integrates_with_real_render(tmp_path, monkeypatch):
    """Render via PromptBuilder.build() — when CWD doesn't match the
    skill's paths, Slot 4b is absent from the rendered prompt."""
    cwd = tmp_path / "no-match-here"
    cwd.mkdir()
    (cwd / "readme.md").write_text("")
    monkeypatch.chdir(cwd)
    s = _skill_at(
        tmp_path,
        name="proj-only",
        body="HIDDEN_BODY_TEXT",
        paths=("uniqueprojectroot_xyzabc987/**/*.py",),
    )
    out = PromptBuilder().build(skills=[s])
    # Slot 4b absent because paths gate filtered out the only opt-in.
    assert SLOT_4B_HEADER not in out
    assert "HIDDEN_BODY_TEXT" not in out
    # But the skill description is still in Slot 4 (the menu) — the
    # menu doesn't gate on paths today, so the model still knows the
    # skill exists; it just doesn't get the standing body.
    assert "**proj-only**" in out


# ─── T3.2 — orthogonality: context: fork ─────────────────────────────


def test_always_on_works_with_fork_context_orthogonally(tmp_path):
    """``context: fork`` controls invocation behaviour (the agent forks
    a subprocess to run the skill). It does NOT control prompt presence.
    A skill that's both ``always_on: true`` AND ``context: fork`` should
    still render its body — the two fields are orthogonal.

    Note: ``context`` is not currently a SkillMeta field (it lives on
    AgentTemplate or is read from frontmatter ad-hoc). We model the
    orthogonality by simulating: any extra frontmatter SHOULD NOT
    interfere with the renderer.
    """
    # We simulate the "context: fork" scenario by including extra
    # frontmatter beyond the schema and verifying the body still
    # renders. The extra key is permissively ignored by the parser.
    d = tmp_path / "forked"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\n"
        "name: forked\n"
        "description: t\n"
        "context: fork\n"
        "---\n"
        "FORK_BODY"
    )
    s = SkillMeta(
        id="forked",
        name="forked",
        description="t",
        path=d / "SKILL.md",
        always_on=True,
    )
    pairs = _collect_always_on_bodies([s])
    assert len(pairs) == 1
    assert pairs[0][1].strip() == "FORK_BODY"


# ─── T3.3 — orthogonality: disable_model_invocation ──────────────────


def test_always_on_with_disable_model_invocation_still_injects(tmp_path):
    """``disable_model_invocation: true`` hides the skill from the
    model's tool surface but does NOT hide the standing body. The two
    fields are orthogonal: the body provides knowledge, the
    invocation flag controls how the skill is triggered."""
    s = _skill_at(
        tmp_path,
        name="human-only",
        body="STILL_VISIBLE_AS_STANDING_RULE",
        disable_model_invocation=True,
    )
    pairs = _collect_always_on_bodies([s])
    assert len(pairs) == 1
    assert "STILL_VISIBLE_AS_STANDING_RULE" in pairs[0][1]


def test_always_on_with_user_invocable_false_still_injects(tmp_path):
    """Hiding the skill from the slash menu (``user_invocable: false``)
    does not hide its standing body either — the menu vs. prompt
    surfaces are independent."""
    s = _skill_at(
        tmp_path,
        name="hidden-menu",
        body="BODY_STILL_IN_PROMPT",
        user_invocable=False,
    )
    pairs = _collect_always_on_bodies([s])
    assert len(pairs) == 1
    assert "BODY_STILL_IN_PROMPT" in pairs[0][1]


# ─── explicit cwd parameter on build() ───────────────────────────────


def test_build_explicit_cwd_overrides_process_getcwd(tmp_path, monkeypatch):
    """Callers can pass an explicit ``cwd`` to ``PromptBuilder.build``
    so paths-gating doesn't accidentally inherit the process-global
    ``os.getcwd()`` — important for cron jobs, subprocesses, or tests
    that chdir somewhere unrelated.
    """
    # Set up: a skill restricted to a unique-prefix project dir.
    project = tmp_path / "uniqueprojectroot_xyzabc987"
    project.mkdir()
    (project / "main.py").write_text("")
    s = _skill_at(
        tmp_path,
        name="proj-cwd-test",
        body="ONLY_IN_PROJECT_CWD",
        paths=("uniqueprojectroot_xyzabc987/**/*.py",),
    )
    # Process cwd is somewhere UNRELATED — but we pass explicit cwd
    # pointing into the matching project, so Slot 4b should render.
    unrelated = tmp_path / "unrelated-process-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    out = PromptBuilder().build(skills=[s], cwd=project)
    assert "ONLY_IN_PROJECT_CWD" in out, (
        "Explicit cwd= must override process-global os.getcwd() so the "
        "skill renders for the caller's intended directory, not whatever "
        "directory the process happens to be in."
    )


def test_build_explicit_cwd_blocks_when_non_matching(tmp_path, monkeypatch):
    """The inverse: explicit cwd that doesn't match should suppress
    Slot 4b. Uses a unique path that exists NOWHERE so the matcher's
    walk-up to filesystem root can't find it via ancestor glob."""
    s = _skill_at(
        tmp_path,
        name="proj-cwd-test-2",
        body="HIDDEN_BY_EXPLICIT_CWD",
        # Path component unique enough that no ancestor up to / will
        # have it. The walk-up is bounded by _SKILL_PATHS_WALK_UP_LIMIT
        # (16 levels) but globs against every ancestor, so we want a
        # pattern guaranteed to miss at every level.
        paths=("definitely_unique_path_zyx98765_abcdef/**/*.py",),
    )
    unrelated = tmp_path / "unrelated-explicit-cwd"
    unrelated.mkdir()
    # Process cwd elsewhere (doesn't matter — explicit cwd overrides).
    monkeypatch.chdir(tmp_path)
    out = PromptBuilder().build(skills=[s], cwd=unrelated)
    assert "HIDDEN_BY_EXPLICIT_CWD" not in out


# ─── extra: defense-in-depth body cap (renderer side) ────────────────


def test_renderer_drops_oversize_body_even_if_meta_flag_true(tmp_path):
    """If a SkillMeta is hand-constructed with always_on=True bypassing
    the loader's cap check, the renderer drops the body with a WARN.
    Belt-and-braces protection of the prompt-token budget."""
    from opencomputer.agent.memory import ALWAYS_ON_BODY_CAP_BYTES

    big = "B" * (ALWAYS_ON_BODY_CAP_BYTES + 1)
    s = _skill_at(tmp_path, name="oversize-skipped", body=big)
    pairs = _collect_always_on_bodies([s])
    assert pairs == []
