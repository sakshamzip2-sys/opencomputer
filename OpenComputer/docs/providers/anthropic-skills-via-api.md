# Anthropic Skills-via-API (opt-in)

OpenComputer supports invoking Anthropic's pre-built skills (`pdf`,
`pptx`, `xlsx`, `docx`) running in Anthropic's code-execution container.
This lets you generate documents (PowerPoints, spreadsheets, Word files,
PDFs) without bundling local Python dependencies like python-pptx /
openpyxl / python-docx.

**This is OFF by default** because it adds a cloud round-trip and
server-side execution cost. Enable per call when you actually need it.

## Enable

### Environment variable (recommended today)

```bash
export OPENCOMPUTER_ANTHROPIC_SKILLS=pdf,pptx,xlsx,docx
opencomputer chat
```

Comma-separated list of Anthropic-managed skill IDs. End-to-end working
path today.

### Programmatic enable (current limitation)

The design intent is for callers to set
`runtime.custom["anthropic_skills"] = ["pdf", "pptx"]` and have the
provider pick it up automatically. **As of this PR (SP4), the
`runtime_extras` translator (`opencomputer/agent/runtime_flags.py::runtime_flags_from_custom`)
only forwards `reasoning_effort` and `service_tier`.** The
`anthropic_skills` key in `runtime.custom` is therefore not yet plumbed
through the agent loop into the provider's `runtime_extras` dict.

A one-line follow-up to `runtime_flags_from_custom` (forward the
`anthropic_skills` key into the returned dict, and update the existing
`test_runtime_flags_*` shape assertions to expect the new key) will
close this gap and make the programmatic path work end-to-end without
touching the provider. That's intentionally a separate PR so the
provider-side helpers and the loop-side flag forwarding can be reviewed
independently.

Until then, use the env var. If you must enable programmatically today,
you can call the provider directly with a `runtime_extras` dict:

```python
from extensions.anthropic_provider.provider import AnthropicProvider

provider = AnthropicProvider(model="claude-opus-4-7")
await provider.complete(
    messages=[...],
    runtime_extras={"anthropic_skills": ["pdf", "pptx"]},
)
```

## What gets injected per request

When `anthropic_skills` resolves to a non-empty list, the Anthropic
provider auto-adds:

1. **Beta headers**: `code-execution-2025-08-25`, `skills-2025-10-02`,
   `files-api-2025-04-14`.
2. **`container.skills`**: array listing each enabled skill with
   `type=anthropic, version=latest`.
3. **`code_execution_20250825` tool**: required for skills to actually
   run; auto-appended to your tools list (no duplicates).

Empty or unset → today's behavior (no kwargs change).

## Available Anthropic-managed skills

| Skill ID | What it does |
|---|---|
| `pdf` | Generate or modify PDF files (forms, reports). |
| `pptx` | Create or edit PowerPoint presentations. |
| `xlsx` | Create or edit Excel spreadsheets, charts, pivot tables. |
| `docx` | Create or edit Word documents. |

(Per Anthropic's Skills-via-API guide. List may grow.)

## Trade-offs

| Aspect | Detail |
|---|---|
| **Latency** | Each call now routes through Anthropic's container before responding. Adds ~1-3s per turn even for simple text replies. |
| **Cost** | Server-side execution is metered. Generating a 5-slide PowerPoint can be substantially more expensive than a plain text reply. |
| **ZDR** | Skills-via-API is **NOT** ZDR-eligible. Files written by skills are retained per Anthropic's standard policy. |
| **Provider lock-in** | Anthropic-only. Bedrock / OpenAI / others ignore the flag. |

## When to use

- User asks "create a PowerPoint summarizing this conversation"
- User asks "build a spreadsheet of my expenses from this CSV"
- User asks "fill out this PDF form"

## When NOT to use

- Any task you can do with OC's local tools (Bash, Read/Write/Edit,
  WebSearch). Local is faster, free, and ZDR-eligible.
- Multi-turn coding sessions. The skills container's compute cost
  dwarfs Bash's.
- Default-on. The user's local-execution agent should stay local
  unless they opt into the cloud capability.

## Implementation references

- Spec: `OpenComputer/docs/superpowers/specs/2026-05-02-sp4-skills-via-api-design.md`
- Plan: `OpenComputer/docs/superpowers/plans/2026-05-02-sp4-skills-via-api.md`
- Helpers: `extensions/anthropic-provider/provider.py::_resolve_anthropic_skills`,
  `_build_skills_container`, `_augment_kwargs_for_skills`
- Loop-side `runtime_extras` translator (the forwarding gap above):
  `opencomputer/agent/runtime_flags.py::runtime_flags_from_custom`
- Anthropic Skills-via-API guide: https://docs.claude.com/en/build-with-claude/skills-guide
- Code execution tool docs: https://docs.claude.com/en/agents-and-tools/tool-use/code-execution-tool
