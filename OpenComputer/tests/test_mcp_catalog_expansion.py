"""MCP catalog expansion + new ``catalog`` synonym command.

Round 4 Item 2. The bundled `PRESETS` dict shipped 5 entries; we
extend it to 20 covering the most-requested servers (notion, slack,
linear, sentry, sqlite, gitlab, etc.). ``opencomputer mcp catalog``
is added as a friendlier-named alias for the existing ``mcp presets``.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_catalog_has_at_least_15_entries() -> None:
    """The expansion brought the bundled list from 5 → ~20 servers."""
    from opencomputer.mcp.presets import PRESETS

    assert len(PRESETS) >= 15, (
        f"expected ≥15 catalog entries after expansion; got {len(PRESETS)}"
    )


def test_catalog_includes_major_third_party_servers() -> None:
    """Spot-check the well-known MCPs people ask about."""
    from opencomputer.mcp.presets import PRESETS

    for required_slug in (
        "notion",
        "linear",
        "sentry",
        "sqlite",
        "gitlab",
        "context7",
    ):
        assert required_slug in PRESETS, (
            f"expansion missing {required_slug!r}; user-survey data shows "
            f"this is in the most-requested set"
        )


def test_every_preset_has_homepage_url() -> None:
    """Catalog entries must point at official docs so users can verify
    they're installing what they expect (no surprise typosquatting)."""
    from opencomputer.mcp.presets import PRESETS

    for slug, preset in PRESETS.items():
        assert preset.homepage.startswith("https://"), (
            f"preset {slug!r} has no homepage; user can't audit before installing"
        )


def test_every_preset_has_a_description() -> None:
    from opencomputer.mcp.presets import PRESETS

    for slug, preset in PRESETS.items():
        assert preset.description, f"preset {slug!r} has empty description"
        assert len(preset.description) >= 20, (
            f"preset {slug!r} description is too terse: {preset.description!r}"
        )


def test_required_env_vars_appear_in_description_for_secrets() -> None:
    """If a preset declares required_env, the description should
    mention how to get the credential — otherwise users hit a wall
    after install with no idea what to do."""
    from opencomputer.mcp.presets import PRESETS

    for slug, preset in PRESETS.items():
        if not preset.required_env:
            continue
        # At least one env var name should appear in the description,
        # OR a reference to where to get it (e.g. "from notion.so").
        env_in_desc = any(
            env_var in preset.description for env_var in preset.required_env
        )
        url_or_get_hint = any(
            keyword in preset.description.lower()
            for keyword in (
                "requires",
                "create",
                "get one",
                "from ",
                "pass --",
                "set ",
                "starts with",
                "free tier",
            )
        )
        assert env_in_desc or url_or_get_hint, (
            f"preset {slug!r} has required_env={preset.required_env} but "
            f"description doesn't tell users where to get them: "
            f"{preset.description!r}"
        )


def test_catalog_command_runs_and_lists_entries(runner: CliRunner) -> None:
    """`opencomputer mcp catalog` is the new friendlier-named alias.
    Both old `presets` and new `catalog` print the same listing."""
    from opencomputer.cli import app

    result = runner.invoke(app, ["mcp", "catalog"])
    assert result.exit_code == 0
    assert "filesystem" in result.stdout
    assert "notion" in result.stdout


def test_presets_command_still_works(runner: CliRunner) -> None:
    """Backwards-compat: existing scripts using `mcp presets` keep working."""
    from opencomputer.cli import app

    result = runner.invoke(app, ["mcp", "presets"])
    assert result.exit_code == 0
    assert "filesystem" in result.stdout


def test_catalog_and_presets_produce_same_count(runner: CliRunner) -> None:
    """`catalog` is just an alias — both list the same N entries.
    Use the title row count which prints '(N)'."""
    from opencomputer.cli import app

    cat_out = runner.invoke(app, ["mcp", "catalog"]).stdout
    pre_out = runner.invoke(app, ["mcp", "presets"]).stdout
    # Both go through mcp_presets which renders a Table titled
    # "MCP Presets (N)". Count must match.
    import re

    cat_n = re.search(r"\((\d+)\)", cat_out)
    pre_n = re.search(r"\((\d+)\)", pre_out)
    assert cat_n is not None and pre_n is not None
    assert cat_n.group(1) == pre_n.group(1)


def test_no_duplicate_slugs() -> None:
    """Defence-in-depth — Python dicts can't actually have duplicate
    keys but a future refactor could merge two dicts and lose entries.
    Test that the slug keys match each preset's internal slug field."""
    from opencomputer.mcp.presets import PRESETS

    for key, preset in PRESETS.items():
        assert preset.slug == key, (
            f"preset under key {key!r} has internal slug={preset.slug!r} — "
            f"split-brain. The dict key MUST match preset.slug."
        )
