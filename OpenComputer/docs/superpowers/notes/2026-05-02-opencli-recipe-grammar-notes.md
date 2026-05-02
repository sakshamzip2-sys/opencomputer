# Recipe Pipeline Grammar — v1 Limitations

While authoring starter recipes I found that some sites need pipeline
grammar features beyond what v1 ships. v1's grammar is intentionally
minimal-and-extensible (fetch / take / map / filter / format / eval) —
not a permanent limit, just a deliberate floor.

## Reddit — deferred

Reddit's public JSON endpoints (`reddit.com/r/<sub>/hot.json`) need
JSON-path traversal: `data.children[*].data.<field>`. v1's grammar
handles flat lists and per-item map-fetch but doesn't natively support
"extract this nested list before continuing."

Workarounds that DON'T work in v1:
1. `eval` returns strings (jinja2 native), so
   `eval: "{{ data.children }}"` produces a stringified list, breaking
   subsequent `take`/`map`/`format`.
2. `map` only supports inner `fetch`, not inner `eval` or `select`.

v2 grammar additions to consider:
- `select` step: JSON path extraction
  (e.g. `select: "data.children[*].data"`)
- `eval` returning native Python objects (extend the runner to detect
  jinja2 expressions that evaluate to non-strings)

## github_trending — also deferred

HTML-scraping recipes need a Playwright-aware fetcher that runs through
the user's CDP-attached Chrome and extracts via CSS selectors. v1's
httpx-based fetcher returns raw HTML strings; the pipeline can't
traverse them without a `selector` step kind.

v2 additions:
- `selector` step kind:
  `selector: 'article.Box-row a.markdown-title'` →
  list of element textContent + href
- Or: extend the default fetcher to dispatch on Content-Type (HTML →
  Playwright, JSON → httpx). When CDP attach mode is on, HTML fetches
  flow through the user's Chrome.

## v1 starter recipes shipped

- `hackernews` (top / new / show) — flat list, JSON, public Firebase API

That's the only one that fits v1 grammar cleanly. Adding more requires
the v2 extensions above. Documented honestly so the limitation is
discoverable, not silently dropped.
