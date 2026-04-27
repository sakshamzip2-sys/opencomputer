# Browser Control — Design

**Date:** 2026-04-28
**Status:** Design
**Branch:** `feat/browser-control`
**Worktree:** `/tmp/oc-browser/`

---

## 1. Goal

Ship browser AUTOMATION (control) for OC — closes the gap from `browser-bridge` plugin which is observation-only (Chrome extension POSTs page-visit events). Default OFF; opt-in. Cross-platform via Playwright (already cross-platform via PyPI wheels).

## 2. Why Playwright not browser-use

- **browser-use** (Hermes's choice): wraps `agent-browser` + Browser Use cloud. Pivoted recently; integration is complex.
- **Playwright MCP**: already available as MCP server in OC's plugin list. Mature, official, cross-platform.
- **Playwright direct**: clean Python API; can run isolated browser sessions per task; deterministic for tests.

We use **Playwright direct** (not via MCP) so the tools live in OC's own surface and can be capability-claim-gated through F1.

## 3. Architecture

### 3.1 Module shape

```
extensions/browser-control/
├── plugin.json              (default OFF)
├── plugin.py
├── browser.py               (Playwright wrapper — lazy import)
├── tools.py                 (5 tools as BaseTool subclasses)
├── README.md
```

### 3.2 Tools (5)

All take `consent_tier=2`, `parallel_safe=True` (each tool gets a fresh browser session), `capability_claims` declared:

1. **`BrowserNavigate(url, *, isolated=True)`** — opens URL in fresh isolated context. Returns page accessibility-tree snapshot (text-based, model-friendly).
2. **`BrowserClick(selector_or_ref)`** — clicks element by CSS selector OR accessibility ref (e.g. `@e3`).
3. **`BrowserFill(selector_or_ref, text)`** — fills text input.
4. **`BrowserSnapshot()`** — returns current page accessibility tree without re-navigating.
5. **`BrowserScrape(url, *, css_selector=None)`** — fetch page; if css_selector given, return matched elements' text; else return full page text.

### 3.3 Session isolation

Each tool call uses a fresh `BrowserContext` by default. No shared cookies / login state. User can opt into shared profile via env var `OPENCOMPUTER_BROWSER_PROFILE_PATH=/path` for power users (advanced; documented as risky).

### 3.4 F1 capabilities

```python
"browser.navigate": ConsentTier.EXPLICIT,
"browser.click": ConsentTier.EXPLICIT,
"browser.fill": ConsentTier.EXPLICIT,
"browser.snapshot": ConsentTier.IMPLICIT,
"browser.scrape": ConsentTier.IMPLICIT,
```

Click/fill/navigate are EXPLICIT because they can submit forms / log in. Snapshot/scrape are read-only IMPLICIT.

### 3.5 Privacy contract

- **Isolated by default** — no user cookies/session shared
- **No persistent storage** — each tool call's browser context is destroyed after use (unless explicit shared-profile opt-in)
- **Screenshot retention** — `BrowserSnapshot` returns text accessibility tree, NOT pixels. Pixel screenshots would go through existing `screenshot` tool (separate, already gated).
- **AST no-egress test** — plugin source has zero direct HTTP-client imports (Playwright handles networking internally; we don't import httpx/requests).

## 4. Implementation phasing

5 sub-tasks, ~8h:

| # | Task | Effort |
|---|---|---|
| T1 | Plugin scaffold + Playwright wrapper (browser.py) | 2h |
| T2 | 5 BaseTool implementations + capability registration | 2.5h |
| T3 | Doctor preflight (Playwright + browser binary) + AST no-egress | 1h |
| T4 | README + tests (mocked Playwright + integration smoke) | 1.5h |
| T5 | CHANGELOG + CI matrix + push + PR | 1h |

## 5. Risks

| Risk | Mitigation |
|---|---|
| Playwright `chromium` binary not installed (~150MB) | Doctor preflights with `playwright install chromium`; user prompted |
| Playwright headless on Linux without display | Works via Xvfb; doctor warns if X missing |
| User accidentally opens shared profile and exposes login | Explicit env var override; documented as risky in README |
| Anti-bot detection (Cloudflare etc.) | Out of scope; Playwright is stealth-mode-capable but we don't enable by default |

## 6. Out of scope

- Visual element identification (image-based clicking) — text accessibility tree only
- Cross-tab orchestration — each tool call is one page
- Browser extensions injection — keep tools simple
- Auto-login flows — too sensitive

---

*Spec ready; plan + execution next.*
