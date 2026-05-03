"""Unit tests for `snapshot/role_snapshot.py` + `snapshot/snapshot_roles.py`.

Covers:
  - 46 ARIA role constants present, partitioned correctly
  - parse_role_ref tolerates `e1`, `@e1`, `ref=e1`, `[ref=e1]`
  - ref dedup matches the deep-dive worked example for `button "OK"` × 2
  - non-duplicate `nth` is stripped from the final ref map
  - interactive-only mode emits a flat list of just interactive elements
  - compact mode drops indent blocks with no refs
  - snapshot stability — repeat snapshots over the same input produce
    the same refs for the same elements
"""

from __future__ import annotations

from extensions.browser_control.snapshot import (
    CONTENT_ROLES,
    INTERACTIVE_ROLES,
    STRUCTURAL_ROLES,
    build_role_snapshot_from_aria_snapshot,
    parse_role_ref,
)

# ─── role constants ───────────────────────────────────────────────────


def test_role_set_sizes_and_disjoint() -> None:
    assert len(INTERACTIVE_ROLES) == 17
    assert len(CONTENT_ROLES) == 10
    assert len(STRUCTURAL_ROLES) == 19
    # No role is in two sets.
    assert not (INTERACTIVE_ROLES & CONTENT_ROLES)
    assert not (INTERACTIVE_ROLES & STRUCTURAL_ROLES)
    assert not (CONTENT_ROLES & STRUCTURAL_ROLES)


def test_well_known_roles_classified_correctly() -> None:
    assert "button" in INTERACTIVE_ROLES
    assert "link" in INTERACTIVE_ROLES
    assert "checkbox" in INTERACTIVE_ROLES
    assert "main" in CONTENT_ROLES
    assert "heading" in CONTENT_ROLES
    assert "generic" in STRUCTURAL_ROLES
    assert "row" in STRUCTURAL_ROLES


# ─── parse_role_ref ───────────────────────────────────────────────────


def test_parse_role_ref_variants() -> None:
    assert parse_role_ref("e1") == "e1"
    assert parse_role_ref("@e1") == "e1"
    assert parse_role_ref("ref=e1") == "e1"
    assert parse_role_ref("[ref=e1]") == "e1"
    assert parse_role_ref("  e1  ") == "e1"


def test_parse_role_ref_invalid() -> None:
    assert parse_role_ref("") is None
    assert parse_role_ref("not a ref") is None
    assert parse_role_ref(None) is None  # type: ignore[arg-type]


# ─── build_role_snapshot — worked example ─────────────────────────────


def _walked_example_input() -> str:
    return (
        '- main "Content":\n'
        '  - button "OK"\n'
        '  - link "Home"\n'
        '  - button "OK"\n'
    )


def test_aria_snapshot_dedups_button_ok() -> None:
    """Mirrors deep-dive §4 worked example."""
    result = build_role_snapshot_from_aria_snapshot(_walked_example_input())
    refs = result.refs
    assert len(refs) == 4
    # Two button "OK" entries get nth=0 and nth=1.
    button_refs = [r for r in refs.values() if r.role == "button" and r.name == "OK"]
    assert len(button_refs) == 2
    nths = sorted(r.nth for r in button_refs)
    assert nths == [0, 1]
    # Non-duplicates have nth stripped.
    main_ref = next(r for r in refs.values() if r.role == "main")
    assert main_ref.nth is None
    link_ref = next(r for r in refs.values() if r.role == "link")
    assert link_ref.nth is None


def test_aria_snapshot_visible_nth_only_when_gt_zero() -> None:
    """The visible `[nth=N]` suffix only appears for nth > 0."""
    result = build_role_snapshot_from_aria_snapshot(_walked_example_input())
    text = result.snapshot
    # The first button "OK" line should have [ref=eX] without [nth=...].
    lines = text.split("\n")
    first_btn = next(ln for ln in lines if 'button "OK"' in ln and "[nth=" not in ln)
    second_btn = next(ln for ln in lines if 'button "OK"' in ln and "[nth=1]" in ln)
    assert first_btn != second_btn


def test_aria_snapshot_stable_refs_across_runs() -> None:
    """Same input twice → same ref→element mapping."""
    src = _walked_example_input()
    a = build_role_snapshot_from_aria_snapshot(src)
    b = build_role_snapshot_from_aria_snapshot(src)
    assert {k: (v.role, v.name, v.nth) for k, v in a.refs.items()} == {
        k: (v.role, v.name, v.nth) for k, v in b.refs.items()
    }


# ─── interactive mode ─────────────────────────────────────────────────


def test_interactive_mode_emits_only_interactive() -> None:
    src = (
        '- main "Content":\n'
        '  - heading "Welcome"\n'
        '  - button "OK"\n'
        '  - paragraph\n'
        '  - link "Home"\n'
    )
    result = build_role_snapshot_from_aria_snapshot(src, interactive=True)
    # Only button + link have refs; main + heading + paragraph dropped.
    assert all(r.role in INTERACTIVE_ROLES for r in result.refs.values())
    assert {r.role for r in result.refs.values()} == {"button", "link"}
    # snapshot text contains only those elements as flat list (no main/heading).
    assert "main" not in result.snapshot
    assert "heading" not in result.snapshot
    assert "button" in result.snapshot
    assert "link" in result.snapshot


def test_interactive_mode_no_interactive_message() -> None:
    src = '- main "Content":\n  - heading "Welcome"\n'
    result = build_role_snapshot_from_aria_snapshot(src, interactive=True)
    assert result.snapshot == "(no interactive elements)"
    assert result.refs == {}


# ─── compact mode ─────────────────────────────────────────────────────


def test_compact_mode_drops_unnamed_structural() -> None:
    src = (
        "- generic:\n"
        "  - generic:\n"
        '    - button "OK"\n'
    )
    result = build_role_snapshot_from_aria_snapshot(src, compact=True)
    # In non-compact, the generics would be kept; in compact, they're dropped
    # (unnamed structural).
    assert "generic" not in result.snapshot
    assert "button" in result.snapshot


def test_compact_mode_keeps_named_structural() -> None:
    src = (
        '- generic "label":\n'
        '  - button "OK"\n'
    )
    result = build_role_snapshot_from_aria_snapshot(src, compact=True)
    # Named structural is kept verbatim (no ref though).
    assert 'generic "label"' in result.snapshot


# ─── non-classified roles ─────────────────────────────────────────────


def test_unclassified_roles_kept_without_ref() -> None:
    src = "- paragraph:\n  - text \"Hello world\"\n"
    result = build_role_snapshot_from_aria_snapshot(src)
    # paragraph + text are not in any of the 3 sets — kept verbatim, no refs.
    assert result.refs == {}
    assert "paragraph" in result.snapshot
    assert "text" in result.snapshot


def test_max_depth_truncates_tree() -> None:
    src = (
        '- main "Content":\n'
        '  - article "First":\n'
        '    - button "Deep"\n'
    )
    # max_depth=1 should drop the depth-2 button.
    result = build_role_snapshot_from_aria_snapshot(src, max_depth=1)
    assert all(r.role != "button" for r in result.refs.values())
    assert "Deep" not in result.snapshot


# ─── content-with-no-name skip ────────────────────────────────────────


def test_content_role_without_name_gets_no_ref() -> None:
    src = "- main:\n  - heading\n"
    result = build_role_snapshot_from_aria_snapshot(src)
    # Both main and heading are content roles; without `name`, neither
    # gets a ref. The lines are kept verbatim.
    assert result.refs == {}
    assert "main" in result.snapshot
    assert "heading" in result.snapshot
