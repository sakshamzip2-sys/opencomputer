"""Central registry of evaluable LLM call sites.

To add a new site:
  1. Add an EvalSite entry below.
  2. If grader is "rubric", add a markdown rubric to evals/rubrics/<id>.md.
  3. Add cases to evals/cases/<name>.jsonl (or generate via 'oc eval generate <name>').

The callable_path's target function MUST accept a single dict argument
(the case input) and return the structured value to grade.
"""

from opencomputer.evals.types import EvalSite


SITES: dict[str, EvalSite] = {
    "reflect": EvalSite(
        name="reflect",
        callable_path="opencomputer.evals.adapters:adapter_reflect",
        grader="rubric",
        rubric_id="reflect_v1",
    ),
    "prompt_evolution": EvalSite(
        name="prompt_evolution",
        callable_path="opencomputer.evals.adapters:adapter_prompt_evolution",
        grader="rubric",
        rubric_id="prompt_evolution_v1",
    ),
    "llm_extractor": EvalSite(
        name="llm_extractor",
        callable_path="opencomputer.evals.adapters:adapter_llm_extractor",
        grader="schema",
    ),
    "job_change": EvalSite(
        name="job_change",
        callable_path="opencomputer.evals.adapters:adapter_job_change",
        grader="exact",
    ),
    "instruction_detector": EvalSite(
        name="instruction_detector",
        callable_path="opencomputer.evals.adapters:adapter_instruction_detector",
        grader="exact",
        requires_provider=False,  # regex-based
    ),
}


def get_site(name: str) -> EvalSite:
    if name not in SITES:
        raise KeyError(f"unknown eval site: {name!r}. Known: {list(SITES)}")
    return SITES[name]
