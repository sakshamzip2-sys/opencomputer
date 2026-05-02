# BRIEF — `tools_core/` (Wave W2a — the workhorse)

> Implementation of every action verb (click/type/press/hover/drag/select/fill/wait/evaluate/close/resize) plus snapshot orchestration, downloads, dialog, file-chooser, storage, trace, emulation. **The densest subsystem.**
> Deep dive: [03-pw-tools-core.md](../refs/openclaw/browser/03-pw-tools-core.md) (797 lines — read end-to-end, especially the per-act-kind playbook).

## What to build

`extensions/browser-control/tools_core/`:

| File | Public API |
|---|---|
| `interactions.py` | `async def execute_single_action(page, kind: BrowserActKind, params: dict, *, ssrf_policy) -> dict` — the central switch over all 11 act kinds; each wraps the Playwright call in `assert_interaction_navigation_completed_safely` (see below) |
| `snapshot.py` | `async def snapshot_role_via_playwright(page: Page, *, mode: Literal["role", "aria"], max_chars: int \| None) -> SnapshotResult` |
| `refs.py` | `def ref_locator(page: Page, ref: str, refs: RoleRefMap) -> Locator` — dispatches role-mode vs aria-mode |
| `downloads.py` | `async def arm_download(page: Page) -> DownloadHandle` · `async def capture_download(handle, ...) -> DownloadResult` — last-arm-wins throws `"superseded"` to the prior caller |
| `dialog.py` | `async def arm_dialog(page: Page, *, accept: bool, prompt_text: str \| None, timeout_ms: int) -> dict` — last-arm-wins **silently** no-ops |
| `file_chooser.py` | `async def arm_file_chooser(page: Page, *, paths: list[str], ref: str \| None, ...) -> dict` — last-arm-wins silent no-op (matches dialog) |
| `activity.py` | `def record_action(target_id: str) -> None` · `def last_action_time(target_id: str) -> float \| None` |
| `storage.py` | Cookies (context-wide via `context.cookies()` / `context.add_cookies()`), localStorage / sessionStorage (origin-scoped via `page.evaluate`) |
| `trace.py` | `async def start_trace(context, *, screenshots: bool, snapshots: bool) -> None` · `async def stop_trace(context, *, path: str) -> str` |
| `responses.py` | `async def read_response_body(response, *, max_bytes: int) -> dict` — **NOT** an envelope normalizer (the deep pass corrected this). Just an HTTP response body reader for the `browser body` route. |
| `state.py` | Emulation knobs only: offline, headers, credentials, geolocation, locale, timezone, device. **NOT** page state. |
| `shared.py` | Timeout clamps, scroll-into-view helper, `assert_interaction_navigation_completed_safely(...)` (the 3-phase nav-guard observer) |

## What to read first

1. The deep dive's per-act-kind playbook (12 kinds, with Python snippets) — your single most important reference.
2. The 3-phase nav-guard design — during-action listener + post-action 250ms observer in **both** success and error paths.
3. The `evaluate` deep dive — Playwright's `{timeout}` only bounds function installation, not execution. To kill long-running JS you need `Promise.race` injection or force-disconnect.
4. The wrap status table for all 13 kinds (note: 13, not 11 — `highlight` and `setInputFiles` exist outside the standard dispatch).

## Acceptance

- [ ] All 11 act kinds in `BrowserActKind` execute correctly against a real Page (e2e test against a local fixture HTML)
- [ ] Each mutating kind (click, type, press, evaluate) wraps in `assert_interaction_navigation_completed_safely`
- [ ] Non-mutating kinds (hover, drag, select, fill, scrollIntoView, wait) do NOT wrap
- [ ] `ref_locator` resolves both role-mode and aria-mode refs, including nth-disambiguation
- [ ] Refs that no longer resolve raise a typed error; agent gets a "re-snapshot needed" hint
- [ ] Downloads: arm → click-link → capture → file saved under per-session dir; second arm without trigger raises `"superseded"` to first caller
- [ ] Dialog + file chooser: silent last-arm-wins (test that double-arm doesn't error)
- [ ] `evaluate` respects timeout — both function installation AND execution. Long-running JS aborts within timeout window.
- [ ] Storage: cookies are context-wide; localStorage/sessionStorage are origin-scoped (test by setting on `https://a.com` and reading on `https://b.com` — should be empty)
- [ ] Trace start/stop produces a `.zip` at the requested path, viewable with Playwright trace viewer
- [ ] State emulation: setting offline=true makes `navigator.onLine` return false in the page
- [ ] Tests in `tests/test_tools_core_*.py`
- [ ] No imports from `opencomputer/*`

## Do NOT reproduce

No bugs explicitly flagged from this subsystem in the deep dive — but read the per-kind playbook carefully because OpenClaw's stabilization sequencing is the kind of thing it's easy to get *almost* right.

## Implementation gotchas

- **`_snapshot_for_ai` is a hard cliff.** `playwright-python` doesn't expose it (it's a TypeScript-internal underscore method). Strategy: ship `mode="role"` for v0.1 using public `aria_snapshot()`. Add `mode="aria"` later if/when the underscore surfaces in playwright-python.
- **Activity tracking** is shared global state — use a module-level `dict[str, float]`. Cleared on profile reset.
- **Storage security model** — cookies are first-party / domain-scoped per Playwright; localStorage is origin-scoped. Don't conflate.
- **Snake-case API**: `aria_snapshot` (not `ariaSnapshot`), `set_input_files` (not `setInputFiles`), `expect_download` context manager (not arm pattern), `expect_file_chooser` context manager.

## Open questions

- All 11 kinds for v0.1, or trim to the most-used 6 (click/type/press/fill/snapshot/wait)? Recommend **all 11** — the marginal cost is small, and the surface gets baked into the model's expectations.
- Trace recording on by default or behind a config flag? Recommend **off by default**; flag controls it.

## Where to ask

PR description with `**Question:**` line.
