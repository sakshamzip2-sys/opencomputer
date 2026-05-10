"""``opencomputer activate`` — interactive wizard that scaffolds dormant features.

Each sub-area detects whether the user has already configured it, and if not,
proposes a sensible default. Idempotent — running twice on a fully-populated
profile is a no-op.

Sub-areas (executed in dependency order):

1. mcp       — write 3 commented-out MCP server stubs to config.yaml
2. agents    — drop 3 user-template starters into <profile>/agents/
3. bindings  — write a default-only bindings.yaml
4. presets   — write a `minimal` preset (no extension plugins)
5. rules     — drop one starter rule (deny .env writes)

Non-interactive use: ``oc activate --accept-defaults`` accepts every prompt
without asking. Useful for CI-style first-run setup.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console

from opencomputer.agent.config import _home

activate_app = typer.Typer(
    name="activate",
    help="Interactive wizard that scaffolds dormant features.",
    no_args_is_help=False,
    invoke_without_command=True,
)
_console = Console()


# ─── Inline starter content ─────────────────────────────────────────────────

_STARTER_AGENTS: dict[str, str] = {
    "test-writer.md": (
        "---\n"
        "name: test-writer\n"
        "description: Given a code change (default: unstaged git diff), write pytest test cases that cover the new branches and edge cases. Reports the test file path(s) and the pytest command to run.\n"
        "tools: Read, Grep, Glob, Bash, Write, Edit\n"
        "---\n\n"
        "You are an expert test author. By default, read `git diff` to identify code under test. Write pytest tests that:\n\n"
        "- Cover EVERY new branch / conditional you can identify in the diff.\n"
        "- Include at least one edge case per public function (empty input, max-size input, None, error path).\n"
        "- Follow the project's existing test conventions — read `tests/conftest.py` and one nearby existing test file before writing new ones to match style.\n"
        "- Use the same pytest fixtures the project already has rather than rolling your own.\n"
        "- Place tests under `tests/` mirroring the module path of the code under test (e.g. `opencomputer/foo/bar.py` → `tests/foo/test_bar.py`).\n\n"
        "Return: the new test file path(s), a one-line summary of each test, and the exact `pytest tests/path -v` command to verify.\n"
    ),
    "doc-writer.md": (
        "---\n"
        "name: doc-writer\n"
        "description: Update README.md, CHANGELOG.md, and docstrings to match recent code changes. Reports diff of the doc files touched.\n"
        "tools: Read, Grep, Glob, Edit, Write, Bash\n"
        "---\n\n"
        "You are a documentation maintainer. Read `git diff` to see what changed, then:\n\n"
        "- Update affected docstrings on touched public functions / classes.\n"
        "- Update `README.md` only if a public-facing surface changed (CLI flag, env var, install step). Don't update for refactors.\n"
        "- Append a Keep-a-Changelog entry to `CHANGELOG.md` under the appropriate header (Added / Changed / Fixed / Removed).\n"
        "- Match the existing tone, voice, and section structure — read the surrounding context before writing.\n\n"
        "Return: the doc files modified and a unified diff for each. Stop without committing.\n"
    ),
    "planner.md": (
        "---\n"
        "name: planner\n"
        "description: Decompose a feature request into 3-5 testable milestones, each with files-to-touch + acceptance criteria. Useful when starting a non-trivial task.\n"
        "tools: Read, Grep, Glob, WebFetch, TodoWrite\n"
        "---\n\n"
        "You are a senior engineer turning a feature request into a concrete plan. For the request the user provides:\n\n"
        "1. Read the project entry points (CLAUDE.md, README.md, top-level package init) to ground in the codebase.\n"
        "2. Decompose the request into 3-5 milestones. Each milestone must:\n"
        "   - Be independently testable / shippable.\n"
        "   - List the exact files to create or modify.\n"
        "   - Define one acceptance test (a specific pytest invocation that proves it works).\n"
        "3. Flag dependencies between milestones (which one MUST ship before the next).\n"
        "4. Surface the riskiest milestone explicitly so the user can prioritize de-risking it.\n\n"
        "Output as a numbered list with sub-bullets. End with a one-sentence recommended-next-step. Don't write code — that's a separate agent.\n"
    ),
}

_STARTER_MCP_BLOCK_COMMENT = """\
# MCP servers — uncomment one or more, fill in any placeholders, save, then restart.
# See https://modelcontextprotocol.io for the full server catalog.
#
# Example: filesystem (lets the agent read/write under one specific dir)
# - name: filesystem
#   transport: stdio
#   command: npx
#   args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/${USER}/Documents"]
#   enabled: true
#
# Example: github (PR / issue / repo introspection — needs GITHUB_TOKEN env var)
# - name: github
#   transport: stdio
#   command: uvx
#   args: ["mcp-server-github"]
#   env:
#     GITHUB_TOKEN: "${GITHUB_TOKEN}"
#   enabled: true
#
# Example: fetch (HTTP fetch tool — no auth required)
# - name: fetch
#   transport: stdio
#   command: uvx
#   args: ["mcp-server-fetch"]
#   enabled: true
"""

_STARTER_BINDINGS_YAML = """\
# Gateway routing rules. Default-only setup: every channel/event maps to the
# `default` profile. Add per-channel overrides under `bindings:`.
#
# Schema:
#   default_profile: <profile name>
#   bindings:
#     - match: {platform: telegram, chat_id: "12345"}
#       profile: coding
#       priority: 10
#
# Run `oc bindings list` to see what's loaded.

default_profile: default
bindings: []
"""

_STARTER_RULE_NO_ENV_BODY = """\
---
name: no-env-writes
paths: ["**/*.env", "**/.env", "**/.env.*"]
priority: 10
---

Do not write to `.env` files. They contain secrets that belong to the user, not
the agent. If the user asks for an env-var change, propose the diff in a
message and wait for them to apply it manually. If they confirm and explicitly
ask you to write the file, you may proceed — but never write `.env` proactively.
"""


# ─── Helpers ────────────────────────────────────────────────────────────────


def _confirm(question: str, *, accept_defaults: bool, default: bool = True) -> bool:
    """Prompt the user; in --accept-defaults mode, return ``default`` immediately."""
    if accept_defaults:
        return default
    return typer.confirm(question, default=default)


def _profile_home() -> Path:
    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    return home


# ─── Sub-area: mcp ──────────────────────────────────────────────────────────


def _activate_mcp(*, accept_defaults: bool) -> str:
    """Append MCP server stubs to config.yaml if section is empty."""
    config_path = _profile_home() / "config.yaml"
    raw: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raw = {}
        except yaml.YAMLError as exc:
            return f"mcp: skip (config.yaml parse error: {exc})"

    mcp_section = raw.get("mcp") or {}
    existing_servers = mcp_section.get("servers") or []
    if existing_servers:
        return f"mcp: skip ({len(existing_servers)} server(s) already configured)"

    if not _confirm(
        "MCP servers: write 3 commented-out starter stubs to config.yaml?",
        accept_defaults=accept_defaults,
    ):
        return "mcp: skipped (user declined)"

    # Append the comment block to config.yaml verbatim — leaves the actual
    # `servers:` list empty so loading config.yaml stays valid YAML.
    existing_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if "MCP servers — uncomment one or more" in existing_text:
        return "mcp: skip (starter block already present)"
    appended = existing_text.rstrip() + "\n\n" + _STARTER_MCP_BLOCK_COMMENT
    config_path.write_text(appended, encoding="utf-8")
    return f"mcp: wrote starter stubs to {config_path}"


# ─── Sub-area: agents ───────────────────────────────────────────────────────


def _activate_agents(*, accept_defaults: bool) -> str:
    """Drop starter agent templates into <profile>/agents/ if missing."""
    agents_dir = _profile_home() / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    existing = {p.name for p in agents_dir.glob("*.md")}
    to_write = {name: body for name, body in _STARTER_AGENTS.items() if name not in existing}
    if not to_write:
        return f"agents: skip (all 3 starters already present in {agents_dir})"

    if not _confirm(
        f"Agents: write {len(to_write)} starter template(s) ({', '.join(sorted(to_write))}) to {agents_dir}?",
        accept_defaults=accept_defaults,
    ):
        return "agents: skipped (user declined)"

    for name, body in to_write.items():
        (agents_dir / name).write_text(body, encoding="utf-8")
    return f"agents: wrote {len(to_write)} template(s) to {agents_dir}"


# ─── Sub-area: bindings ─────────────────────────────────────────────────────


def _activate_bindings(*, accept_defaults: bool) -> str:
    """Write a default-only bindings.yaml if it doesn't exist."""
    path = _profile_home() / "bindings.yaml"
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return f"bindings: skip (already present at {path})"

    if not _confirm(
        "Bindings: write a default-route-only bindings.yaml?",
        accept_defaults=accept_defaults,
    ):
        return "bindings: skipped (user declined)"

    path.write_text(_STARTER_BINDINGS_YAML, encoding="utf-8")
    return f"bindings: wrote {path}"


# ─── Sub-area: presets ──────────────────────────────────────────────────────


def _activate_presets(*, accept_defaults: bool) -> str:
    """Write a `minimal` preset if no presets exist."""
    presets_dir = Path.home() / ".opencomputer" / "presets"
    presets_dir.mkdir(parents=True, exist_ok=True)
    existing = list(presets_dir.glob("*.yaml"))
    if existing:
        return f"presets: skip ({len(existing)} preset(s) already in {presets_dir})"

    if not _confirm(
        "Presets: write a `minimal` starter preset (no extension plugins)?",
        accept_defaults=accept_defaults,
    ):
        return "presets: skipped (user declined)"

    minimal = presets_dir / "minimal.yaml"
    minimal.write_text("plugins: []\n", encoding="utf-8")
    return f"presets: wrote {minimal}"


# ─── Sub-area: rules ────────────────────────────────────────────────────────


def _activate_rules(*, accept_defaults: bool) -> str:
    """Drop one starter rule (deny .env writes) if rules dir is empty."""
    rules_dir = _profile_home() / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    existing = list(rules_dir.glob("*.md"))
    if existing:
        return f"rules: skip ({len(existing)} rule(s) already in {rules_dir})"

    if not _confirm(
        "Rules: write a starter rule that denies writes to .env files?",
        accept_defaults=accept_defaults,
    ):
        return "rules: skipped (user declined)"

    rule = rules_dir / "no-env-writes.md"
    rule.write_text(_STARTER_RULE_NO_ENV_BODY, encoding="utf-8")
    return f"rules: wrote {rule}"


# ─── Top-level command ──────────────────────────────────────────────────────


_SUB_AREAS = [
    ("mcp", _activate_mcp),
    ("agents", _activate_agents),
    ("bindings", _activate_bindings),
    ("presets", _activate_presets),
    ("rules", _activate_rules),
]


def _run_all(*, accept_defaults: bool) -> int:
    """Run every sub-area in order. Return 0 unless any sub-area errored."""
    _console.print(
        "[bold]oc activate[/bold] — scaffolding dormant features."
        " Each step asks before writing; no-op if already configured.\n"
    )
    failures = 0
    for label, fn in _SUB_AREAS:
        try:
            outcome = fn(accept_defaults=accept_defaults)
            if outcome.startswith(f"{label}:"):
                outcome = outcome[len(label) + 1:].lstrip()
            _console.print(f"  [cyan]{label}[/cyan]: {outcome}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            _console.print(f"  [red]{label}[/red]: error — {exc}")
    if failures:
        _console.print(f"\n[red]{failures} sub-area(s) failed.[/red]")
    else:
        _console.print(
            "\n[green]Done.[/green] Run [bold]oc doctor[/bold] to verify, then "
            "edit any of the written files to taste."
        )
    return failures


@activate_app.callback()
def _activate_root(
    ctx: typer.Context,
    accept_defaults: Annotated[
        bool, typer.Option("--accept-defaults", "-y", help="Accept every prompt without asking (CI-friendly).")
    ] = False,
) -> None:
    """Run every activation sub-area in order. Idempotent."""
    if ctx.invoked_subcommand is None:
        rc = _run_all(accept_defaults=accept_defaults)
        raise typer.Exit(rc)


@activate_app.command("mcp")
def cmd_mcp(
    accept_defaults: Annotated[bool, typer.Option("--accept-defaults", "-y")] = False,
) -> None:
    """Activate just the MCP sub-area."""
    typer.echo(_activate_mcp(accept_defaults=accept_defaults))


@activate_app.command("agents")
def cmd_agents(
    accept_defaults: Annotated[bool, typer.Option("--accept-defaults", "-y")] = False,
) -> None:
    """Activate just the agent-template sub-area."""
    typer.echo(_activate_agents(accept_defaults=accept_defaults))


@activate_app.command("bindings")
def cmd_bindings(
    accept_defaults: Annotated[bool, typer.Option("--accept-defaults", "-y")] = False,
) -> None:
    """Activate just the bindings sub-area."""
    typer.echo(_activate_bindings(accept_defaults=accept_defaults))


@activate_app.command("presets")
def cmd_presets(
    accept_defaults: Annotated[bool, typer.Option("--accept-defaults", "-y")] = False,
) -> None:
    """Activate just the presets sub-area."""
    typer.echo(_activate_presets(accept_defaults=accept_defaults))


@activate_app.command("rules")
def cmd_rules(
    accept_defaults: Annotated[bool, typer.Option("--accept-defaults", "-y")] = False,
) -> None:
    """Activate just the rules sub-area."""
    typer.echo(_activate_rules(accept_defaults=accept_defaults))


__all__ = ["activate_app"]
