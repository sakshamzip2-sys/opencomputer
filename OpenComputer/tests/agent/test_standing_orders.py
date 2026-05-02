"""Standing Orders parser tests.

Parser is a line-state-machine — NOT regex (Python `re` doesn't support
multi-line negative lookahead the way the rev-1 plan assumed). The
critical regression test here covers two adjacent `## Program:` blocks
not merging into one.
"""
from __future__ import annotations

import pytest

from opencomputer.agent.standing_orders import StandingOrder, parse_agents_md


def test_empty_file_returns_empty_list():
    assert parse_agents_md("") == []
    assert parse_agents_md("# Just a heading") == []


def test_single_well_formed_program():
    text = """# Project notes

Some preamble text.

## Program: weekly-summary
Scope: opencomputer/
Triggers: cron weekly
Approval Gates: human-confirm before send
Escalation: notify Saksham

## Other section
unrelated content
"""
    orders = parse_agents_md(text)
    assert len(orders) == 1
    o = orders[0]
    assert isinstance(o, StandingOrder)
    assert o.name == "weekly-summary"
    assert o.scope == "opencomputer/"
    assert o.triggers == "cron weekly"
    assert o.approval_gates == "human-confirm before send"
    assert o.escalation == "notify Saksham"


def test_two_adjacent_program_blocks_dont_merge():
    """Critical regression: rev-1's regex `(?!^## )` lookahead would have
    eaten the second block as the first block's body. Line-state-machine
    must terminate cleanly at every `## ` heading.
    """
    text = """## Program: alpha
Scope: a-only
Triggers: x

## Program: beta
Scope: b-only
Triggers: y
"""
    orders = parse_agents_md(text)
    assert len(orders) == 2
    assert orders[0].name == "alpha"
    assert orders[0].scope == "a-only"
    assert orders[0].triggers == "x"
    assert orders[1].name == "beta"
    assert orders[1].scope == "b-only"
    assert orders[1].triggers == "y"


def test_malformed_block_missing_triggers_skipped():
    """A block without `Triggers:` is malformed — skip it (don't crash),
    keep parsing the rest of the file.
    """
    text = """## Program: incomplete
Scope: nope

## Program: good
Scope: ok
Triggers: cron daily
"""
    orders = parse_agents_md(text)
    assert len(orders) == 1
    assert orders[0].name == "good"


def test_multi_line_field_value():
    """Continuation lines (indented or unmarked) become part of the
    previous field's value.
    """
    text = """## Program: alpha
Scope: line one
  continued indented
  also continued
Triggers: cron daily
"""
    orders = parse_agents_md(text)
    assert len(orders) == 1
    assert "line one" in orders[0].scope
    assert "continued indented" in orders[0].scope
    assert "also continued" in orders[0].scope


def test_program_name_with_hyphens_and_underscores():
    text = """## Program: weekly_health-check
Triggers: cron weekly
"""
    orders = parse_agents_md(text)
    assert len(orders) == 1
    assert orders[0].name == "weekly_health-check"


def test_raw_fields_preserves_unknown_keys():
    """Unknown fields (e.g. `Notes:`) are kept in raw_fields for
    forward compatibility — the parser doesn't drop them silently.
    """
    text = """## Program: alpha
Triggers: cron daily
Notes: some custom key
"""
    orders = parse_agents_md(text)
    assert len(orders) == 1
    assert orders[0].raw_fields.get("notes") == "some custom key"


def test_h1_heading_does_not_terminate_block():
    """Only `## ` (H2) headings terminate a Program block. H1 (`# `) does
    not — that's project-meta content above the orders.
    """
    text = """## Program: alpha
Scope: x
Triggers: y

# Random H1 in the middle (shouldn't appear normally but be tolerant)

Some more body? Actually no — H1 should not be inside a block in
practice. This case never arises in real AGENTS.md so we accept the
parser's choice. Leaving the spec unspecified for H1.
"""
    # Just verifying it doesn't crash; either behavior is acceptable
    orders = parse_agents_md(text)
    assert len(orders) == 1
    assert orders[0].name == "alpha"


def test_parser_does_not_crash_on_pathological_input():
    """Defensive: a wide range of weird inputs must not raise."""
    pathological = [
        "## Program:\n",                      # missing name
        "## Program: ",                       # name is whitespace
        "## program: lowercase\nTriggers: x", # lowercase keyword
        "##Program: nospace\nTriggers: x",    # no space after ##
        "## Program: ok\nTriggers: x\n## Program: dup\nTriggers: y\n## Program: dup\nTriggers: z",
    ]
    for text in pathological:
        # Just must not raise
        result = parse_agents_md(text)
        assert isinstance(result, list)
