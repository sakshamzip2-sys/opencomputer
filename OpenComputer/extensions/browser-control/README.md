# Browser Control (Playwright)

Default OFF. Opt in by:

1. `pip install opencomputer[browser]`
2. `playwright install chromium` (one-time, ~150MB)
3. Tools auto-register when the plugin loads.

## What this does

Five browser-automation tools backed by Playwright:

- `browser_navigate(url)` — open URL in fresh isolated context, return text snapshot
- `browser_click(url, selector)` — navigate + click element + post-click snapshot
- `browser_fill(url, selector, value)` — navigate + fill text input + snapshot
- `browser_snapshot(url)` — read-only snapshot (alias for navigate)
- `browser_scrape(url, css_selector?)` — scrape text from page (with optional CSS filter)

## What this does NOT do

| Thing | Status |
|---|---|
| Auto-login or use cookies from your real browser | No — isolated session per call |
| Capture screenshots / pixels | No — text accessibility tree only |
| Send any data to a network destination from plugin source | No — AST-enforced |
| Bypass anti-bot measures (Cloudflare, captchas) | No — defaults are NOT stealth |
| Auto-fill passwords / credit cards | No — see CAUTION below |
| Persistent storage between calls | No — each call gets fresh context |

The "no direct network egress" rule is enforced by
`tests/test_browser_control_no_egress.py` — a CI guard that AST-scans
this directory for HTTP-client imports. Adding networking here is a
contract break, not just a code change; it requires updating the
deny-list, this README, and the CHANGELOG.

## Privacy contract

| Captured | Storage | Where it goes |
|---|---|---|
| Page URL + title + accessibility tree + visible text | RAM only | Tool result → agent loop |
| Form input value (browser_fill) | Submitted to the page's JS | Network behavior is the page's |
| Cookies / login state | NOT captured | (isolated context discarded after call) |

## CAUTION — what to never fill

`browser_fill` submits the input value to the page. NEVER use it for:

- Passwords
- Credit-card numbers
- Personal IDs (SSN, passport)

Use it for benign text (search queries, search-form names, etc.) only.
`browser_fill` is gated at the EXPLICIT consent tier so the agent must
justify each call.

## Shared profile (advanced, risky)

If you need login state to persist across calls — e.g. to interact with
a site that requires authentication — set:

```bash
export OPENCOMPUTER_BROWSER_PROFILE_PATH=/path/to/profile
```

This carries cookies + login state. Use only on sites you fully trust.
The agent could submit forms while logged in. **Default is isolated;
opt into shared profile only when you understand the risk.**

## Platform support

| Platform | Status |
|---|---|
| macOS | Supported (Playwright bundles chromium per-OS) |
| Linux | Supported (X server or headless via `--with-deps`) |
| Windows | Supported |
| Headless / SSH | Supported (chromium runs headless by default) |

## Installation

```bash
pip install opencomputer[browser]
playwright install chromium
```

## Troubleshooting

- **"playwright not installed"** — `pip install opencomputer[browser]`
- **"failed to launch chromium"** — `playwright install chromium`
- **Linux missing system libs** — `playwright install --with-deps chromium`
- **Anti-bot block (Cloudflare, etc.)** — out of scope; consider browserless or stealth plugins (not shipped)

`opencomputer doctor` runs the same preflight (`browser-control` row) so
you can confirm the plugin sees Playwright before invoking a tool.

## Disabling

Browser control is opt-in. There's no daemon. Tools become available
when the plugin is registered AND the user has run `playwright install`.
To fully disable, simply don't install the `[browser]` extra — the
tools won't register.
