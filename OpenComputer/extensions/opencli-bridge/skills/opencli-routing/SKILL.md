---
name: opencli-routing
description: When to use OpenCLI vs browser-harness vs raw CDP. Encodes the "tools as enablers, agent picks the cheapest" decision tree.
---

# OpenCLI routing

You have three browser-related tool families available. **Pick by cost, not capability.**

## Cheapest first — always start here

**`OpenCliList`** → discover what's already deterministic. 100+ sites are pre-built (HN, Reddit, X/Twitter, Wikipedia, PyPI, Steam, Bilibili, Xiaohongshu, GitHub via `gh`, Cursor, Notion, ChatGPT, Antigravity, …). Free recon — call it before any web task.

If your target site is in the list:

**`OpenCliRun`** → run the deterministic adapter.
- Returns clean JSON
- **Zero LLM tokens at runtime** — the adapter has site-specific logic baked in
- Same command → same output schema, every time
- Use this even for trivial tasks: `OpenCliRun({site: "hackernews", command: "top", args: {limit: 5}})` is always strictly better than browsing HN's HTML.

## When no adapter exists yet — the authoring path

**`OpenCliBrowse`** → live browser via the chrome.debugger extension. Use ONLY for sites without an adapter, OR for one-off queries you'll never repeat.

After 1–2 successful browse sessions on a domain, you SHOULD call **`OpenCliAuthor`** to crystallize the pattern into a reusable adapter. Decision tree:

✅ AUTHOR a new adapter if any of:
- The site has structured data (lists, feeds, search results, scores, prices)
- The user said "every", "always", "daily", "check", "monitor", "remind"
- You've browsed this domain before in any prior session
- You needed >2 browser actions to reach the data
- Public site, no captcha wall

❌ DON'T author if:
- One-off question ("what does the about page say")
- Random unique URL (specific blog post, ephemeral content)
- Login-walled with captcha and the user hasn't expressed recurring intent

**DEFAULT: when in doubt, AUTHOR.** Cost is paid once. Not authoring means LLM tokens forever.

## When OpenCLI doesn't fit at all

**`BrowserNavigate / BrowserSnapshot / BrowserClick / BrowserType / BrowserVision`** (browser-harness) → low-level Chromium control via agent-browser CLI. Use only when:
- Running on a VPS where the OpenCLI extension isn't loaded
- Doing raw exploration with no specific schema in mind
- Working on a site OpenCLI's extension doesn't reach (chrome:// URLs, file:// URLs, sandboxed iframes the extension can't attach)

## The mental model

```
OpenCliList                ← always first (free)
       │
       ├── site exists ──→ OpenCliRun (zero tokens)
       │
       └── site missing ──→ OpenCliBrowse → OpenCliAuthor → next time it's free
                                ↓
                        (fallback: BrowserNavigate via browser-harness)
```

## Failure handling

When `OpenCliRun` returns `adapter_not_found`: do NOT just fall back to live browsing without crystallizing. Use the authoring path. The hint is in the error response.

When `OpenCliRun` succeeds but data looks wrong (site changed): use the OpenCLI autofix flow — call `OpenCliInspect` to see the adapter source, then OpenCliBrowse to see what changed, then OpenCliAuthor to update.

## Storage

All authored adapters live at `<oc_profile_home>/opencli/clis/<site>/<command>.js` (per-OC-profile, isolated via HOME-shim). They survive `npm update @jackwener/opencli` because user-authored state takes precedence over built-ins.
