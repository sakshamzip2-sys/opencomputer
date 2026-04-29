---
name: failure-recovery-ladder
description: Use when a tool, fetch, search, scrape, or API call fails (403, paywall, empty result, timeout, rate-limit) — provides the per-failure-type recovery ladder so you don't fold at the first failure. Especially load before reporting "I couldn't find X" to the user.
---

# Failure recovery ladder

You are the agent. The user gave you a task with an end-state ("find X", "fetch Y", "summarize Z"). A tool just failed. **The failure is not the answer.** The answer is what you produce after walking this ladder.

This skill exists because there is one persistent bug pattern in agentic loops: at the first 403 or empty result, the agent says *"I tried but it didn't work, want me to try X?"* and hands the work back. That's stalling, not collaboration. The user already asked. Walk the ladder, *then* report.

## The discipline in three rules

1. **Two attempts of the same approach is the cap.** Two attempts across different rungs of the ladder is not — that's the work.
2. **Stop asking. Start trying.** Read-only attempts to fulfill the original request are pre-authorized. The user is not your gatekeeper for `WebFetch`, `WebSearch`, `Read`, or alternative URLs.
3. **Report concretely or not at all.** "It didn't work" is not a report. "I tried A (403), B (no results), C (paywalled), D (returned the data — see below)" is a report. So is "I tried A/B/C/D and all failed; here's what's left to try / what info would unblock me."

## The ladders, by failure type

### Web fetch → 403 / 401 / paywall / login wall

Walk this in order. Stop the moment one rung returns the data:

1. **Try a different URL on the same domain.** Look for `/sitemap.xml`, `/feed.xml`, `/feed`, `/rss`, AMP (`?output=amp` or subdomain), mobile subdomain (`m.`), printable variants (`?print=1`, `/print/`).
2. **Try cached copies.** `WebSearch` for the title or unique phrase + `cache:` operator. Try archive.org's wayback (`https://web.archive.org/web/<URL>`).
3. **Try the underlying API.** Many sites expose a JSON endpoint (`/api/`, `/wp-json/`, `?format=json`). Inspect the page's network tab equivalent — look for `<script type="application/ld+json">` or `__NEXT_DATA__` blobs.
4. **Try a related authoritative source.** Original press release, the company's investor-relations page, a regulatory filing, the GitHub repo's README, the official docs site.
5. **Try aggregators / mirrors.** Reddit threads, HN comments, archive sites, content mirrors that quote or excerpt the source.

Only when 1-5 are exhausted: **report concretely** — "Source X is 403; I tried sitemap (404), archive.org (older snapshot from 2024), the press release (different angle), the official docs (don't cover this case). Here's what I got from the closest substitute, and the gap is Z."

### Search → empty / irrelevant results

1. **Reformulate.** Broader terms, drop adjectives, try synonyms, try the question form vs. the keyword form, try the negation form ("X without Y").
2. **Switch backend.** If `WebSearch` is multi-provider, the next call may use a different provider; if not, swap to a domain-specific search (the project's GitHub issues, the official docs search, Stack Overflow's tag pages).
3. **Drop down to the source.** Skip search and fetch the canonical domain directly (the GitHub repo, the docs site, the company site).
4. **Try a related but distinct query.** If "X benchmarks" fails, try "X performance comparison" or "X vs Y benchmarks". The user's mental model of the topic and the indexed phrasing don't always match.

### Scrape / parse → no useful content

1. **Check JS-rendering.** If the raw HTML doesn't contain the data, the page is JS-rendered. Look for the underlying JSON (`__NEXT_DATA__`, `<script type="application/ld+json">`, network XHR endpoints) before giving up.
2. **Try variant URLs.** AMP / mobile / print versions are usually static and contain the same content.
3. **Inspect raw HTML.** Download with `WebFetch` or `Bash curl`, then `Read` the file. Sometimes the content IS there but the parser is wrong.
4. **Try a related page on the same site.** A topic page, a category index, an author page — these aggregate the same content.

### Tool errored / API rate-limited / 429 / 503

1. **Read the message.** "Rate-limited, retry-after: 30s" is an instruction, not an error.
2. **Wait + retry once.** Use exponential backoff if you'll loop more than once.
3. **Switch tool.** If `WebFetch` rate-limited on a domain, `WebSearch` may surface the same content via a different fetch path.
4. **Split the request.** A 5MB scrape failing → 5×1MB scrapes. A 100-row query failing → 10×10-row queries.

### Build / test / lint failed

1. **Read the actual error message.** Not the summary — the actual line, file, and traceback.
2. **Fix the diagnosed cause.** Don't randomly mutate code; understand the failure first.
3. **Re-run.** Fix-and-rerun is the work, not a question to ask the user.
4. **If the cause is unclear after diagnosis** (mysterious flake, environment-dependent), then report — with the actual error text, what you ruled out, and what you tried.

### File / path not found

1. **Verify your assumption.** `ls` the parent directory; the file might be at a slightly different path or have a different extension.
2. **Glob broader.** Drop the prefix; search by the most distinctive part of the filename.
3. **Grep the codebase.** A symbol that's referenced somewhere is defined somewhere — find the definition by searching for the reference.
4. **Check whether it should exist.** Maybe the file was renamed (git log) or the user is asking about a file in a sibling repo.

## Anti-patterns — name-and-shame

When you catch yourself doing one of these, stop, name the pattern out loud (in chain-of-thought), and walk the ladder instead:

- **Asking-as-stalling.** "Want me to try the archive?" when the archive is the obvious next rung. The user already authorized read-only retrieval. Don't ask — try.
- **Narrating dead-ends as the report.** "The page returned 403, so the info isn't accessible." 403 is rung 0; the report comes after rung 5.
- **Premature surrender phrasing.** "I'm not sure I can find this" before you've actually walked the ladder. Either find it or report concretely after trying. No hedging in advance.
- **Capability disclaimers as substitute for effort.** "I don't have access to live X" — sometimes true, but say it after the ladder, not before.
- **Single-attempt verdicts.** "Search returned no results, so this doesn't exist." One query is one data point. Try three phrasings before declaring nonexistence.

## When to actually stop

The ladder is exhausted when:
- You've tried 3-5 rungs of the appropriate ladder and none returned usable data.
- The failure is fundamental (the URL genuinely 404s on every variant; the search engine has zero results across multiple phrasings; the API is down on retry).
- Continuing requires user input (credentials, a specific account, a real-world action you can't take).

In those cases, report:
1. **What you tried** (the ladder rungs, by name).
2. **What each returned** (one line each — "sitemap.xml: 404", "archive.org: snapshot from 2023, doesn't cover the new feature").
3. **What's left** (the rungs that need user input — "I'd need your login for the paywalled source", or "this looks genuinely unindexed").
4. **Best partial answer you have** (don't hand back nothing — give the closest thing you found and flag the gap explicitly).

That report is useful. "It didn't work" is not.
