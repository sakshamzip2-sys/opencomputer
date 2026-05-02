"""'oc browser' subcommand — recipe-driven scrapes against logged-in Chrome.

Two layers:
  - Recipe-first: 'oc browser run <site> <verb>' looks up a YAML recipe.
  - LLM-fallback (--llm-fallback): one-off LLM-driven scrape if no recipe.
    v1 ships this as a STUB that exits 2 with a 'not yet implemented'
    message. Phase 5 (next-session work) wires the real LLM-fallback path.

Default behaviour (no flag, missing recipe) is exit 1 with helpful
options pointing at the user's recipe dir.
"""

from __future__ import annotations

import typer

browser_app = typer.Typer(
    help=(
        "Recipe-driven browser commands. "
        "'oc browser list' to see installed recipes."
    ),
    no_args_is_help=True,
)


@browser_app.command("list")
def list_command():
    """List all installed recipes (profile-local + bundled)."""
    from opencomputer.recipes import list_recipes

    names = list_recipes()
    if not names:
        typer.echo(
            "No recipes installed. "
            "Add one to ~/.opencomputer/<profile>/recipes/."
        )
        return
    for name in names:
        typer.echo(name)


@browser_app.command("show")
def show_command(site: str = typer.Argument(...)):
    """Show a recipe's commands and pipeline summary."""
    from opencomputer.recipes import load_recipe

    try:
        recipe = load_recipe(site)
    except KeyError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Recipe: {recipe.name}")
    if recipe.description:
        typer.echo(f"  {recipe.description}")
    typer.echo("\nCommands:")
    for verb, cmd in recipe.commands.items():
        typer.echo(f"  {verb}: {cmd.description or '(no description)'}")
        kinds = [list(s.keys())[0] for s in cmd.pipeline]
        typer.echo(f"    pipeline ({len(cmd.pipeline)} steps): {', '.join(kinds)}")
        typer.echo(f"    formats: {cmd.formats}")


@browser_app.command("chrome")
def chrome_command():
    """Print the Chrome launch command for CDP attach mode."""
    import importlib.util as _ilu
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    spec = _ilu.spec_from_file_location(
        "_chrome_launch_for_cli",
        str(repo / "extensions" / "browser-control" / "chrome_launch.py"),
    )
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    try:
        cmd = mod.chrome_launch_command()
    except NotImplementedError as exc:
        typer.echo(f"# {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo("# Run this in a SEPARATE terminal to launch Chrome with CDP enabled:")
    typer.echo(cmd)
    typer.echo()
    typer.echo("# Then in your shell:")
    typer.echo("export OPENCOMPUTER_BROWSER_CDP_URL=http://localhost:9222")
    typer.echo()
    typer.echo("# Now 'oc browser run <site> <verb>' will use your real Chrome.")


@browser_app.command()
def run(
    site: str = typer.Argument(...),
    verb: str = typer.Argument(...),
    limit: int = typer.Option(10, "--limit", "-n"),
    fmt: str = typer.Option("json", "--format", "-f"),
    llm_fallback: bool = typer.Option(False, "--llm-fallback"),
):
    """Run a recipe: 'oc browser run <site> <verb>'.

    NOTE on '--llm-fallback': v1 ships this flag as a STUB that exits 2
    with a "not yet implemented" message. Phase 5 (next-session) wires
    the real LLM-fallback path. Default behaviour (no flag, missing
    recipe) is exit 1 with helpful options.
    """
    from opencomputer.recipes import run_recipe
    from opencomputer.recipes.fetcher import httpx_fetcher

    try:
        out = run_recipe(
            site=site,
            verb=verb,
            args={"limit": limit},
            fetcher=httpx_fetcher,
            fmt=fmt,
        )
    except KeyError as e:
        if llm_fallback:
            typer.echo(
                f"# LLM fallback for {site}/{verb} not yet implemented (Phase 5).",
                err=True,
            )
            typer.echo(f"# Reason no recipe matched: {e}", err=True)
            raise typer.Exit(code=2)
        typer.echo(
            f"No recipe for {site}/{verb}. Options:\n"
            f"  - oc browser run {site} {verb} --llm-fallback   "
            f"# one-off LLM scrape (v1 stub)\n"
            f"  - Add a recipe to ~/.opencomputer/<profile>/recipes/{site}.yaml\n"
            f"  - oc browser list   # see installed recipes",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(out)


@browser_app.command("cascade")
def cascade_command(
    url: str = typer.Argument(...),
):
    """Probe a URL with PUBLIC -> COOKIE -> HEADER strategies.

    Reports which strategy succeeds. No LLM key required. Honors
    OPENCOMPUTER_BROWSER_CDP_URL for the cookie strategy (skipped
    silently if not set).
    """
    from opencomputer.recipes.discovery import run_cascade

    result = run_cascade(url)
    typer.echo(f"URL:           {url}")
    typer.echo(f"Strategy:      {result.strategy or '(all failed)'}")
    typer.echo(f"Status code:   {result.status_code}")
    typer.echo(f"Attempted:     {result.attempted}")
    if result.strategy is None:
        raise typer.Exit(code=1)


@browser_app.command("explore")
def explore_command(
    url: str = typer.Argument(...),
    site: str = typer.Option(..., "--site", help="Slug to namespace artifacts under"),
    output_dir: typer.FileText = typer.Option(
        None, "--output-dir",
        help="Where to write artifacts (default: ./.opencli/explore/<site>/)",
    ),
):
    """Navigate the URL with network capture, write endpoints.json.

    No LLM is involved — pure observation. Writes:
      <output>/endpoints.json   list of {url, method, status, headers, ...}

    Sensitive headers (Authorization, Cookie, X-API-Key, X-Auth-Token)
    are REDACTED before persisting.

    Run synthesize next to turn endpoints.json into a YAML recipe
    (requires LLM API key).
    """
    import asyncio
    from pathlib import Path

    from opencomputer.recipes.discovery import explore_endpoints

    out = Path(f".opencli/explore/{site}") if output_dir is None else Path(str(output_dir))

    captured = asyncio.run(explore_endpoints(url, output_dir=out))
    typer.echo(f"Captured {len(captured)} endpoints -> {out / 'endpoints.json'}")
    typer.echo(
        "Run 'oc browser synthesize {site}' to turn this into a YAML recipe "
        "(requires ANTHROPIC_API_KEY or OPENAI_API_KEY)."
    )


@browser_app.command("synthesize")
def synthesize_command(
    site: str = typer.Argument(...),
):
    """Read explore artifacts; LLM writes a YAML recipe. STUB.

    v2 ships this as a STUB requiring ANTHROPIC_API_KEY / OPENAI_API_KEY.
    Phase 5 (next-session) wires the LLM-driven YAML synthesis. Today,
    this command exits 2 with a clear message about the missing key
    and points at the explore artifacts for manual recipe authoring.
    """
    import os
    from pathlib import Path

    artifacts = Path(f".opencli/explore/{site}/endpoints.json")
    if not artifacts.exists():
        typer.echo(
            f"No explore artifacts at {artifacts}. "
            f"Run 'oc browser explore <url> --site {site}' first.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        typer.echo(
            f"# LLM-driven synthesize is a Phase 5 stub. Needs:\n"
            f"#   ANTHROPIC_API_KEY or OPENAI_API_KEY in env.\n"
            f"#\n"
            f"# Manual workaround for {site}:\n"
            f"#   1. Read {artifacts} to find the most useful endpoint\n"
            f"#   2. Author a recipe at ~/.opencomputer/<profile>/recipes/{site}.yaml\n"
            f"#   3. See extensions/browser-recipes/recipes/hackernews.yaml for the shape",
            err=True,
        )
        raise typer.Exit(code=2)

    typer.echo(
        "# Synthesize is a Phase 5 stub even with API key set. "
        "# The LLM prompt + iteration logic is documented in "
        "docs/superpowers/plans/2026-05-02-opencli-discovery-NEXT-SESSION.md",
        err=True,
    )
    raise typer.Exit(code=2)


@browser_app.command("generate")
def generate_command(
    url: str = typer.Argument(...),
    goal: str = typer.Option(..., "--goal", help="What you want (e.g. 'hot' / 'feed')"),
    site: str = typer.Option(..., "--site", help="Slug for the new recipe"),
):
    """One-shot: explore + synthesize + register. STUB inherits synthesize's key reqs.

    The composed flow:
      1. oc browser explore <url> --site <site>
      2. oc browser synthesize <site>
    The second step needs an LLM API key (Phase 5).

    v2 today: runs explore (works), then exits with synthesize's stub message.
    """
    import asyncio
    import os
    from pathlib import Path

    from opencomputer.recipes.discovery import explore_endpoints

    out = Path(f".opencli/explore/{site}")
    typer.echo(f"# Step 1: explore {url} -> {out / 'endpoints.json'}")
    captured = asyncio.run(explore_endpoints(url, output_dir=out))
    typer.echo(f"# Captured {len(captured)} endpoints")

    typer.echo(f"# Step 2: synthesize recipe (goal={goal})")
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        typer.echo(
            f"#\n"
            f"# Synthesize step needs ANTHROPIC_API_KEY or OPENAI_API_KEY (Phase 5 stub).\n"
            f"# Explore artifacts are saved at {out}; run 'oc browser synthesize {site}'\n"
            f"# manually after setting an API key, or hand-author a recipe.",
            err=True,
        )
        raise typer.Exit(code=2)

    typer.echo("# Synthesize stub — Phase 5 not yet wired even with API key.", err=True)
    raise typer.Exit(code=2)
