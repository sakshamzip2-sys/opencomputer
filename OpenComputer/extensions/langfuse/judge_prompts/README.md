# OC × Langfuse — Judge Prompt Templates

Four production-ready LLM-as-a-judge prompts for scoring OC traces in Langfuse.
All decisions, rationale, and limitations live in the design spec at
`OpenComputer/docs/superpowers/specs/2026-05-06-llm-judge-prompts-design.md`.
This README is the install runbook only.

## What's in here

| File | Score axis | Direction |
|---|---|---|
| `oc_response_quality_v1.txt` | Overall (4 dims rolled into 1) | Higher = better |
| `oc_hallucination_v1.txt` | Unsupported claims | **Higher = WORSE** (inverted) |
| `oc_tool_selection_quality_v1.txt` | Tool choice + args | Higher = better |
| `oc_companion_voice_v1.txt` | Persona voice adherence | Higher = better |

## Install (5 minutes)

1. Open Langfuse → **LLM-as-a-Judge** (`localhost:3000/project/<id>/evals`).
2. For each `.txt` file:
   - Click **Create Evaluator** → **Create new template**.
   - **Template name:** matches the filename without `.txt` (e.g. `oc_response_quality_v1`).
   - **Prompt body:** `cat <file>.txt | pbcopy` then paste.
   - **Provider:** `openai` (or `anthropic` if no OpenAI key — see spec §4 fallback).
   - **Model:** `gpt-4o-mini` (or `claude-haiku-4-5-20251001`).
   - **Temperature:** `0.1` · **Top P:** `1` · **Output token limit:** `512`.
   - **Score schema:** `{score: number 0-1, reasoning: string}` (see spec §4).
3. After all four templates exist, attach each as an active **Evaluator**:
   - **Filter:** `metadata.site = "agent_loop"` (skip eval-grader self-loop).
   - **Additional filter for `oc_tool_selection_quality_v1`:** `output contains "tool_use"`.
   - **Sampling:** start at 10% (raise after a week of clean signal).

## Verify

After install, run `oc` once with a real chat turn. Within ~10 minutes:

- Open Langfuse → **Tracing** → pick the latest `oc-agent_loop` trace.
- Confirm all four scores appear.
- Confirm `oc_response_quality_v1` is in [0,1] with non-empty reasoning.
- Confirm `oc_tool_selection_quality_v1` is absent (filter excludes) on
  text-only turns and present on tool-using turns.

If any score is consistently 0.0 with reasoning "input/output empty", you're
hitting the OpenAI-provider preview gap — see spec §13.1 + §17.

## Self-test calibration

Each prompt has a self-test pair in spec §16. Paste these into Langfuse →
LLM-as-a-Judge → **Test** before promoting to live evaluators. If a score
falls outside its expected band by >0.20, the rubric is mis-calibrated —
log a follow-up before promoting.

## Rollback

Disable evaluators in Langfuse UI. No code change to OC; existing traces
keep their historical scores.
