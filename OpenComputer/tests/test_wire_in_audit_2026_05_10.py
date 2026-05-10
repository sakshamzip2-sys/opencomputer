"""Wire-in audit — fails when production code ships without callsites.

Background
----------

OpenComputer has shipped features multiple times where a module is
fully implemented + unit-tested but no production code ever calls it.
The most recent example is :mod:`opencomputer.agent.title_generator`
(Tier S Hermes port, 2026-04-27): function ``maybe_auto_title`` was
defined and unit-tested, but never imported from the agent loop —
so every session showed "(untitled · ID)" in the resume picker for
two weeks. PR #575 (2026-05-10) finally wired it in.

Earlier examples in the codebase memory: the auto-skill-evolution
SessionDB adapter (PR #204), the Active Memory pre-loop injection
(still off-by-default), checkpoint_manager wire-in failures
silenced by ``_log.debug``.

The pattern is the same every time:

1. Module shipped with full unit test coverage in isolation.
2. No grep-able caller in production code.
3. CI green.
4. User reports "feature X doesn't seem to do anything".

This test catches the pattern automatically. For each curated list
of "must-be-wired" symbols below, it grep-scans production code
(non-test, non-self) for at least one import or call. Failure prints
a precise diagnostic.

Curation policy
---------------

This test is intentionally narrow. It enforces wire-in for symbols
that:

* are *features* (not internal helpers),
* require integration into a long-running loop / daemon / CLI to
  reach the user,
* have failed this gate at least once in production.

Adding a new symbol to ``MUST_BE_WIRED`` is a deliberate decision —
typically when porting a new module from a reference repo, or when
debugging finds an unwired feature. Don't add internal helpers;
they get tested via their consumers.

The check is purely textual (``grep``-style) — fast, deterministic,
no imports of the production modules. Whitelisting via the
``allowed_callers`` field lets us point at module-paths that legitimately
own the call site (so editing the wire-in module isn't a wire-out).

False positives
---------------

If you see this test fail with a symbol you *just* added a caller for:

1. Verify the caller is OUTSIDE ``tests/`` and outside the symbol's
   defining module.
2. Verify the caller is in ``opencomputer/`` or ``extensions/`` (the
   production tree). Subdirectories of ``docs/`` or ``scripts/`` don't
   count — they don't run during normal usage.
3. If still failing: the caller is real but uses an alias/re-export.
   Add the alias to ``MUST_BE_WIRED[symbol].extra_search_terms``.

If you see this test fail with a symbol you removed callers from
intentionally (e.g., feature kill): remove the entry from
``MUST_BE_WIRED`` in the same commit. The audit list is part of the
contract; it deserves the same care as the wire-in itself.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class WireInRequirement:
    """One curated symbol whose production wire-in is contractually required.

    ``defining_module`` is the file path (relative to repo root) where
    the symbol is defined; that file is excluded from the caller scan
    (otherwise the definition itself counts as a caller).

    ``extra_search_terms`` adds aliases or shim names that also count
    as wire-in evidence. Useful when callers import via re-export.
    """

    symbol: str
    defining_module: str
    rationale: str
    extra_search_terms: tuple[str, ...] = field(default_factory=tuple)


MUST_BE_WIRED: tuple[WireInRequirement, ...] = (
    WireInRequirement(
        symbol="maybe_auto_title",
        defining_module="opencomputer/agent/title_generator.py",
        rationale=(
            "Hermes-ported auto-titler. Without a caller in agent/loop.py "
            "every session shows '(untitled · ID)' in the resume picker. "
            "Was unwired between 2026-04-27 (port) and 2026-05-10 (PR #575)."
        ),
    ),
    WireInRequirement(
        symbol="CheckpointManager",
        defining_module="opencomputer/agent/checkpoint_manager.py",
        rationale=(
            "v1.1 plan-2 M5.2 per-prompt message-history snapshots. "
            "Without caller in agent/loop.py, prompt_checkpoints stays "
            "empty and oc session rewind has nothing to restore."
        ),
    ),
    WireInRequirement(
        symbol="run_dreaming_v2_tick",
        defining_module="opencomputer/cron/dreaming_v2_tick.py",
        rationale=(
            "v1.1 plan-3 M6.4 dreaming-v2 consolidation. Cron tick must "
            "import this; otherwise dreaming_v2_enabled=True is a lie."
        ),
        extra_search_terms=("dreaming_v2_tick",),
    ),
    WireInRequirement(
        symbol="run_system_tick",
        defining_module="opencomputer/cron/system_jobs.py",
        rationale=(
            "Cron daemon entry — drives Dreaming v2, plugin demand decay, "
            "policy engine ticks. Without a caller the cron daemon is "
            "a no-op."
        ),
    ),
)


def _ripgrep_count(term: str, exclude_path_globs: tuple[str, ...]) -> int:
    """Count files containing *term* under repo root, excluding paths matching globs.

    Uses ``git grep`` so we honor .gitignore (skips .venv, dist, etc.)
    and work fast over the tracked tree.

    Falls back to ``grep -r`` if git isn't available (CI snapshots).
    Either way returns the number of distinct files matching.
    """
    exclude_args: list[str] = []
    for g in exclude_path_globs:
        exclude_args += [":(exclude)" + g]

    try:
        # git grep -l: list filenames once
        result = subprocess.run(
            ["git", "grep", "-l", term, "--", "*.py", *exclude_args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode in (0, 1):  # 1 = no matches; not an error
            return len([line for line in result.stdout.splitlines() if line.strip()])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback to plain grep (slower; honors no .gitignore but the
    # exclude_path_globs catch the noisy dirs).
    cmd = ["grep", "-rl", "--include=*.py"]
    for g in exclude_path_globs:
        cmd += ["--exclude-dir", g.rstrip("/").lstrip("/")]
    cmd.append(term)
    cmd.append(str(REPO_ROOT))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
    return len([line for line in result.stdout.splitlines() if line.strip()])


@pytest.mark.parametrize(
    "req",
    MUST_BE_WIRED,
    ids=[req.symbol for req in MUST_BE_WIRED],
)
def test_symbol_has_production_caller(req: WireInRequirement) -> None:
    """Each curated symbol must have ≥1 caller outside tests + its defining module.

    Production tree = ``opencomputer/`` + ``extensions/`` (excluding the
    symbol's own defining module). Callers in ``tests/``, ``docs/``,
    ``scripts/``, and ``audit/`` don't count toward production wire-in.
    """
    exclude_globs = (
        "tests/*",
        "docs/*",
        "scripts/*",
        "audit/*",
        "experiments/*",
        "evals/*",
        ".claude/*",
        req.defining_module,
    )

    matched_files: set[str] = set()
    search_terms = (req.symbol, *req.extra_search_terms)

    for term in search_terms:
        # `git grep -l` returns one filename per match. We collect the
        # union across all search terms.
        try:
            result = subprocess.run(
                [
                    "git",
                    "grep",
                    "-l",
                    term,
                    "--",
                    "opencomputer/*.py",
                    "opencomputer/**/*.py",
                    "extensions/**/*.py",
                    *[":(exclude)" + g for g in exclude_globs],
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if result.returncode in (0, 1):
                for line in result.stdout.splitlines():
                    if line.strip():
                        matched_files.add(line.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pytest.skip("git grep not available")

    if not matched_files:
        pytest.fail(
            f"WIRE-IN AUDIT FAILED for {req.symbol!r}.\n"
            f"  Defined in:    {req.defining_module}\n"
            f"  Searched for:  {list(search_terms)}\n"
            f"  In production: opencomputer/ + extensions/ (excluding the defining module)\n"
            f"  Why this matters:\n"
            f"    {req.rationale}\n\n"
            f"  How to fix: import and call {req.symbol} from a production module "
            f"(typically the agent loop, a cron tick, a CLI command, or a hook). "
            f"Then re-run this test."
        )

    # Sanity: also assert it's an IMPORT call site (not just a docstring
    # mention). Otherwise a stale comment could satisfy the audit.
    has_real_import = False
    for fname in matched_files:
        try:
            with open(REPO_ROOT / fname, encoding="utf-8") as fh:
                src = fh.read()
        except OSError:
            continue
        # An import OR a function-call usage. We avoid pinning to one
        # exact pattern because callers may import via __init__.py, may
        # import the parent module, or may call via attribute access.
        for term in search_terms:
            if (
                f"import {term}" in src
                or f", {term}" in src  # multi-import line
                or f"{term}(" in src  # call site
                or f"{term}." in src  # attribute access
            ):
                has_real_import = True
                break
        if has_real_import:
            break

    assert has_real_import, (
        f"WIRE-IN AUDIT: {req.symbol!r} appears in {sorted(matched_files)} "
        f"but ONLY in comments/docstrings — no actual import or call. "
        f"A documentation reference is not a wire-in.\n  Why this matters:\n    "
        f"{req.rationale}"
    )


def test_audit_list_symbols_actually_exist() -> None:
    """Sanity: each entry in ``MUST_BE_WIRED`` points at a real file + symbol.

    Catches typos / renames in the audit list itself. If a curated
    symbol no longer exists (renamed, removed), this test fails before
    the per-symbol audit so the diagnostic is unambiguous.
    """
    for req in MUST_BE_WIRED:
        path = REPO_ROOT / req.defining_module
        assert path.exists(), (
            f"WIRE-IN AUDIT META: {req.defining_module} (for {req.symbol}) "
            f"does not exist. Update or remove the entry."
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        assert req.symbol in src, (
            f"WIRE-IN AUDIT META: {req.symbol!r} not found in "
            f"{req.defining_module}. Renamed? Update the entry."
        )
