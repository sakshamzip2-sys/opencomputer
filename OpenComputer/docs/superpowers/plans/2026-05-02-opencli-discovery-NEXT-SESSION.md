# OpenCLI Discovery — NEXT SESSION

This is a placeholder plan for the AI-driven recipe synthesis sub-project
(originally Phase 5 of the OpenCLI integration spec at
`OpenComputer/docs/superpowers/specs/2026-05-02-opencli-integration-design.md`).

## Why deferred

- Network capture via Playwright `route()` interception needs careful design
  for auth-token redaction
- LLM-driven recipe synthesis needs prompt engineering + iteration
- `cascade` auth detection is a multi-strategy probe with state
- Pipeline grammar may need extending first (see
  `notes/2026-05-02-opencli-recipe-grammar-notes.md`)

## Scope when picked up

Four subcommands under `oc browser`:

| Command | Behaviour |
|---|---|
| `oc browser explore <url> --site <name>` | Agent navigates the site with network capture on; writes `.opencli/explore/<site>/manifest.json`, `endpoints.json`, `capabilities.json`, `auth.json` |
| `oc browser cascade <api-url>` | Probes URL with PUBLIC → COOKIE → HEADER strategies; remembers what works |
| `oc browser synthesize <site>` | Reads explore artifacts; LLM writes YAML recipe at `~/.opencomputer/<profile>/recipes/<site>.yaml` |
| `oc browser generate <url> --goal <goal>` | One-shot: explore + synthesize + register |

## Pre-requisites

Before starting:
- v1 (Phases 1-4 of opencli-integration plan) merged ✓ when the parent PR lands
- Pipeline grammar extensions needed first:
  - `select` step kind for JSON-path traversal (so reddit-shape recipes work)
  - HTML / Playwright fetcher path (so HTML-scrape recipes work)
- `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in env (LLM-driven synthesis)

## Estimated scope

2-3 weeks of careful work. Each subcommand is its own bite-sized plan.

Recommended ordering:
1. Pipeline grammar extensions (unblocks all of these)
2. `cascade` (smallest, demonstrates auth-strategy state)
3. `explore` (medium, network capture + redaction)
4. `synthesize` (LLM prompt iteration)
5. `generate` (composition of the above)

## Risks

1. **Auth-token leakage**: live network capture will see `Authorization`,
   `Cookie`, `X-API-Key` headers. Must redact before writing artifact files.
   Add a redaction pass + warn the user explicitly that artifact files MAY
   contain non-secret request structure.

2. **LLM-generated recipe correctness**: write to `*.candidates.yaml` first;
   user reviews before renaming to activate.

3. **Cost runaway**: rate-limit `generate` to one site/day by default.
   Surface cost in `oc insights llm`.
