"""'oc eval' subcommand: run, generate, regress."""

from __future__ import annotations

from pathlib import Path

import typer

from opencomputer.evals.baseline import compare_to_baseline, save_baseline
from opencomputer.evals.report import format_report
from opencomputer.evals.runner import run_site
from opencomputer.evals.sites import SITES, get_site

eval_app = typer.Typer(help="Run, generate, and regress LLM-decision-point evals.")


@eval_app.command("run")
def run_command(
    site: str = typer.Argument(..., help="Site name. 'all' to run every registered site."),
    save_baseline_flag: bool = typer.Option(
        False, "--save-baseline", help="Persist this run's accuracy as the new baseline."
    ),
    cases_dir: Path = typer.Option(
        Path("evals/cases"), "--cases-dir", help="Directory containing <site>.jsonl files."
    ),
    baselines_dir: Path = typer.Option(
        Path("evals/baselines"), "--baselines-dir", help="Directory for baseline snapshots."
    ),
    grader_model: str | None = typer.Option(
        None, "--grader-model", help="Explicit model for rubric grader (required for non-Anthropic setups)."
    ),
):
    """Run evals for one or all sites."""
    target_sites = list(SITES) if site == "all" else [site]

    for s in target_sites:
        eval_site = get_site(s)  # validates name; raises if unknown

        grader_provider = None
        if eval_site.grader == "rubric":
            try:
                from opencomputer.evals.providers import get_grader_provider
                grader_provider = get_grader_provider(model_override=grader_model)
            except (ImportError, RuntimeError) as e:
                typer.echo(
                    f"Skipping rubric site {s!r} — grader provider unavailable: {e}",
                    err=True,
                )
                continue

        report = run_site(
            site_name=s,
            cases_dir=cases_dir,
            grader_provider=grader_provider,
        )
        diff = compare_to_baseline(report, baselines_dir=baselines_dir)
        typer.echo(format_report(report, baseline_diff=diff))

        if save_baseline_flag:
            save_baseline(
                report,
                baselines_dir=baselines_dir,
                model="claude-sonnet-4-6",
                provider="anthropic",
            )


@eval_app.command("generate")
def generate_command(
    site: str = typer.Argument(...),
    n: int = typer.Option(30, "-n", help="Number of cases to generate."),
    cases_dir: Path = typer.Option(Path("evals/cases"), "--cases-dir"),
    grader_model: str | None = typer.Option(
        None, "--grader-model", help="Override the model used to generate cases."
    ),
):
    """Generate test cases via LLM. Writes to <site>.candidates.jsonl."""
    from opencomputer.evals.generator import generate_cases

    try:
        from opencomputer.evals.providers import get_grader_provider
    except ImportError:
        typer.echo(
            "Generator provider helper not available. "
            "evals/providers.py is added in Task 1.12.",
            err=True,
        )
        raise typer.Exit(code=2)

    provider = get_grader_provider(model_override=grader_model)
    out_path = generate_cases(
        site_name=site, n=n, cases_dir=cases_dir, generator_provider=provider
    )
    typer.echo(f"Generated candidates -> {out_path}")


@eval_app.command("regress")
def regress_command(
    site: str = typer.Argument("all"),
    cases_dir: Path = typer.Option(Path("evals/cases"), "--cases-dir"),
    baselines_dir: Path = typer.Option(Path("evals/baselines"), "--baselines-dir"),
    grader_model: str | None = typer.Option(
        None, "--grader-model", help="Explicit model for rubric grader (required to regress rubric-graded sites)."
    ),
):
    """Run sites and exit non-zero if any accuracy regressed past threshold.

    Rubric-graded sites are skipped if no grader provider is wired (no
    --grader-model passed AND no Anthropic provider available). The
    regression check still works on the deterministic-graded sites in
    that case.
    """
    threshold = 0.05  # 5pp accuracy drop triggers regression
    target_sites = list(SITES) if site == "all" else [site]
    regressed = []
    skipped: list[tuple[str, str]] = []
    for s in target_sites:
        eval_site = get_site(s)

        grader_provider = None
        if eval_site.grader == "rubric":
            try:
                from opencomputer.evals.providers import get_grader_provider
                grader_provider = get_grader_provider(model_override=grader_model)
            except (ImportError, RuntimeError) as e:
                skipped.append((s, str(e)))
                continue

        report = run_site(
            site_name=s,
            cases_dir=cases_dir,
            grader_provider=grader_provider,
        )
        diff = compare_to_baseline(report, baselines_dir=baselines_dir)
        if diff is not None and diff.accuracy_delta < -threshold:
            regressed.append((s, diff.accuracy_delta))

    for s, reason in skipped:
        typer.echo(f"Skipped rubric site {s!r}: {reason}", err=True)

    if regressed:
        typer.echo("REGRESSED:")
        for s, delta in regressed:
            typer.echo(f"  {s}: {delta:+.2%}")
        raise typer.Exit(code=1)

    typer.echo("No regressions detected.")
