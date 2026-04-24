# OpenComputer F6 — OpenCLI Scraper Plugin (User Guide)

> **Status: C1 (deep-scan + design only).** The `extensions/opencli-scraper/` plugin is **not yet on disk** — this is the scope/safety doc. C2 ships the plugin skeleton; Session A's Phase 4 wires consent + signal-normalizer; only after that is the plugin user-facing.

## What is this?

OpenCLI Scraper wraps [OpenCLI](https://github.com/jackwener/opencli) (Apache-2.0) so OpenComputer can **fetch public web data on your behalf** — your GitHub profile, your Reddit comments, an arXiv paper, a LinkedIn timeline you have access to, etc. — without you having to remember the right URL or click through a UI.

15 platforms supported in MVP (the curated shortlist):

- **Public, no login**: GitHub, Reddit (user/posts/comments), HackerNews, StackOverflow, YouTube, Medium, Bluesky, arXiv, Wikipedia, ProductHunt
- **Cookie-required (uses your existing browser session)**: LinkedIn (timeline), Twitter/X (profile + tweets)

## Safety guarantees

These are the hard rules — verifiable in code (paths in parens):

1. **Disabled by default.** Plugin manifest sets `enabled_by_default: false`. You explicitly enable it via `opencomputer plugin enable opencli-scraper`. (`extensions/opencli-scraper/plugin.json`)
2. **Strategy-aware consent.** Each adapter has a "strategy": `PUBLIC` (anonymous fetch), `LOCAL` (filesystem), `COOKIE` (uses browser cookies), `HEADER` / `INTERCEPT` / `UI`. The consent prompt names the strategy explicitly so you know what's at stake. A `COOKIE`-strategy scrape gets a stricter prompt than `PUBLIC`.
3. **Per-domain rate limits.** Conservative defaults (GitHub 60/hr, Reddit 60/min, LinkedIn 30/min). Tunable.
4. **Robots.txt enforced.** We layer robots.txt parsing on top of OpenCLI. Disallowed paths refused even if OpenCLI would happily fetch them.
5. **Field whitelist per adapter.** OpenCLI returns rich data; the plugin filters to only fields the agent should see. Reduces PII surface, saves tokens. Unknown adapters return empty until explicitly whitelisted.
6. **Subprocess isolation.** OpenCLI runs in an isolated Chrome window (not your main browser). Crashes don't propagate.
7. **Concurrent scrape cap** (8 by default). Prevents the agent from accidentally hammering sites.
8. **No auto-install.** If `opencli` (Node.js binary) or Chrome aren't installed, the plugin refuses with a clear error — never silent installs.
9. **No telemetry.** OpenCLI itself emits none (per its `PRIVACY.md`); our wrapper adds none.

## What you can do today (C1)

**Nothing user-facing yet.** Architecture: `docs/f6/design.md`. Upstream deep-scan: `docs/f6/opencli-source-map.md`.

## Phase status

| Phase | Status | What ships |
|---|---|---|
| **C1** | ✅ Landed (this branch) | Deep-scan + design doc + this README |
| **C2** | Coming | Plugin skeleton: wrapper, 3 tools, rate limiter, robots cache, 15 adapter whitelists, subprocess bootstrap, ~50-60 tests. **Tools NOT registered yet** (waits for Session A wiring). |
| **C4** | After C2 | 5 use-case libraries: research-automation (arXiv/Scholar/PubMed citation graph), content-monitoring, context-enrichment, competitor-research, market-signals (with separate consent tier). |
| **Session A's Phase 4** | Outside Session C scope | Wire ConsentGate per-strategy + signal-normalizer publish + flip `enabled_by_default` to `true` (only after legal review). |

## Setup (post-Phase-4)

```bash
brew install node
npm install -g @jackwener/opencli
brew install --cask google-chrome   # if not already
opencomputer plugin enable opencli-scraper
opencomputer plugins | grep opencli-scraper
```

First use prompts for per-strategy consent grants.

## FAQ

**Will the agent scrape my private LinkedIn data without asking?** No — first use of any `COOKIE`-strategy adapter prompts you, naming the strategy. Allow once, allow with TTL, or deny.

**Can I add adapters beyond the 15?** Post-MVP. We'll ship an `add_adapter(slug, whitelist)` extension hook so you don't need to fork. Until then, the 15-adapter list is hardcoded in `field_whitelist.py`.

**What if OpenCLI scrapes data with PII?** Two layers — (a) per-adapter field whitelist drops fields known to contain PII, (b) C2.5 follow-up adds a per-FIELD redactor that masks emails, phone numbers, SSNs even inside whitelisted fields.

**Rate-limit behavior?** Token-bucket blocks until a token is free. Latency builds up if the agent fires faster than the limit. Upstream errors (e.g. GitHub 429) surface clearly — no blind retry.

**Does this plugin send my data anywhere?** No. OpenCLI runs entirely on your machine. Data flows: target site → OpenCLI subprocess → wrapper → agent context. Nothing leaves your machine unless your provider plugin (Anthropic / OpenAI) sends it as part of an LLM call.

**How do I wipe everything?** `opencomputer plugin disable opencli-scraper` to turn off. `opencomputer plugin reset opencli-scraper` (post-Phase-4 CLI) for cached robots.txt + scrape cache.

---

*Last updated: C1 landing. Updated each phase.*
