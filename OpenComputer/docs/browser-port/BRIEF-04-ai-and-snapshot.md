# BRIEF — `snapshot/` (Wave W1b)

> Role-snapshot pipeline + Chrome MCP subprocess (for `existing-session` profile) + screenshot compression.
> Deep dive: [04-ai-and-snapshot.md](../refs/openclaw/browser/04-ai-and-snapshot.md) (908 lines — read end-to-end).

## What to build

`extensions/browser-control/snapshot/`:

| File | Public API |
|---|---|
| `role_snapshot.py` | `def build_role_snapshot_from_aria_snapshot(aria_text: str) -> SnapshotResult` (parses Playwright's text aria tree, assigns refs, dedups) · `class RoleNameTracker` |
| `snapshot_roles.py` | `INTERACTIVE_ROLES: frozenset[str]` (17) · `CONTENT_ROLES: frozenset[str]` (10) · `STRUCTURAL_ROLES: frozenset[str]` (19) — port the 46-role enumeration verbatim from OpenClaw |
| `chrome_mcp.py` | `async def spawn_chrome_mcp(*, executable: str = "npx", args: list[str] = DEFAULT) -> ChromeMcpClient` · `class ChromeMcpClient` (uses official `mcp` Python SDK, NOT hand-rolled JSON-RPC) — wraps the 18 MCP tools (`take_snapshot`, `take_screenshot`, `click`, `fill`, etc.) |
| `chrome_mcp_snapshot.py` | `def build_ai_snapshot_from_chrome_mcp_snapshot(tree: ChromeMcpSnapshotNode, *, interactive: bool, compact: bool, max_depth: int \| None) -> SnapshotResult` |
| `screenshot.py` | `async def normalize_screenshot(image_bytes: bytes, *, max_side: int = 2000, max_bytes: int = 5_000_000, type: Literal["png", "jpeg"] = "png") -> bytes` — implements the 7-side × 6-quality compression grid |

## What to read first

1. The deep dive's "Chrome MCP protocol deep dive" — confirms it uses official `@modelcontextprotocol/sdk` (not hand-rolled). Lists all 18 wrapped tools.
2. Pseudocode for all 3 snapshot pipelines + the unification step that lands every path at `{snapshot_text, refs}`.
3. The worked dedup walkthrough showing `RoleNameTracker` state for a 3-element example.
4. The verbatim role enumerations (46 roles total) — port as `frozenset` constants.
5. The screenshot 7-side × 6-quality grid algorithm.

## Acceptance

- [ ] `build_role_snapshot_from_aria_snapshot` produces stable `e1`, `e2`, … refs that survive page rerenders (test: two snapshots of same page produce same refs for same elements)
- [ ] Dedup correctly: two `button "OK"` get `[nth=0]`, `[nth=1]`; a unique `link "Home"` gets no `[nth=...]`
- [ ] All 46 role constants present; matches OpenClaw exhaustively
- [ ] `spawn_chrome_mcp` boots `npx chrome-devtools-mcp@latest --autoConnect --experimentalStructuredContent` as a subprocess and exchanges `initialize` / `tools/list` over MCP
- [ ] Chrome MCP session caching: per `(profile_name, user_data_dir)` pair; reconnects on transport failure; tool-level errors don't tear down the session
- [ ] `take_snapshot` / `take_screenshot` calls return parsed `ChromeMcpSnapshotNode` trees / image bytes
- [ ] `build_ai_snapshot_from_chrome_mcp_snapshot` walks the tree, lowercases roles, defaults missing roles to `"generic"`, applies `interactive`/`compact`/`max_depth` filters, returns the unified shape
- [ ] Element-targeted Chrome MCP screenshots: ref → uid is **identity** (no extra mapping needed; documented in deep dive)
- [ ] `normalize_screenshot` iterates the 7-side × 6-quality grid; picks smallest variant under limit; raises if no variant fits
- [ ] Tests in `tests/test_snapshot_*.py` covering all three pipelines (mock Playwright `aria_snapshot()` output for path 2; mock MCP responses for path 3; real Pillow for screenshots)
- [ ] No imports from `opencomputer/*`

## Do NOT reproduce

| OpenClaw choice | Don't do |
|---|---|
| Path 1 (`page._snapshotForAI`) | Skip for v0.1. playwright-python doesn't officially expose it. Ship Path 2 (`aria_snapshot()`) only; revisit Path 1 if/when the underscore API surfaces in Python. |
| Hand-rolled JSON-RPC for Chrome MCP | Don't. Use the official `mcp` Python SDK (`pip install mcp`). |

## Implementation gotchas

- **`npx` requires Node ≥ 18 on PATH.** If Node isn't installed, `spawn_chrome_mcp` should raise a typed error with a hint pointing at `opencomputer doctor`. `existing-session` profile then becomes unavailable until Node lands.
- **Vendor vs npx**: OpenClaw uses npx at runtime. Open question: do we vendor `chrome-devtools-mcp` (gives us a known-good version, but adds ~10MB)? Default: npx for v0.1; revisit if version churn becomes a problem.
- **Image ops**: `Pillow` is stdlib-adjacent; `Image.thumbnail` for resize, `Image.save(buf, "JPEG", quality=q)` for compression.
- **MCP tool error vs transport error**: the deep dive documents the distinction — tool errors keep session alive; transport errors tear it down. Replicate this state machine.
- **`pw-ai-module.ts`'s soft-loader pattern** is interesting but not load-bearing in Python. We can lazy-import directly with a try/except; no need to mirror the dynamic-loader complexity.

## Open questions

- Snapshot mode `"role"` only for v0.1 (skip `"aria"` mode entirely until path 1 is unlocked)? Recommend yes — already locked in BLUEPRINT.
- Should `chrome_mcp.py` boot the subprocess on first use, or eagerly when the profile activates? Recommend lazy on first use — saves boot time when the user's mostly using the `openclaw` profile.

## Where to ask

PR description with `**Question:**` line.
