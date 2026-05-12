# OC splash — replace Hermes-derived design with OpenCode-style minimal home

**Date:** 2026-05-12
**Owner:** Saksham / Claude (Opus 4.7, learning mode)
**Status:** Design approved → executing
**Source of truth:** sst/opencode `packages/opencode/src/cli/cmd/tui/routes/home.tsx`, cloned to `sources/opencode/` at SHA pinned 2026-05-12.
**Scope correction (mid-Phase 4):** `ui-tui/**` and `ui-web/**` are not ours — pulled out of scope. Python `oc chat` CLI is the only surface touched.

## 1. Problem

Both surfaces that show OC's startup splash carry **Hermes-derived patterns** that no longer match OC's positioning:

- `ui-tui/src/components/branding.tsx` line 51 falls back to the literal string `"NOUS HERMES"` when the terminal is too narrow to render the logo art. Hard-coded brand leak.
- `ui-tui/src/components/branding.tsx` line 55 renders the Hermes tagline: `"Nous Research · Messenger of the Digital Gods"`. Hard-coded.
- `ui-tui/src/banner.ts` (referenced by branding.tsx) is **missing entirely** — the import resolves to nothing, so the file as-written doesn't compile.
- `ui-tui/package.json:6` description says `"Hermes shell, OC backend."` — still framing OC as a Hermes derivative.
- `opencomputer/cli_banner.py` implements the "Option D HUD": mascot + letterspaced wordmark + 4-column runtime grid (MODEL / PROVIDER / CWD / SESSION) + chip rows for tools/skills + footer. This **info-load-on-splash** pattern is a Hermes inheritance, not an OpenCode one.
- `opencomputer/cli_banner_art.py:6` says `"Visual register modeled after hermes-agent's banner.py"`.

## 2. What OpenCode actually does

Verified by reading `sources/opencode/packages/opencode/src/cli/cmd/tui/routes/home.tsx`:

```
┌─ terminal ──────────────────────────────────┐
│                                              │
│                                              │
│                                              │
│              OPEN  CODE   logo               │  ← animated half-block wordmark
│                                              │     (centered, ~39 cols × 4 rows)
│                                              │
│              > _____________                 │  ← prompt, maxWidth=75
│                                              │
│                                              │
│                                              │
└─ statusline (plugin slot, app_bottom) ──────┘  ← model/ctx/cwd live here
```

Hard rules of OpenCode's home:

1. **No model name on splash.** Lives in `/models` dialog and the statusline.
2. **No cwd on splash.** Lives in the statusline.
3. **No session-id on splash.** Lives in `/sessions` dialog.
4. **No tool count.** Tools are listed only when relevant (autocomplete, slash menu).
5. **No skill count.** Same.
6. **No version+commit on splash.** Renderer sets terminal title `OpenCode` instead.
7. **Logo + prompt centered.** Vertical centering via `flexGrow={1}` spacers above and below.
8. **Logo is the personality.** Animated shimmer, mouse-interactive, sound on press (we deliberately skip these in v1 — see YAGNI sweep).

## 3. Design — OC adopts the OpenCode pattern

### 3.1 New home/splash layout (both Python CLI and Ink TUI)

```
┌─ terminal ──────────────────────────────────┐
│                                              │
│                                              │
│  █▀▀█ █▀▀█ █▀▀▀ █▄ █ ▄▀▀ ▄▀▀█ █▄ ▄█ ...   v2026.5.10.post3 · c2f3d31
│  █▄▄█ █▀▀▀ █▀▀  █ ██ █   █  █ █ ▀ █ ...    
│  ▀  ▀ ▀    ▀▀▀▀ ▀  ▀  ▀▀ ▀▀▀▀ ▀   ▀ ...
│                                              │
│  › Ready.  Type a message, or /help     /status · /model · /help · /exit
│                                              │
└──────────────────────────────────────────────┘
```

Components:

1. **Logo block** — 3-row half-block `OPENCOMPUTER` wordmark in `t.color.primary`. Reuses the existing `OPENCOMPUTER_BLOCK_LOGO` (71 cols, already in `cli_banner_art.py`, already validated by tests).
2. **Version-right** — `v{__version__} · {git_short_sha}` on the same row as the logo's middle row, pulled to right edge of terminal, in primary+muted.
3. **Footer row** — `› Ready. Type a message, or /help` (left, primary+text+muted+text) and `/status · /model · /help · /exit` (right, muted). Single row; gap is justified to opposite edges.
4. **Narrow-terminal fallback** — When `cols < 73`, drop the half-block art and render `OPENCOMPUTER` bold text only. Version-right wraps under it.

No mascot. No 4-column grid. No tool chips. No skill chips. No second rule. No tagline.

### 3.2 Where the runtime info actually goes

The model/cwd/session/tool-count/skill-count don't disappear — they relocate:

| Field | New home |
|-------|----------|
| Model | `StatusRule` statusline (already exists at `appChrome.tsx:264`) + `/models` dialog |
| CWD | `StatusRule.cwdLabel` (already exists) |
| Session ID | `/sessions` slash command output + terminal title |
| Tools (registered) | `/status` slash command (existing tools/registry) |
| Skills (registered) | `/status` slash command |
| Version + commit | Logo right-justified line (only first-impression spot) |

For the Python CLI (`oc chat`), the statusline equivalent is the user's shell prompt — `cwd` and `model` are surfaced via `oc status` / `oc models` subcommands. No statusline component is added on the Python side (already what `oc` does today via `cli_banner.py` doesn't track turn-state anyway).

## 4. Files touched

| File | Change | LOC |
|------|--------|-----|
| `ui-tui/src/components/branding.tsx` | Replace `Banner()` body (remove `NOUS HERMES` fallback, render OC half-block wordmark). Rewrite `SessionPanel()` body (drop mascot/grid/chips/rule; render only version-right + footer). Strip dead `banner.js` import. Add inline `OPENCOMPUTER_ROWS` art constant. | ~-150 / +110 |
| `ui-tui/src/components/appLayout.tsx` | No change; `<Banner>` + `<SessionPanel>` still mount the same way. | 0 |
| `ui-tui/package.json` | Description: `"OpenComputer Ink+React TUI."` (drop "Hermes shell"). | 1 |
| `opencomputer/cli_banner.py` | Rewrite `build_welcome_banner()` body — drop mascot, grid, chips, second rule. Render OC half-block wordmark (reuse `OPENCOMPUTER_BLOCK_LOGO`), version-right on its middle row, footer. Keep `get_available_skills()`, `get_available_tools()`, `format_banner_version_label()`, `_split_model_provider()`, `_shorten_session()` exports — they migrate to a future `oc status` slash. | ~-200 / +90 |
| `opencomputer/cli_banner_art.py` | Update header docstring: drop "modeled after hermes-agent" framing. Mark `OPENCOMPUTER_LOGO` (figlet slant) + `SIDE_GLYPH` deprecated, still exported for back-compat tests. | ~-5 / +5 |
| `tests/test_cli_banner.py` | Rewrite all `build_welcome_banner` assertions — no `TOOLS · N`, no `MODEL`, no chip borders. Add new assertions: logo block present, version-right, footer text, narrow-terminal fallback. Keep tests for `get_available_skills/tools`, `format_banner_version_label`, `OPENCOMPUTER_LOGO/_FALLBACK/SIDE_GLYPH` art constants (back-compat), Pico tests (unrelated). | ~-150 / +110 |
| `ui-tui/src/__tests__/branding.test.tsx` | New file. Test `Banner()` renders `OPENCOMPUTER` somewhere, never `NOUS HERMES`. Test `SessionPanel()` renders version + footer hints. Test narrow-terminal fallback. | +80 |
| `docs/superpowers/specs/2026-05-12-oc-splash-replace-hermes-design.md` | This spec. | +250 |

Total: roughly −500 / +650 net. Strictly fewer than the current Option D HUD.

## 5. API stability

Public symbols preserved (so external imports don't break):

- `opencomputer.cli_banner.build_welcome_banner(console, model, cwd, *, provider=None, session_id=None, session_label=None, home=None)` — same signature; ignored args (`provider`, `session_id`, `session_label`, `home`) still accepted but no longer rendered on the splash. Documented in the docstring.
- `opencomputer.cli_banner.get_available_skills()`, `get_available_tools()`, `format_banner_version_label()` — unchanged.
- `opencomputer.cli_banner_art.OPENCOMPUTER_LOGO`, `OPENCOMPUTER_LOGO_FALLBACK`, `SIDE_GLYPH`, `OPENCOMPUTER_BLOCK_LOGO` — unchanged.
- `ui-tui` `branding.tsx` exports: `Banner`, `SessionPanel`, `Panel`, `ArtLines` — all kept. Internals rewritten.

## 6. Error handling & failure surface

| Failure | Handling |
|---------|----------|
| Terminal cols < 73 | Drop half-block art; render `OPENCOMPUTER` bold text only. |
| `__version__` empty / None | Skip the version-right cluster entirely. |
| `_git_short_sha()` returns None | Render `v{__version__}` without the ` · sha` suffix. |
| `cli_update_check.get_update_hint()` throws | Existing `except Exception: pass` — fail-open. |
| Terminal doesn't support UTF-8 | Half-block chars (`█▀▄`) render as `?` or replacement char. ASCII fallback already covers narrow terminals; same triggers when locale is non-UTF-8 (detect via `sys.stdout.encoding`). |
| `t.color.primary` undefined (custom theme) | Rich/Ink default to terminal foreground. No crash. |
| `t.brand.icon` undefined | New code doesn't read `t.brand`; old reference is deleted with `NOUS HERMES`. |

## 7. Security surface

- No user input is rendered as markup. Model name, CWD, session ID flow from typed dataclasses or SDK responses; both Rich and Ink escape `[...]` markup automatically.
- `_git_short_sha()` uses `subprocess.check_output(["git", "rev-parse", "--short=7", "HEAD"])` — argv list, no shell, no injection.
- No new subprocess calls. No network calls. No file writes.
- The Hermes string `"Nous Research · Messenger of the Digital Gods"` is removed (cosmetic, not a security issue, noted for completeness).

## 8. Testing strategy

### Python (pytest)

Replace test assertions that depend on the old HUD with new assertions:

- `test_build_welcome_banner_renders_oc_logo` — asserts `OPENCOMPUTER` appears (via half-block art or fallback); version present; footer present; no `MODEL`/`PROVIDER`/`CWD`/`SESSION` labels; no chip borders (`╭`, `╮`).
- `test_build_welcome_banner_narrow_terminal_fallback` — width=40, asserts plain `OPENCOMPUTER` text, no half-block art.
- `test_build_welcome_banner_omits_runtime_info` — passes `model`, `cwd`, `session_id`, `provider`; asserts none of those values appear in output.
- `test_build_welcome_banner_handles_missing_version` — monkeypatches `__version__ = ""`; asserts no `v · ` artifact.
- `test_build_welcome_banner_handles_missing_sha` — monkeypatches `_git_short_sha` → None; asserts no trailing ` · `.
- `test_build_welcome_banner_does_not_leak_hermes` — asserts none of `["Hermes", "Nous Research", "NOUS"]` appear (case-insensitive).
- All existing helper tests (`get_available_skills`, `get_available_tools`, `format_banner_version_label`, `OPENCOMPUTER_LOGO/_FALLBACK/SIDE_GLYPH`, Pico) preserved unchanged.

### TypeScript (vitest)

- `test('Banner renders OPENCOMPUTER, never NOUS HERMES')` — render `<Banner t={mockTheme} />`, assert output contains `OPEN` or `█`, never `NOUS` / `HERMES` / `Nous Research`.
- `test('SessionPanel renders version + footer')` — pass `info: { version: '2026.5.10', commit: 'abc1234' }`; assert version and `Ready` appear.
- `test('SessionPanel narrow-terminal fallback')` — cols=40; assert text-mode logo, no half-block characters.

### Manual smoke

- `cd OpenComputer && python -m opencomputer chat` → see new minimal splash.
- `cd OpenComputer/ui-tui && npm run dev` → see new minimal Ink splash.

## 9. YAGNI sweep

Explicitly **not** in scope for v1:

- Logo shimmer / pulse animation (OpenCode does this; deferred to a future v1.1).
- Mouse-interactive logo press → burst (OpenCode does this; deferred).
- Sound on logo press (OpenCode does this; **never** for us — not core).
- Background pulse art (`bg-pulse.tsx`; deferred).
- Migration from Ink to `@opentui/solid` (multi-month effort; out of scope).
- Statusline that's shared with Home AND Session in ui-tui — `StatusRule` already serves the Session view; adding it to Home is a follow-up.

## 10. Acceptance criteria

- `cd OpenComputer && pytest tests/test_cli_banner.py -v` is green.
- `cd OpenComputer/ui-tui && npm test` is green.
- `cd OpenComputer && ruff check opencomputer/ tests/` is clean.
- `cd OpenComputer/ui-tui && npm run type-check` is clean.
- `grep -r "NOUS HERMES\|Nous Research" OpenComputer/opencomputer/ OpenComputer/ui-tui/src/ OpenComputer/tests/ OpenComputer/ui-tui/src/__tests__/` returns nothing.
- Visual diff: opening `python -c "from rich.console import Console; from opencomputer.cli_banner import build_welcome_banner; build_welcome_banner(Console(), 'claude-opus-4-7', '/tmp', session_id='abc-123')"` shows the new minimal splash with logo + version-right + footer.
- The 4 Hermes lineage strings are removed: `"NOUS HERMES"`, `"Nous Research · Messenger of the Digital Gods"`, `"Hermes shell, OC backend"`, `"Visual register modeled after hermes-agent's banner.py"`.

## 11. Migration / rollback

- Single PR, single commit recommended (otherwise the half-finished `branding.tsx` blocks the build).
- Rollback = `git revert <commit>`. The two surfaces are independent (Python CLI and Ink TUI); a partial revert is mechanical.

## 12. Risks accepted

⚠️ **OpenCode's animations are not ported in v1.** The animated logo is a major part of OpenCode's perceived polish. Accepting this gap because (a) porting OpenTUI's sub-pixel renderer to Ink is multi-week work, (b) static art is what cli_banner.py / branding.tsx already render, and (c) follow-up shimmer is additive, not blocking.

⚠️ **First-impression info-density drops.** Users who relied on seeing model/cwd/session on the splash now look at the statusline (Ink TUI) or run `oc status` (Python CLI). Accepted because that's the OpenCode pattern by design.

⚠️ **OPENCOMPUTER_BLOCK_LOGO is 71 cols wide.** Below 73-col terminals, the splash falls back to text mode. ~3% of terminals are narrower than 73 cols (mostly older `xterm` defaults at 80). Accepted; fallback is graceful.
