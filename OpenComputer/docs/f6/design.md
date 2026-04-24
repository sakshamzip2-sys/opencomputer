# OpenComputer F6 — OpenCLI Scraper Plugin Design

> **Phase C1 design doc.** Companion to `opencli-source-map.md` (OpenCLI deep-scan).
> Audience: Session C implementers + Session A integration owner + Session B reviewers.

---

## 1. Goal (recap)

Wrap [OpenCLI](https://github.com/jackwener/opencli) (Apache-2.0) as the `extensions/opencli-scraper/` plugin. The plugin invokes OpenCLI's CLI binary via subprocess, applies our rate limiter + robots.txt cache + per-adapter field whitelist + (eventually) ConsentGate from Session A, and exposes 3 typed tools (`ScrapeRawTool`, `FetchProfileTool`, `MonitorPageTool`) that fetch from a curated 15-adapter set.

**Phase C2 ships the wrapper + adapter whitelists + tools without registration** — Session A wires in consent + signal-normalizer in Phase 4 of the master plan.

---

## 2. Why we wrap (don't import) OpenCLI

| Decision | Reason |
|---|---|
| Subprocess invocation, not Python import | OpenCLI is TypeScript/Node.js — no Python bindings. We'd have to call its CLI either way. |
| Wrapper applies our gates BEFORE OpenCLI runs | Rate-limit + robots check happen in our process; OpenCLI never starts a browser if we deny. Saves Chrome/CDP cycles + reduces error surface. |
| Wrapper sanitises OpenCLI output | Per-adapter field whitelist drops fields we don't want (PII leakage defense). |
| Subprocess isolation = clean failure mode | If OpenCLI crashes, our agent loop is unaffected. |

**License posture (from source map):** Apache-2.0. Closed-source wrapper is allowed; we include LICENSE + NOTICE attribution. **NO AGPL contamination concerns** for this plugin (unlike F7 OI).

---

## 3. Architecture sketch

```
agent loop  →  ScrapeRawTool/FetchProfileTool/MonitorPageTool.execute()
                   │
                   ├─ ConsentGate.require(scope=...)     ← Session A wires this in Phase 4
                   ├─ RateLimiter.acquire(domain)         ← C2: token bucket per domain
                   ├─ RobotsCache.allowed(url)            ← C2: 24h TTL cache
                   ├─ subprocess: opencli <site> <cmd> ...
                   │      (Node.js binary; ours invokes via asyncio.create_subprocess_exec)
                   ├─ FieldWhitelist.filter(adapter, output)
                   ├─ SignalNormalizer.publish(...)        ← Session A wires in Phase 4 tail
                   └─ return Tool result to agent loop
```

Each piece is independent and testable. C2 ships everything except the Session A integration points; those are no-op stubs ready to be replaced.

---

## 4. The 15-adapter shortlist (justified)

From source-map verification: all 15 exist in OpenCLI; field shapes documented; auth requirements known. Our justification for each:

| # | Adapter | OpenCLI path | Strategy | Why this one |
|---|---|---|---|---|
| 1 | `github/user` | `clis/github/` | PUBLIC | Single highest-value dev signal; profile + repos + activity |
| 2 | `reddit/user` | `clis/reddit/user.ts` | PUBLIC | User profile (karma + recent posts) |
| 3 | `reddit/posts` | `clis/reddit/posts.ts` | PUBLIC | User's posts list |
| 4 | `reddit/comments` | `clis/reddit/comments.ts` | PUBLIC | User's comment list |
| 5 | `linkedin/timeline` | `clis/linkedin/timeline.ts` | COOKIE | Professional updates (cookie required — surfaced in consent) |
| 6 | `twitter/profile` | `clis/twitter/profile.ts` | COOKIE | Profile + bio + counts |
| 7 | `twitter/tweets` | `clis/twitter/tweets.ts` | COOKIE | Recent tweet list |
| 8 | `hackernews/user` | `clis/hackernews/` | PUBLIC | HN profile + submissions |
| 9 | `stackoverflow/user` | `clis/stackoverflow/` | PUBLIC | SO profile + top answers |
| 10 | `youtube/user` | `clis/youtube/` | PUBLIC | Channel info + recent uploads |
| 11 | `medium/user` | `clis/medium/` | PUBLIC | Author + recent posts |
| 12 | `bluesky/profile` | `clis/bluesky/` | PUBLIC | Profile + recent skeets |
| 13 | `arxiv/search` | `clis/arxiv/` | PUBLIC | Paper search by author/topic |
| 14 | `wikipedia/user-contributions` | `clis/wikipedia/` | PUBLIC | Contribution log |
| 15 | `producthunt/user` | `clis/producthunt/` | PUBLIC | Maker profile + launches |

**Deferred** (post-MVP, separate consent tier): xiaohongshu, gitlab, mastodon — all exist in OpenCLI but plan §risks edge case #2 flags paid-API or Chinese-region issues.

---

## 5. Strategy → consent prompt mapping

OpenCLI ships 6 strategies (per source map §Strategy Enum). Each maps to a different consent surface:

| Strategy | What it does | Consent prompt language |
|---|---|---|
| `PUBLIC` | Anonymous HTTP fetch | "Fetch public data from `<domain>`. No login required." |
| `LOCAL` | Reads local files only | "Read local file `<path>`. No network." |
| `COOKIE` | Uses your browser cookies | "Use your existing browser session for `<domain>`. **Cookies will be read.**" |
| `HEADER` | Sends specific HTTP headers | "Add header `<name>` to a request to `<domain>`." |
| `INTERCEPT` | Captures browser network | "Watch your browser's network traffic for `<domain>` requests." |
| `UI` | Simulates clicks / typing | "Drive your browser to interact with `<domain>` (clicks/typing visible)." |

**Plan §risks edge case (NEW in self-audit below):** the consent prompt MUST surface the strategy name to the user. A `COOKIE`-strategy scrape is dramatically more powerful than `PUBLIC` — failing to distinguish them in the consent UI would be a security regression.

In our 15-adapter shortlist: 12 are `PUBLIC`, 3 are `COOKIE` (linkedin, twitter profile + tweets). The 3 `COOKIE` adapters get a stricter consent prompt + an explicit `cookie_consent_acknowledged` flag in the consent record (Phase 4 work).

---

## 6. Port-collision mitigation (plan §risks edge case #5)

Source map confirms: OpenCLI uses **port 19825** by default, hardcoded in `src/constants.ts`. Override via `OPENCLI_DAEMON_PORT` env var.

**Our wrapper's strategy:**

```python
class OpenCLIWrapper:
    def _find_free_port(self) -> int:
        """Find a free port in [19825, 19899]; default to 19825 if free."""
        for candidate in [19825, *range(19826, 19900)]:
            if _port_is_free(candidate):
                return candidate
        raise OpenCLIError("No free port in 19825-19899 — check for stale daemon")

    async def _spawn(self, *args):
        port = self._find_free_port()
        env = {**os.environ, "OPENCLI_DAEMON_PORT": str(port)}
        return await asyncio.create_subprocess_exec("opencli", *args, env=env, ...)
```

Each subprocess gets a fresh free port. **Concurrent scrapes use distinct ports** — no contention. (Trade-off: 75 simultaneous scrapes max; way more than realistic usage.)

---

## 7. Rate limiter (per-domain token bucket)

**Why per-domain**: site-specific TOS limits vary wildly. GitHub allows 60 req/hr unauthenticated; Reddit 60 req/min; LinkedIn doesn't publish a number but rate-limits aggressively.

**Defaults** (configurable via plugin manifest):

```python
DEFAULT_LIMITS = {
    "github.com":         (60, 3600),    # 60 per hour
    "reddit.com":         (60, 60),      # 60 per minute
    "linkedin.com":       (30, 60),      # conservative
    "x.com":              (30, 60),      # twitter, conservative
    "news.ycombinator.com": (60, 60),    # generous
    "stackoverflow.com":  (60, 60),
    "youtube.com":        (60, 60),
    "medium.com":         (60, 60),
    "bsky.app":           (60, 60),
    "arxiv.org":          (60, 60),
    "wikipedia.org":      (200, 60),     # MediaWiki API is generous
    "producthunt.com":    (60, 60),
    "*":                  (30, 60),      # default for unknown
}
```

Token bucket algorithm: `acquire(domain)` blocks until a token is available. Implementation: simple in-memory counter + asyncio.Lock, ~30 lines.

---

## 8. Robots.txt cache (24h TTL)

Goal: stop the agent from scraping disallowed paths. OpenCLI doesn't enforce robots.txt; we layer it on top.

```python
class RobotsCache:
    async def allowed(self, url: str) -> bool:
        domain = urlparse(url).netloc
        rp = await self._fetch_or_cached(domain)
        return rp.can_fetch(USER_AGENT, url)

    async def _fetch_or_cached(self, domain: str) -> urllib.robotparser.RobotFileParser:
        if domain in self._cache and (now - self._cache[domain].fetched_at) < 86400:
            return self._cache[domain].parser
        # Fetch + parse + cache
```

Uses Python stdlib `urllib.robotparser` — zero new deps.

---

## 9. Field whitelist (per-adapter)

**Why**: OpenCLI returns rich data per adapter (e.g., `linkedin/timeline` returns dozens of fields including the user's email/phone if present in the post). We don't want all of that — most agent use cases need only `{title, url, posted_at, snippet}`. Whitelisting:
- Reduces PII surface
- Keeps tokens cheap when scraped data is fed back into the LLM
- Forces explicit decisions about what's needed

```python
FIELD_WHITELISTS = {
    "github/user": {"login", "name", "bio", "public_repos", "followers", "html_url"},
    "reddit/user": {"name", "karma", "created_utc"},
    "linkedin/timeline": {"author", "text", "posted_at", "url"},  # NOT email/phone
    # ... 12 more entries
    "*": None,  # explicit "fail closed" — unknown adapters return [] until whitelisted
}
```

Unknown adapter → empty result + warning log. Forces the implementer to add an explicit entry before any new adapter ships data.

---

## 10. Three-tool surface

```python
class ScrapeRawTool(BaseTool):
    """Low-level: invoke any whitelisted adapter directly with raw args."""
    schema = {"name": "ScrapeRaw", "input": {"adapter": str, "args": list[str]}}

class FetchProfileTool(BaseTool):
    """High-level: fetch user profile from a known adapter family."""
    schema = {"name": "FetchProfile", "input": {"platform": str, "user": str}}

class MonitorPageTool(BaseTool):
    """Long-poll: re-fetch a URL on schedule, return diff if changed (uses C4 content_monitoring)."""
    schema = {"name": "MonitorPage", "input": {"url": str, "interval_s": int}}
```

C2 ships these as classes + `execute()` methods that work today (callable from tests). They are **NOT** registered with the global `ToolRegistry` — Session A registers them in Phase 4 once consent + signal-normalizer are wired.

---

## 11. Subprocess bootstrap

OpenCLI requires Node.js + Chrome/Chromium. Our `subprocess_bootstrap.py`:

1. Detect `opencli` binary (via `shutil.which("opencli")` first, then common paths)
2. If not found → emit clear error with install instructions (`npm install -g @jackwener/opencli`); do NOT auto-install
3. Detect Chrome/Chromium; same fallback
4. Verify Node.js >= 18 if `opencli --version` doesn't return cleanly

**No auto-install**: the plugin refuses to operate until the user provides the binaries. Surfacing the requirement explicitly is safer than silent installs.

---

## 12. Tests (C2 spec)

| Test file | What it covers | ~Tests |
|---|---|---|
| `test_opencli_wrapper.py` | Subprocess spawn (mocked), env-var port override, error path mapping | 8-10 |
| `test_opencli_rate_limiter.py` | Token-bucket math, per-domain isolation, default fallback | 6-8 |
| `test_opencli_robots_cache.py` | TTL behavior, allow/deny per User-Agent, mocked HTTP | 6-8 |
| `test_opencli_field_whitelist.py` | Per-adapter entries return only whitelisted fields, unknown adapter returns [] | 8-10 |
| `test_opencli_tools.py` | 3 tool schemas; execute() with mocked subprocess; consent stub plumbing | 12-15 |
| `test_opencli_subprocess_bootstrap.py` | Binary detection happy path + missing-binary error message | 4-6 |

Target ~50-60 tests. **Mock every external** — no live network, no live `opencli` binary in CI.

---

## 13. Manifest

```json
{
  "id": "opencli-scraper",
  "name": "OpenCLI scraper",
  "kind": "tools",
  "version": "0.1.0",
  "enabled_by_default": false,
  "description": "Wraps OpenCLI for safe, consented web scraping. 15 curated adapters.",
  "schema_version": 1
}
```

`enabled_by_default: false` is the safety lock per plan §refinements.

---

## 14. What ships in C2 vs what waits for Session A's Phase 4

**C2 ships (this plan's scope):**
- `wrapper.py`, `tools.py`, `rate_limiter.py`, `robots_cache.py`, `field_whitelist.py`, `subprocess_bootstrap.py`
- `plugin.py` with `register(api)` that does NOT register the tools (returns early, logs "awaiting Phase 4 integration")
- 50-60 tests
- LICENSE + NOTICE for OpenCLI attribution

**Session A wires in Phase 4:**
- `ConsentGate.require(...)` calls inside each tool's `execute()`
- `SignalNormalizer.publish(...)` after each successful scrape
- `api.register_tool(...)` calls in `plugin.py::register`
- `enabled_by_default` flip (only after legal review)

---

# Part I — Self-audit (expert critic)

## Flawed assumptions

1. **"Field whitelist is enough to handle PII."** Wrong. A whitelisted field like `linkedin.author` may itself contain PII (the user's name, employer, location). Whitelisting only stops UNKNOWN fields; it doesn't redact KNOWN fields. **Mitigation:** add a per-field redactor (mask emails, phone numbers, SSNs) on top of the whitelist. Document this as C2.5 follow-up.

2. **"OpenCLI's `opencli` binary is available globally."** Many users install via `npx` instead of `npm install -g`. Our `shutil.which("opencli")` would fail. **Mitigation:** also try `npx --no-install opencli` (returns the binary if available locally without triggering an install).

3. **"Per-domain rate limit table covers all access patterns."** Wrong: a single LinkedIn page load may trigger 5+ XHRs. Our token-bucket counts the OpenCLI invocation, not the underlying HTTP requests. **Mitigation:** doc-comment that rate limits are per-INVOCATION; underlying request counts are OpenCLI's responsibility. If we hit upstream rate-limit errors, surface them clearly (don't retry blindly).

4. **"Robots.txt parsing via stdlib is reliable."** stdlib's `urllib.robotparser` is decade-stable but doesn't support modern extensions like `Crawl-delay` or sitemap directives. **Mitigation:** acceptable for MVP; document the limitation.

5. **"15 adapters cover the agent's needs."** Speculative — no user research. **Mitigation:** ship 15 + provide an `add_adapter(slug, whitelist)` extension hook so users can add more without code changes.

## Edge cases not addressed

1. **OpenCLI version skew.** If user has OpenCLI 1.6.x but our adapter list assumes 1.7.x field names. **Mitigation:** wrapper checks `opencli --version` against a `MIN_OPENCLI_VERSION` constant; fail-fast with clear error.

2. **Chrome window stays open after a crash.** OpenCLI launches isolated Chrome; if our subprocess dies between `launcher.spawn()` and `launcher.kill()`, Chrome lingers. **Mitigation:** wrapper uses `asyncio.subprocess` with `try/finally` cleanup + a SIGTERM-on-exit hook.

3. **User has multiple LinkedIn cookies (work + personal).** OpenCLI uses whichever Chrome profile is default. We have no way to disambiguate. **Mitigation:** document as a known limitation; surface in consent prompt: "Will use your default Chrome profile's cookies."

4. **Network change mid-scrape (Wi-Fi switch).** OpenCLI's HTTP retries may misbehave. **Mitigation:** wrap subprocess timeout (default 60s); on timeout, log + return clear error.

5. **Robots.txt unreachable (404 / 5xx).** Should we deny by default or allow? **Decision:** allow on 404 (no robots.txt = no restrictions per RFC); deny on 5xx (could be blocking us deliberately). Document.

6. **Concurrent scrapes from the same agent loop turn.** Could exhaust the 75-port budget. **Mitigation:** add a global semaphore capping concurrent scrapes at 8 (configurable). Plenty of headroom under the 75 free ports.

## Missing considerations

1. **Adapter version pinning.** If OpenCLI updates `linkedin/timeline` to add/remove fields, our whitelist is brittle. **Mitigation:** pin OpenCLI version in subprocess_bootstrap.py; doc-bump procedure for upgrades.

2. **Cookie-strategy adapters need explicit "ok to use cookies" consent.** Per §5 above. C2 spec must include a `requires_cookie_consent: bool` flag in the manifest entry per adapter.

3. **Scrape-result cache.** Repeated identical scrapes within a turn re-spawn subprocess. **Mitigation:** add a 5-min in-memory cache keyed by `(adapter, args)`. Skip on stale data — agent can pass `force=True` to bypass.

4. **Audit log integration.** Each scrape should log to Session A's audit log (when ready) for forensic review. **Mitigation:** add an `AuditEvent.append(actor="opencli-scraper", action="scrape", target=domain, ...)` call point — wire in Phase 4.

5. **What if OpenCLI's daemon crashes mid-scrape?** Our wrapper hangs waiting on a response. **Mitigation:** subprocess timeout (default 60s); on timeout, kill daemon explicitly.

6. **Sandboxing the subprocess.** OpenCLI launches Chrome with full network access. On macOS, we could optionally invoke under `sandbox-exec`. **Decision:** out of C2 scope; document as Session A's Phase 4 sandbox-strategy concern.

## Refinements applied to plan

- **Added §12 PII redactor as C2.5 follow-up** (initially missing).
- **Subprocess detection includes `npx --no-install`** fallback (not just `which opencli`).
- **MIN_OPENCLI_VERSION constant** + version check in wrapper.
- **Concurrent scrape semaphore** (cap 8) added to wrapper design.
- **Scrape-result 5-min cache** added (skippable via `force=True`).
- **`requires_cookie_consent: bool` flag** in per-adapter whitelist entries (instead of inferring from strategy).

---

# Part II — Adversarial self-review

## Alternative #1 — Vendor OpenCLI as a Python library (rejected)

**Shape:** Port OpenCLI's adapters to Python; eliminate Node.js dependency.

**Pros:** No subprocess; no Node.js install; faster.

**Cons:** OpenCLI is 624 commands across 103+ sites; porting is years of work. Upstream maintains adapters actively; we'd diverge immediately. Apache-2.0 allows it but the engineering cost is prohibitive.

**Verdict:** Rejected. Subprocess wrapper is the right call.

## Alternative #2 — Use Playwright directly, skip OpenCLI (rejected)

**Shape:** Write our own Playwright-based scraper for the 15 sites; no OpenCLI dependency at all.

**Pros:** No subprocess; full control; no version-skew with upstream.

**Cons:** Maintenance burden. Each site changes layouts every few months. OpenCLI's community keeps adapters updated; we lose that. Also: 15 adapters × ~200 lines of Playwright per adapter = 3000+ lines vs ~500 lines of subprocess wrapper.

**Verdict:** Rejected. The community-maintenance argument is decisive.

## Alternative #3 — Direct API integrations only, drop browser scraping (partial accept)

**Shape:** For the 5 platforms with public APIs (GitHub, Reddit, HN, StackOverflow, arXiv), skip OpenCLI entirely; use HTTP client with bearer tokens. Use OpenCLI only for the 10 that require browser sessions.

**Pros:** API calls are cheaper, faster, more reliable. Avoids cookie-leak risk for those 5.

**Cons:** Two code paths to maintain. API tokens require user setup.

**Verdict:** PARTIAL ACCEPT — defer to a Phase 4 follow-up. C2 ships unified-via-OpenCLI MVP; if the API-only path proves valuable, add it as a per-adapter routing decision in `wrapper.py`.

## Hidden assumptions surfaced

1. **"Users have Chrome installed."** Many corp Macs have only Safari. **Mitigation:** subprocess_bootstrap detects Chromium too; if neither, clear error + install link.

2. **"OpenCLI's daemon doesn't leak data."** Source map says "no telemetry; all data stays on localhost" — but OpenCLI's privacy posture could change in a future version. **Mitigation:** pin to a specific OpenCLI version; only bump after re-reading PRIVACY.md.

3. **"15 adapters worth shipping immediately."** Only 5 of the 15 are in the user's known interest area (dev tools: GitHub, HN, SO, arXiv, GitLab). Others are speculative. **Mitigation:** consider "5 high-confidence + 10 speculative" tiering — promote speculative ones based on usage signal (Session B evolution can help here).

4. **"ConsentGate covers our needs."** Session A's ConsentGate is being designed for tool-call-level granularity. Scraping is a multi-step subprocess; we may need a finer "consent for this domain for the next N minutes" surface. **Mitigation:** flag this as a coordination item with Session A.

## Quantified uncertainty

| Claim | Confidence | Swing |
|---|---|---|
| Subprocess wrapper effort ~1 week | 80% | Could double if OpenCLI's CLI returns inconsistent JSON across adapters |
| All 15 adapters work without auth (apart from the 3 cookie ones) | 70% | Could be 90% if adapters are clean; could drop to 50% if some demand undocumented headers |
| Rate-limit defaults are realistic | 60% | Need real-world testing; defaults are educated guesses |
| Robots.txt enforcement is correct for all sites | 75% | stdlib parser limitations could miss edge cases |
| Field whitelist captures PII concerns | 70% | Per §13.1, PII can hide INSIDE a whitelisted field |

## Worst-case edges

**WC1 — OpenCLI ships a malicious adapter update.** Supply-chain attack. **Mitigation:** pin version in subprocess_bootstrap; review changelog before bump.

**WC2 — User's Chrome has malware extensions.** OpenCLI uses isolated Chrome; theoretically immune, but extensions in the user's main Chrome profile could persist cookies that leak through `COOKIE`-strategy adapters. **Mitigation:** consent prompt explicitly names which Chrome profile is in use.

**WC3 — Rate limit too generous; OpenCLI gets us banned.** Some sites ban the IP, not the account. **Mitigation:** start conservative (defaults above are intentionally lower than published limits); allow user to tune up.

**WC4 — User has port 19825 in use by another tool.** Our free-port scan handles this. ✓

**WC5 — `opencli` binary returns garbled output (encoding mismatch).** **Mitigation:** wrapper sets `PYTHONIOENCODING=utf-8` + reads bytes, decodes with `errors='replace'`; never crashes on malformed output.

## Refinements applied after adversarial review

- **Pin OpenCLI version** in `subprocess_bootstrap.py`; document upgrade procedure
- **Consent prompt names Chrome profile** explicitly when COOKIE strategy is in use
- **Conservative rate limits as defaults** (already in §7; emphasizing here)
- **Encoding-safe subprocess output reading**
- **5+10 adapter tiering** option flagged for Session A's review
- **API-direct alternative kept on table** for Phase 4 follow-up (per Alternative #3)

---

## 15. Status

C1 design locked. C2 implementation tasks map 1:1 to §3 architecture sketch. PR review by Session A confirms the 15-adapter shortlist + strategy-to-consent mapping before C2 starts.

**Coordination items for Session A** (please flag in PR review):
- Is ConsentGate's per-tool-call granularity sufficient, or do we need per-domain-with-TTL?
- Should `enabled_by_default: false` flip to `true` once Phase 4 wiring is done, or stay false until explicit user opt-in?
- API-direct routing for the 5 API-supporting platforms (Alternative #3) — accept now, defer, or reject?
