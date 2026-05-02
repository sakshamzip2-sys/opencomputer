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
