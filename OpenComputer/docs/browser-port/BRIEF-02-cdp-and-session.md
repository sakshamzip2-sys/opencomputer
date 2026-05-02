# BRIEF — `session/` (Wave W1a)

> CDP attach, Playwright session lifecycle, page management, navigation guard.
> Deep dive: [02-cdp-and-session.md](../refs/openclaw/browser/02-cdp-and-session.md) (607 lines — read end-to-end).

## What to build

`extensions/browser-control/session/`:

| File | Public API |
|---|---|
| `cdp.py` | `async def connect_browser(cdp_url: str, *, ssrf_policy) -> Browser` (with dedup map + retry + proxy bypass) · `async def force_disconnect_playwright_for_target(target_id: str) -> None` (uses `Runtime.terminateExecution`, NOT `Connection.close`) |
| `helpers.py` | `redact_cdp_url(url: str) -> str` · `class CdpTimeouts` (HTTP, WS, per-profile clamps) |
| `playwright_session.py` | `class PlaywrightSession`: holds a Browser + Context + active Page, owns the role-ref cache (`role_refs_by_target`), exposes `get_page_for_target(target_id) -> Page`, `list_pages() -> list[Page]` |
| `target_id.py` | `async def page_target_id(page: Page) -> str` (queries `Target.getTargetInfo` over CDP, falls back to `/json/list` HTTP polling) |
| `nav_guard.py` | `async def install_navigation_guard(page: Page, *, ssrf_policy) -> None` — installs `page.route("**/*", handler)` that runs the SSRF check pre-nav AND validates resolved URL post-nav (redirect-chain hostname pinning) |

## What to read first

1. The deep dive's `connect_browser()` algorithm (per-attempt timeout, additive backoff, dedup map, proxy bypass scope).
2. The 3-phase nav-guard design from [03-pw-tools-core.md](../refs/openclaw/browser/03-pw-tools-core.md) deep pass — the during-action listener + post-action 250ms observer interaction.
3. The Python translation table flagging `route.continue_()` (keyword collision), `page.context` (property not method), `_snapshot_for_ai` (undocumented).
4. The "stale Page recovery" section — `page_target_id` + `/json/list` fallback is non-obvious.

## Acceptance

- [ ] `connect_browser` deduplicates concurrent calls to the same CDP URL (test: spawn 5 concurrent calls; only one underlying `playwright.chromium.connect_over_cdp` runs)
- [ ] Connect retries on connection refused with backoff `250 + attempt*250ms`, max 3 attempts
- [ ] Proxy bypass is reference-counted (test: nested calls don't restore NO_PROXY prematurely)
- [ ] `page_target_id` returns ground truth even when Playwright's Page object is stale
- [ ] Falls back to `/json/list` HTTP polling when CDP `Target.getTargetInfo` fails
- [ ] Navigation guard blocks: `file://`, `chrome://`, private IPs, blocked hostnames; **fails closed** when frame can't be resolved
- [ ] Post-nav re-validation catches a redirect that lands on an attacker-controlled host (test with a mock redirect chain)
- [ ] Force-disconnect uses `Runtime.terminateExecution`; **does NOT** call `Connection.close()` (since Playwright shares one Connection across all Browser objects)
- [ ] Role-ref cache survives Page swaps — write a test that deliberately swaps the Page and verifies refs still resolve
- [ ] Tests in `tests/test_session_*.py`
- [ ] No imports from `opencomputer/*`

## Do NOT reproduce

| OpenClaw bug | Don't do |
|---|---|
| `roleRefsByTarget` is FIFO-by-insertion despite "LRU" naming | Either implement true LRU via `collections.OrderedDict.move_to_end` on access, OR keep FIFO but rename the variable to `role_refs_by_target_fifo` |

## Implementation gotchas

These bit OpenClaw and will bite the port if you're not careful:

- **`route.continue_()` is `route.continue_` with the underscore in playwright-python** — `continue` is a Python keyword. Easy typo.
- **`page.context` is a property** in playwright-python, not a method. `page.context()` will TypeError.
- **`Connection.close()` kills every browser**, not just one. Use `Runtime.terminateExecution` to free a stuck eval, then drop the Page reference.
- **Proxy env mutation mid-flight** can race with other processes. The reference-count protects against re-entry but not external mutation. Document the limitation.
- **Page crashes** (`page.on("crash", handler)`) — OpenClaw doesn't listen for this. We should — at minimum, evict the role-ref cache for that target.

## Open questions

- Should we emit the SSRF policy block list as a config option, or hard-code OpenClaw's defaults (private IPs, file://, chrome://)? Recommend: hard-code defaults, allow overrides via config.
- The 250ms post-action observation window — is that the right number for OpenComputer's typical pages, or do we want it configurable? Recommend: keep at 250ms; revisit if real-world flakes appear.

## Where to ask

PR description with `**Question:**` line. Don't block.
