# OpenCLI Integration — Design

**Date:** 2026-05-02
**Status:** Approved (verbal, after stress-test corrected α' from β)
**Branch:** `feat/opencli-integration`
**Author:** Claude (with Saksham's iterative guidance + audit ritual)

## Problem

Users want OpenCLI's value proposition: **sub-second, zero-token, deterministic, cron-friendly access to logged-in websites** as terminal commands (`oc browser bilibili hot --limit 10 -f json`). OpenComputer today has Playwright-based browser tools, but they're LLM-driven (5–15s, $0.05–$0.20/call, prose output) — wrong shape for repeated structured scrapes.

The integration must coexist with OpenComputer's existing browser tools (used by the chat agent for general browsing) without regressing them.

## Architecture choice: α' (recipes-first for CLI, LLM-first for agent)

Two surfaces, two philosophies, **clear seam**:

| Surface | Philosophy | Latency | Cost | Output |
|---|---|---|---|---|
| `oc browser <site> <verb>` | **Recipe-first** (YAML adapter looked up; LLM only on explicit `--llm-fallback`) | <1s typical | $0 | Deterministic |
| Existing agent loop browser tools (Navigate / Click / Snapshot / Scrape) | **LLM-first** (model decides actions) | 5-15s | tokens | Prose |
| `oc browser explore` / `synthesize` / `cascade` / `generate` | **Hybrid**: agent loop drives the browser, network capture writes YAML | LLM-bound | tokens | YAML recipe artifact |

Wrong-direction philosophy (β: LLM-first for everything) was rejected because it doesn't deliver OpenCLI's value proposition (cron-able, cheap, deterministic).

## Five sub-projects

The work decomposes into five independent units that ship sequentially. Each is independently shippable; user value compounds:

### Sub-project 1: CDP attach mode for `browser-control`

**What**: extension `extensions/browser-control/` learns a new `attach_cdp` mode. Set `OPENCOMPUTER_BROWSER_CDP_URL=http://localhost:9222` (or pass as a tool arg) and the existing Playwright tools connect to the user's already-running Chrome instead of launching a fresh ephemeral browser.

**Why this matters**: this single change unlocks the OpenCLI value (use my real session, my real cookies, my real logins) for *every* existing browser tool — the LLM agent gets it for free, the recipe layer (sub-project 2) gets it for free.

**User-side requirement**: launch Chrome with `--remote-debugging-port=9222`. Provide `oc browser chrome` helper that prints the right command for the user's OS (we don't auto-launch — that's risky).

**Files touched**:
- Modify: `extensions/browser-control/browser.py` — add `_get_browser_via_cdp(cdp_url)` path
- Modify: `extensions/browser-control/tools.py` — read `cdp_url` from env or tool arg
- Modify: `extensions/browser-control/README.md` — new "Attach to existing Chrome" section

**Tests**: mock `playwright.async_api.connect_over_cdp` and verify it's called with the right URL.

### Sub-project 2: YAML recipe loader + 3 starter adapters

**What**: new module `opencomputer/recipes/` that:
- Defines a YAML schema for recipes (mostly compatible with OpenCLI's, simplified)
- Loads recipes from `~/.opencomputer/<profile>/recipes/*.yaml` and from a bundled `extensions/browser-recipes/` directory
- Executes a recipe against a Playwright `Page` (uses sub-project 1's CDP attach when configured)

**Recipe schema** (simplified from OpenCLI's; v1 supports declarative pipelines, not TS adapters):

```yaml
name: hackernews
description: "Hacker News scrapers"
commands:
  top:
    description: "Top stories from HN"
    pipeline:
      - fetch: "https://hacker-news.firebaseio.com/v0/topstories.json"
      - take: "{{ limit | default(10) }}"
      - map:
          fetch: "https://hacker-news.firebaseio.com/v0/item/{{ item }}.json"
      - format:
          fields: [title, url, score, by]
    formats: [json, table, md]
```

**Three starter adapters** (chosen for safety + immediate user value, no auth required, public APIs):
1. `hackernews` — public Firebase API, simplest possible
2. `reddit` — public `.json` endpoints (no login needed for public subreddits)
3. `github_trending` — github.com/trending HTML scrape (demonstrates HTML+selectors path)

Avoid bundling adapters that require login (Bilibili, Twitter, Reddit-private) for v1 — those depend on sub-project 1 and present privacy/security review surface. Users add those as profile-local recipes.

**Files**:
- Create: `opencomputer/recipes/__init__.py` — public API: `load_recipe(site)`, `run_recipe(site, verb, args)`
- Create: `opencomputer/recipes/schema.py` — pydantic models for the YAML schema
- Create: `opencomputer/recipes/loader.py` — find recipes in profile-dir + bundled dir
- Create: `opencomputer/recipes/runner.py` — pipeline executor (fetch / map / take / format)
- Create: `opencomputer/recipes/formats.py` — JSON / table / markdown output formatters
- Create: `extensions/browser-recipes/recipes/hackernews.yaml`
- Create: `extensions/browser-recipes/recipes/reddit.yaml`
- Create: `extensions/browser-recipes/recipes/github_trending.yaml`

**Tests**: schema validation, loader precedence (profile-local overrides bundled), pipeline execution against mock Page, format output correctness.

### Sub-project 3: CLI dispatcher

**What**: new `oc browser` subcommand that dispatches to the recipe runner.

**CLI shape**:
```bash
oc browser <site> <verb> [--limit N] [--format json|table|md] [--llm-fallback]
oc browser list             # list installed recipes
oc browser show <site>      # show a recipe's commands
oc browser chrome           # print Chrome launch command for current OS
```

**Files**:
- Create: `opencomputer/cli_browser.py` — Typer app
- Modify: `opencomputer/cli.py` — register `app.add_typer(browser_app, name="browser")`

**Tests**: CliRunner verifies `list`, `show`, `chrome` commands work; `<site> <verb>` calls `run_recipe` with right args.

### Sub-project 4: `--llm-fallback` opt-in

**What**: when no recipe matches, default behaviour is helpful error:
```
$ oc browser foosite hot
No recipe for site 'foosite'. Options:
  - oc browser explore https://foosite.com   # LLM discovers + writes a recipe
  - oc browser foosite hot --llm-fallback     # one-off LLM scrape
  - Add a recipe to ~/.opencomputer/<profile>/recipes/foosite.yaml
```

With `--llm-fallback`: dispatch to a special agent-loop run with a constrained prompt: "Navigate to <url> derived from verb, scrape <inferred fields>, return as JSON." Reuses existing browser-control tools.

**Files**:
- Modify: `opencomputer/cli_browser.py` — add `--llm-fallback` flag
- Create: `opencomputer/recipes/llm_fallback.py` — builds the constrained prompt, runs the agent loop, parses output

**Tests**: missing recipe + no flag → exit 1 with helpful message; missing recipe + flag → calls LLM-fallback path.

### Sub-project 5: AI-driven recipe synthesis

**What**: four `oc browser` subcommands that use the agent loop to explore a new site and write a YAML recipe:

| Command | Behaviour |
|---|---|
| `oc browser explore <url> --site <name>` | Agent navigates the site with network capture on; writes `.opencli/explore/<site>/manifest.json`, `endpoints.json`, `capabilities.json`, `auth.json` |
| `oc browser cascade <api-url>` | Probes URL with PUBLIC → COOKIE → HEADER strategies; remembers what works in `auth.json` |
| `oc browser synthesize <site>` | Reads explore artifacts; LLM writes YAML recipe at `~/.opencomputer/<profile>/recipes/<site>.yaml` |
| `oc browser generate <url> --goal <goal>` | One-shot: explore + synthesize + register |

**Why this is genuinely speculative**: OpenCLI's discovery is the most under-tested part of their codebase. We should ship it as **labelled experimental** with explicit caveats:
- LLM-generated recipes need human review before use
- Network capture has privacy implications (token leakage in URLs/headers)
- Generation rate-limited to discourage runaway costs

**Files**:
- Create: `opencomputer/recipes/discovery/explorer.py`, `synthesizer.py`, `cascade.py`, `generator.py`
- Modify: `opencomputer/cli_browser.py` — add the four subcommands
- Create: `extensions/browser-control/network_capture.py` — Playwright `route()` interception → JSON log

**Tests**: mock browser session + mock LLM; verify artifact files are written with the right shape; YAML output validates against the schema from sub-project 2.

## Realistic ship plan

**v1 in this session (realistic)**: sub-projects 1, 2 (with 1 starter recipe), 3, partial 4. ~600-1000 LOC, possibly more depending on Playwright API specifics.

**v2 (separate session)**: sub-project 2's remaining 2 starter recipes + sub-project 5 (discovery layer). The discovery layer alone is several days of careful work; not honest to claim it ships in this session.

The phasing in the implementation plan reflects this — phase 1-3 are mandatory, phase 4 is optional in this session, phase 5 is documented as next-session.

## Out of scope

- Chrome extension (OpenCLI's "Bridge"). We don't need it because CDP attach mode covers the same use case with no extension to maintain. If users want browser-bridge-style awareness, the existing `browser-bridge` extension already does that.
- Multi-profile / multi-Chrome routing. v1 attaches to whatever Chrome is on `localhost:9222`. Future work could discover multiple debug-port Chromes.
- TypeScript adapters (OpenCLI's TS path for complex sites). v1 is YAML-only. Complex sites that don't fit YAML → user uses LLM-fallback OR contributes a new YAML pattern.
- Multi-tenant scraping service. This is a single-user personal tool — same as OpenCLI.
- Selenium support. Playwright only.

## Parallel-session coordination

| File | Touched | Contended? | Risk |
|---|---|---|---|
| `extensions/browser-control/browser.py` | YES | Need to check | TBD before commit |
| `extensions/browser-control/tools.py` | YES | Need to check | TBD before commit |
| `extensions/browser-control/network_capture.py` | NEW | N/A | None |
| `extensions/browser-recipes/` | NEW dir | N/A | None |
| `opencomputer/recipes/` | NEW dir | N/A | None |
| `opencomputer/cli_browser.py` | NEW | N/A | None |
| `opencomputer/cli.py` | YES (one-line `add_typer`) | Need to check | Low — additive |

## Success criteria

**Sub-project 1**: `OPENCOMPUTER_BROWSER_CDP_URL=http://localhost:9222 oc <prompt>` connects to user's Chrome instead of launching a fresh one. All 5 existing browser tools continue to work in both modes.

**Sub-project 2**: `oc browser hackernews top --limit 5 -f json` returns 5 stories as JSON in <1s with $0 LLM cost. Profile-local recipes override bundled recipes. 30+ tests cover schema, loader, runner, formats.

**Sub-project 3**: `oc browser list` lists 3 starter recipes. `oc browser show hackernews` prints the recipe's commands. `oc browser chrome` prints the right Chrome launch command for the user's OS.

**Sub-project 4**: Missing recipe + no flag → exit 1 + helpful message. Missing recipe + `--llm-fallback` → LLM scrapes (with tokens) and returns.

**Sub-project 5** (next session): `oc browser explore <url> --site foo` writes 4 artifact files. `oc browser synthesize foo` writes valid YAML to profile-local recipes dir. Generated recipe runs successfully via sub-project 2's runner.

## Risks and mitigations

1. **Playwright CDP attach quirks** — `connect_over_cdp` has known issues with detached/attached browser context lifecycle. Mitigation: separate browser context per request, never close the user's tabs.

2. **YAML pipeline expressiveness** — the `fetch / map / take / format` pipeline may not cover all sites. Mitigation: v1 tackles 3 simple sites; complex sites use `--llm-fallback`. v2 can extend the pipeline grammar based on real adoption.

3. **Auth token leakage in network capture** — sub-project 5's `explore` captures live network calls including auth headers. Mitigation: redact `Authorization`, `Cookie`, `X-API-Key` headers in artifact files; warn users explicitly.

4. **Recipe security** — a malicious YAML recipe could `fetch` arbitrary URLs from the user's authenticated browser. Mitigation: bundled recipes are reviewed; profile-local recipes are user-installed (their own risk); add a `--require-signed` flag stub for future signing.

5. **LLM-generated recipe correctness** — sub-project 5's auto-generated recipes will be wrong sometimes. Mitigation: write to a `*.candidates.yaml` extension first; user reviews + renames to `<site>.yaml` to activate.

## Followups

- TypeScript adapter support (OpenCLI's complex-site path)
- Recipe signing / trust levels
- Cron-friendly `oc browser cron <site> <verb>` runner that handles auth refresh
- Recipe sharing protocol (publish/install from a registry)
- Integration with `oc insights llm` to track cost savings vs LLM-fallback
