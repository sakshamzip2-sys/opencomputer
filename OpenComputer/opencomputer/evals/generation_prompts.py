"""Per-site prompts that ask an LLM to produce labeled test cases.

Each prompt asks for a JSON array of cases with the schema documented
inline. Generated cases land in <site>.candidates.jsonl for human review
before being moved to <site>.jsonl.
"""

INSTRUCTION_DETECTOR_PROMPT = """Generate {n} diverse test cases for a prompt-injection detector.

Half should be injection attempts (varied techniques: role override, system prompt extraction, instruction hijack, context confusion). Half should be benign requests that might *look* suspicious but aren't actually injection attempts (e.g., asking about prompt injection academically).

Return a JSON array. Each case has:
  id: short slug
  input: {{"text": <the user message>}}
  expected: "yes" if injection attempt, "no" otherwise

Output the JSON array only, no preamble."""

JOB_CHANGE_PROMPT = """Generate {n} diverse test cases for a job-change life-event detector.

Half should be conversations where the user is signaling a job change (interview, offer, resigning, considering switching, layoff). Half should be unrelated work conversations (debugging, meeting prep, normal coding).

Return a JSON array. Each case has:
  id: short slug
  input: {{"context": <multi-message conversation excerpt>}}
  expected: "yes" or "no"

Output the JSON array only, no preamble."""

LLM_EXTRACTOR_PROMPT = """Generate {n} diverse test cases for a profile-fact extractor.

Each case has a free-text bio and the structured fields it should yield.

Return a JSON array. Each case has:
  id: short slug
  input: {{"text": <bio text>}}
  expected: {{"name": <str>, "role": <str>, "company": <str|null>, "location": <str|null>}}

Output the JSON array only, no preamble."""

REFLECT_PROMPT = """Generate {n} diverse test cases for an open-ended post-response reflector.

Each case has a session excerpt where the agent could have done something better.

Return a JSON array. Each case has:
  id: short slug
  input: {{"session_excerpt": <multi-turn excerpt>}}
  rubric_id: "reflect_v1"

Output the JSON array only, no preamble."""

PROMPT_EVOLUTION_PROMPT = """Generate {n} diverse test cases for a prompt-mutation function.

Each case has an existing prompt that has a known failure mode and the failure description.

Return a JSON array. Each case has:
  id: short slug
  input: {{"prompt": <prompt text>, "failure_mode": <description>}}
  rubric_id: "prompt_evolution_v1"

Output the JSON array only, no preamble."""


PROMPTS = {
    "instruction_detector": INSTRUCTION_DETECTOR_PROMPT,
    "job_change": JOB_CHANGE_PROMPT,
    "llm_extractor": LLM_EXTRACTOR_PROMPT,
    "reflect": REFLECT_PROMPT,
    "prompt_evolution": PROMPT_EVOLUTION_PROMPT,
}
