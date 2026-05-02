"""LLM-driven test-case generation.

generate_cases() calls an LLM (a different model than the site under test
to avoid same-model correlation in case design) and writes candidates to
<site>.candidates.jsonl. User reviews via PR or local edit, then moves
approved cases to <site>.jsonl manually.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from opencomputer.evals.generation_prompts import PROMPTS


class _GeneratorProvider(Protocol):
    def complete(self, prompt: str) -> Any: ...


def generate_cases(
    *,
    site_name: str,
    n: int,
    cases_dir: Path,
    generator_provider: _GeneratorProvider,
) -> Path:
    """Generate n candidate cases for site_name. Append to candidates.jsonl."""
    if site_name not in PROMPTS:
        raise KeyError(f"no generation prompt for site {site_name!r}")

    prompt = PROMPTS[site_name].format(n=n)
    response = generator_provider.complete(prompt)
    text = getattr(response, "text", str(response))

    cases = json.loads(text)
    if not isinstance(cases, list):
        raise ValueError(f"generator returned non-list for site {site_name!r}")

    cases_dir.mkdir(parents=True, exist_ok=True)
    out_path = cases_dir / f"{site_name}.candidates.jsonl"

    with out_path.open("a") as f:
        for case in cases:
            f.write(json.dumps(case) + "\n")

    return out_path
