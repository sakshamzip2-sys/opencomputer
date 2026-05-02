# Hermes-Style Onboarding — Foundation (F0+F1+F2)

**Date:** 2026-05-02
**Status:** Design — pending implementation
**Sub-project:** Foundation (1/N) of full Hermes-onboarding port
**Scope-level:** C (full Hermes parity, decomposed into ~30 sub-PRs; this is the first)

## 1. Context

User wants OpenComputer's first-run onboarding to look and feel **exactly** like Hermes Agent's setup wizard (screenshots in conversation; reference impl at `/Users/saksham/.hermes/hermes-agent/hermes_cli/`). Total Hermes wizard is ~5,000 LOC across `setup.py` (3,373), `curses_ui.py` (466), `banner.py` (635), `claw.py` (795). This is too large for a single PR; this spec covers **only the foundation that makes everything else possible**.

The user-visible win after this sub-project lands: typing `oc setup` enters a Hermes-style arrow-key wizard with the section banners + "skipped (keeping current)" pattern, and typing `oc chat` greets the user with the OPENCOMPUTER ASCII banner + categorized tools/skills listing. Provider gap-fill, channel gap-fill, migration, and remaining wizard sections land in subsequent sub-projects.

## 2. Goals

1. Provide arrow-key menu primitives (`radiolist`, `checklist`, `single_select`) — clean-room re-implementation on top of OC's existing `prompt_toolkit` dep, visually modeled on Hermes's curses UI but written from scratch (per § 10 O1 license decision).
2. Replace OC's procedural `setup_wizard.py` body with a section-driven orchestrator pattern that mirrors Hermes's `run_setup_wizard()`. Backbone for all future sections.
3. Replace OC's bare `OpenComputer v… session: …` chat preamble with a Hermes-style welcome banner: ASCII title, version label, side-panel face-art, categorized tools + skills listing, footer, welcome message + tip rotation.
4. Demonstrate the wizard end-to-end with **two live sections** (`inference-provider`, `messaging-platforms`) using OC's existing providers and channel adapters; remaining sections registered as deferred placeholders that print "(coming in <subproject>)".

## 3. Non-goals (explicit out-of-scope)

The following Hermes features land in **separate sub-projects** and are NOT touched by this PR:

- M1 — prior-install (OpenClaw / Hermes / OC) detection + import (port of `claw.py`)
- M2 — first-time-quick vs full-setup branching
- P1–P5 — provider plugin gap-fill (~25 missing providers)
- C1–C9 — channel adapter gap-fill (~10 missing platforms)
- S1 — agent-settings section
- S2 — TTS provider section
- S3 — terminal-backend section
- S4 — tools section
- S5 — launchd-service installer
- Q1 — reconfigure mode
- Q2 — non-interactive `--non-interactive` flag
- Q3 — setup summary + offer-launch-chat

## 4. Architecture

### 4.1 F0 — Arrow-key menu primitives

**File:** `opencomputer/cli_ui/menu.py` (new — clean-room implementation, NOT a port)

Built on `prompt_toolkit.application.Application` with custom `KeyBindings`. No use of stdlib `curses` (avoids the Windows-vs-stdlib-curses headache and matches OC's existing TUI choice — `opencomputer/cli_ui/` already houses `slash.py`, `resume_picker.py`, `slash_completer.py`, `_profile_swap.py`, `input_loop.py`, `keyboard_listener.py`, all prompt_toolkit-based). Visual register modeled on Hermes screenshots (yellow title, green selection arrow `→`, `(●)`/`(○)` radio glyphs, `[✓]`/`[ ]` checklist glyphs, `↑↓ navigate  ENTER/SPACE select  ESC cancel` hint footer).

Public API:

```python
def radiolist(
    question: str,
    choices: list[Choice],
    default: int = 0,
    description: str | None = None,
) -> int:
    """Single-select with arrow nav + (●)/(○) markers. Returns selected index."""

def checklist(
    title: str,
    items: list[Item],
    pre_selected: list[int] | None = None,
) -> list[int]:
    """Multi-select with [✓]/[ ] markers. Returns list of selected indices."""

def single_select(
    title: str,
    items: list[Item],
    default: int = 0,
) -> int:
    """Alternative single-select (different visual). Returns selected index."""

def flush_stdin() -> None:
    """Drain leftover keypresses before opening a prompt_toolkit Application."""
```

Internal: numbered-fallback functions (`_radio_numbered_fallback`, `_numbered_fallback`, `_numbered_single_fallback`) for non-TTY contexts. Detection via `sys.stdin.isatty()`. Fallbacks use `input()` with a numbered list — same UX as Hermes's fallback path.

**Cross-platform:** prompt_toolkit handles macOS / Linux / Windows uniformly (it's already a dep on every platform). No extras dependency needed. The numbered-fallback covers piped-stdin and CI contexts on every OS.

**Color/palette:** centralized in `opencomputer/cli_ui/style.py` (new, ~30 LOC). prompt_toolkit `Style` rules keyed by class names (`menu.title`, `menu.selected`, `menu.glyph.radio.on`, etc.). Re-skinning lives in this single file.

### 4.2 F1 — Wizard orchestrator

**Files:**
- `opencomputer/cli_setup/__init__.py` (new)
- `opencomputer/cli_setup/wizard.py` (new — orchestrator)
- `opencomputer/cli_setup/sections.py` (new — section registry)
- `opencomputer/cli_setup/section_handlers/inference_provider.py` (new)
- `opencomputer/cli_setup/section_handlers/messaging_platforms.py` (new)
- `opencomputer/cli_setup/section_handlers/_deferred.py` (new — placeholder for not-yet-ported sections)
- `opencomputer/setup_wizard.py` (existing — shrunk to a thin re-export so existing callers (`cli.py`, tests) keep working)

**Core abstraction:**

```python
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

class SectionResult(Enum):
    CONFIGURED = "configured"
    SKIPPED_KEEP = "skipped-keep"      # already configured, user chose keep
    SKIPPED_FRESH = "skipped-fresh"    # not configured, user chose skip
    CANCELLED = "cancelled"            # user pressed ESC

@dataclass
class WizardSection:
    key: str                           # e.g. "model_provider"
    icon: str                          # default "◆"
    title: str                         # e.g. "Inference Provider"
    description: str                   # multi-line, printed under title
    handler: Callable[["WizardCtx"], SectionResult]
    configured_check: Optional[Callable[["WizardCtx"], bool]] = None
    deferred: bool = False             # if True, prints "(coming in <subproject>)" stub

@dataclass
class WizardCtx:
    config: dict                       # loaded config.yaml dict (mutated in place)
    config_path: Path
    is_first_run: bool                 # True if no config existed before
    quick_mode: bool = False           # quick vs full setup (Q3 territory; default False)
```

**Orchestrator flow** (mirrors Hermes `run_setup_wizard`):
1. Print banner: `✦ OpenComputer Setup Wizard` (Rich box, magenta/pink to match Hermes pink).
2. Load existing config (or empty dict on first run).
3. For each section in registry order:
   - Print `<icon> <title>` header (cyan).
   - Print description.
   - If `deferred=True`: print "(coming in <subproject>)" and continue.
   - If `configured_check` returns True: prompt `radiolist` with 3 choices [Keep current / Reconfigure / Skip] using F0 primitives. Map to `SKIPPED_KEEP` / call handler / `SKIPPED_FRESH`.
   - Else: call handler directly.
   - Print result line (`✓ Configured` / `Skipped (keeping current)` / etc).
4. After all sections: write config back to disk.
5. Print `✓ Setup complete`. Offer to launch chat (Q3 stub for now — just print "Run `oc chat` to start.").

**Section registry order** (matches Hermes order; deferred markers indicate not-yet-ported):
```
[
  ("opencomputer_prior_detect", deferred=True, → M1),
  ("inference_provider", LIVE, handler=section_handlers.inference_provider),
  ("messaging_platforms", LIVE, handler=section_handlers.messaging_platforms),
  ("agent_settings", deferred=True, → S1),
  ("tts_provider", deferred=True, → S2),
  ("terminal_backend", deferred=True, → S3),
  ("tools", deferred=True, → S4),
  ("launchd_service", deferred=True, → S5),
]
```

**Section handler — `inference_provider` (LIVE in this PR):**
- Discover providers via existing `opencomputer.plugins.discovery` (returns plugin manifests with `setup.providers[*]`).
- Build choice list: one entry per provider plugin, formatted like Hermes (`(○) Anthropic (Claude models — API key or Claude Code)`). Description string from manifest.
- `radiolist` to pick. On selection: call provider plugin's setup hook (existing — see `setup_wizard.py:_prompt_api_key` flow). Update config's `model.provider` + `model.api_key_env`.
- Append "Configure auxiliary models..." and "Leave unchanged" entries at the end (mirrors Hermes).

**Section handler — `messaging_platforms` (LIVE in this PR):**
- Two-step Hermes flow:
  1. `radiolist` "Connect a messaging platform?" → [Set up now (recommended) / Skip]
  2. If "now": `checklist` "Select platforms to configure:" listing OC's installed channel-kind plugins (each with current configured-status label).
- For each toggled platform: print `◆ <Platform>` sub-header + description, call platform's setup hook (existing `_setup_telegram` etc, lifted into channel plugin manifests).
- Detect if Telegram allowlist is empty / home channel unset; print Hermes-style warnings (`⚠ No allowlist set`, `⚠ No home channel set`).

### 4.3 F2 — Welcome banner

**File:** `opencomputer/cli_banner.py` (new — clean-room implementation modeled on Hermes's banner.py)

ASCII art generated locally (figlet `slant` font, hand-tuned), version label assembled from OC's own `__version__` + git sha, tools/skills discovery via OC's existing `ToolRegistry` and `~/.opencomputer/skills/` walk. No code copied from Hermes.

```python
def build_welcome_banner(
    console: Console,
    model: str,
    cwd: str,
    *,
    session_id: str | None = None,
    home: Path | None = None,
) -> None:
    """Print the OPENCOMPUTER welcome banner with categorized tools/skills."""
```

**Visual sections:**
1. Big "OPENCOMPUTER" ASCII art (figlet-style, hand-tuned to match Hermes's font weight). Color: orange/yellow (Hermes uses orange for HERMES-AGENT — keep it).
2. Subtitle line: `OpenComputer v<__version__> (<date>) · upstream <git-sha>` (port `format_banner_version_label` from Hermes).
3. Side-panel face-art: hand-drawn ASCII glyph (Hermes uses a stylized profile face; OC's version will be visually distinct — a simple geometric/abstract glyph, ~12 lines tall, drawn from scratch). Below glyph: model name + provider + home dir + session ID.
4. Body — categorized tools/skills listing in two columns:
   - **Available Tools:** group by **plugin-of-origin** (the plugin that registered the tool, tracked in `ToolRegistry`). Built-in tools (Edit/Read/Write/Bash/etc.) group under `core`. Format: `<plugin>: <tool1>, <tool2>, …` (truncate with `…` if line > 80 chars). Note: Hermes uses a `toolset` attribute that OC tools don't have; plugin-of-origin is the equivalent natural grouping in OC's architecture.
   - **Available Skills:** walk skill discovery paths (`~/.opencomputer/skills/`, bundled `opencomputer/skills/`, plus per-profile path) for `SKILL.md` files. Group by parent directory name. Format: `<group>: <skill1>, <skill2>, …` (same truncation).
5. Footer: `<N> tools · <M> skills · /help for commands`.
6. Welcome message: `Welcome to OpenComputer! Type your message or /help for commands.`
7. Optional tip line: `random.choice` from a small list of OC-specific tips, picked once per banner render. Tips list lives at module top, ~6 entries to start. Each entry is grounded — references a real flag, env var, or slash command. Replace Hermes's `HERMES_*` env-var refs with `OPENCOMPUTER_*` equivalents (only those that actually exist in OC — verify each via grep before adding to the list).

**Helpers (new in OC):**
- `get_available_skills() -> dict[str, list[str]]` — skill discovery, walks `~/.opencomputer/skills/` + bundled `opencomputer/skills/` + per-profile `~/.opencomputer/profiles/<name>/skills/`. Groups by parent directory name.
- `format_banner_version_label() -> str` — `OpenComputer v<__version__> (<release-date>) · <git-sha>`. Reads `__version__` from `opencomputer/__init__.py`; git-sha via `git rev-parse --short HEAD` with try/except (skip git decoration if not in a repo).
- `prefetch_update_check()` / `get_update_result(timeout)` — already exist in OC's `cli_update_check.py`; reuse for the optional "(update available: vX.Y.Z)" line.
- `_format_context_length(tokens: int) -> str` — pretty-print model context window (e.g., `200K`, `1M`).

**Hook:** called from `opencomputer/cli.py::_run_chat_session` immediately after `_configure_logging_once()`, replacing the existing bare-line preamble (current code: bare `print()` of model name + session id).

## 5. Files

**New:**
- `opencomputer/cli_ui/__init__.py`
- `opencomputer/cli_ui/menu.py` (~350 LOC — prompt_toolkit-based primitives + numbered fallback)
- `opencomputer/cli_ui/style.py` (~40 LOC — Style rules)
- `opencomputer/cli_setup/__init__.py`
- `opencomputer/cli_setup/wizard.py` (orchestrator + `WizardCancelled` exception, ~280 LOC)
- `opencomputer/cli_setup/sections.py` (section registry + dataclasses, ~120 LOC)
- `opencomputer/cli_setup/section_handlers/__init__.py`
- `opencomputer/cli_setup/section_handlers/inference_provider.py` (~180 LOC)
- `opencomputer/cli_setup/section_handlers/messaging_platforms.py` (~220 LOC)
- `opencomputer/cli_setup/section_handlers/_deferred.py` (~40 LOC)
- `opencomputer/cli_banner.py` (~450 LOC — banner build + helpers)
- `opencomputer/cli_banner_art.py` (~80 LOC — ASCII art constants)
- `tests/test_cli_ui_menu.py` (~250 LOC)
- `tests/test_cli_setup_wizard.py` (~280 LOC)
- `tests/test_cli_setup_section_inference_provider.py` (~150 LOC)
- `tests/test_cli_setup_section_messaging_platforms.py` (~180 LOC)
- `tests/test_cli_banner.py` (~200 LOC)

**Modified:**
- `opencomputer/setup_wizard.py` — body shrinks to `from .cli_setup.wizard import run_setup` re-export. Public function name + signature unchanged for backward compat with `cli.py` callers + existing tests.
- `opencomputer/cli.py::_run_chat_session` — swap bare preamble for `cli_banner.build_welcome_banner(...)`.

**Estimated total LOC:** ~2,820 (1,760 prod + 1,060 tests). Note: total is ~10% smaller than the ported-version estimate because clean-room implementation on prompt_toolkit is more compact than curses-based code (no manual paint loops).

## 6. Testing strategy

TDD per `superpowers:test-driven-development`. Each new prod file paired with a test file. Write test first → confirm RED → implement → confirm GREEN.

**F0 menu primitives:**
- Numbered-fallback: pure I/O test, mock `sys.stdin` with StringIO, assert returned index.
- TTY happy path: prompt_toolkit's `create_pipe_input()` + `DummyOutput()` test harness — drive arrow keys via `pipe_input.send_text("\x1b[B\r")` etc. Pattern is documented in prompt_toolkit's own test suite; reuse it.
- Default-index respected when user immediately presses Enter.
- ESC raises `WizardCancelled` exception (defined in `cli_setup/wizard.py`). Decision: exception (not sentinel return) because it propagates cleanly through nested section handlers without each one having to check return values. Hermes uses `None` return; OC's choice diverges here for clarity.

**F1 wizard orchestrator:**
- Section iteration order: register 3 sections, call `run_setup`, assert handlers called in order.
- Deferred section: `deferred=True` skipped without invoking handler, prints expected stub line.
- Configured-check path: `configured_check=True` → 3-option radiolist appears (mock the menu primitive), each branch tested.
- Config persistence: handler mutates `ctx.config`, orchestrator writes to disk.
- ESC mid-section: `SectionResult.CANCELLED` halts orchestrator, returns to caller cleanly.

**F1 inference-provider section:**
- Empty plugin discovery → choice list still has "Custom endpoint" + "Leave unchanged".
- Selecting an `Anthropic`-style entry calls plugin's setup hook with mocked input.
- Selecting "Leave unchanged" leaves config untouched.

**F1 messaging-platforms section:**
- "Skip" branch: no platforms touched.
- "Set up now" → checklist returns 2 platforms → both setup hooks called in selection order.
- Telegram empty-allowlist warning printed when allowed_user_ids is empty after setup.

**F2 banner:**
- Snapshot test: full output rendered to a `StringIO`-backed `Console`, compared against fixture file. (Use Rich's `record=True` capture.)
- Tool grouping: register 3 fake tools with toolsets, assert grouped output.
- Skill discovery: tmp_path with 2 skills in different groups, assert grouped output.
- Truncation: tools list with 30 entries → output line ≤ 80 chars, ends with `…`.
- No-tools / no-skills graceful empty state.

**Integration:**
- `test_setup_wizard_end_to_end`: drive the full wizard with mocked menu primitives + mocked plugin setup hooks; assert config.yaml written with expected provider + selected platforms; assert deferred sections logged stub line.

## 7. Migration & backward compat

- `opencomputer.setup_wizard.run_setup` keeps its current signature (no kwargs added). Existing callers (`cli.py:_offer_setup_or_exit`, `tests/test_cli_first_run_offer.py`, etc.) continue working unchanged.
- Existing tests that mocked `input()` need updating to mock `cli_ui.menu.radiolist` / `checklist` instead. Identified via grep; will be fixed in the same PR.
- Config schema unchanged — orchestrator reads/writes the same `config.yaml` shape.
- For users with an existing wizard run (config already populated), the `configured_check` path triggers — they get the keep/reconfigure/skip prompt instead of being re-prompted from scratch. Net UX improvement.

## 8. Success criteria

The PR merges to `main` when **all** of these hold:

1. `oc setup` invocation produces the Hermes-style flow: ASCII section banner → arrow-key provider menu → radiolist messaging-platforms gate → multi-select platform list → per-platform setup → deferred-section stubs → "✓ Setup complete" footer.
2. `oc chat` invocation prints the new welcome banner: OPENCOMPUTER ASCII art + version label + face panel + categorized tools/skills + footer + welcome message + tip.
3. All new tests pass; full pytest suite passes (no regressions vs `main` baseline of 6,615 passed).
4. `ruff check` clean.
5. Manual smoke on macOS: `oc setup` → fresh config → `oc chat` → banner renders correctly. Manual smoke on Linux (CI's existing Linux job suffices).
6. Windows / Linux / macOS pytest jobs all green (prompt_toolkit handles all three uniformly; no platform-specific xfails expected).

## 9. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Visual fidelity to Hermes screenshots <100% (clean-room re-impl, not pixel-port) | Med | The user said "exactly like" referring to the *flow* + *layout* — stylistic deviation is invisible. Track perceived gap during dogfood; if material, follow-up PR tunes specific glyphs/colors. |
| prompt_toolkit Application clashes with OC's existing chat REPL prompt session | Low | Wizard runs *before* chat REPL is mounted; primitive uses single-shot `Application.run()` per call (not a persistent session). Verified by reading `cli_repl/` module structure. |
| Banner skill discovery paths conflict with active profile | Low | Walk all 3 sources (~/.opencomputer/skills, bundled, profile path) and dedupe by skill key. |
| Tools-listing reads `Tool.toolset` attr that doesn't exist on every tool | Med | Fall back to `(uncategorized)` group; verify via grep that all built-in tools have a toolset OR plan a follow-up to add them. |
| Spec sections referenced as `M1/S1-S5/Q1-Q3` get out of sync with future PRs | Low | Cross-link from each follow-up PR description back to this spec; nothing here hard-codes those names. |
| Existing setup_wizard tests break | High | Same-PR update of mocked `input()` → mocked menu primitives; no signature changes to the public entry point. |

## 10. Open questions

**O1 — License compatibility (BLOCKING).** Hermes Agent ships under GPL-3.0-or-later (verify at port time via `LICENSE` at hermes-agent root). OpenComputer is currently MIT (`/Users/saksham/Vscode/claude/opencomputer/LICENSE`). Direct ports of GPL code into an MIT codebase are not legal without re-licensing the whole work GPL. Three resolutions:
  1. **Re-license OC as GPL-3.0-or-later** — simplest, but affects every downstream consumer.
  2. **License-segregate the ported code** under `extensions/hermes-onboarding/` with its own GPL `LICENSE` file; keep main package MIT. Verified-clean boundary: ported code may not be imported by main-package code; main-package only invokes via plugin discovery hooks.
  3. **Re-implement from scratch** on top of OC's existing `prompt_toolkit` + `rich` deps (clean-room). Adds ~5 days to F0 timeline; no GPL contamination. Visual fidelity to Hermes screenshots ~95% (some pixel-pixel deviation in escape-sequence ordering / ASCII-art glyphs).

  **Recommendation: pick (3) — clean-room re-implementation.** Rationale: keeps OC's MIT license intact, avoids segregation complexity, and the visual deviation from Hermes is invisible to a typical user (the *flow* and *layout* are what they asked to match, not exact escape-sequence parity). Code stays maintainable by us long-term.

  This must be resolved before code lands. The implementation-plan that follows this spec assumes resolution (3) unless overridden.

## 11. Cross-references

- Reference impl: `/Users/saksham/.hermes/hermes-agent/hermes_cli/{setup.py,curses_ui.py,banner.py,claw.py}`
- OC code touched today (PR #288): `opencomputer/security/env_loader.py` — env-loader profile-leaf fix; **not in this spec's scope** but landed in parallel because it was blocking dogfood.
- Decomposition for the remaining ~29 sub-PRs lives in this conversation; pull into a roadmap doc when starting M1.

## 12. Attribution

Per resolution of **§ 10 O1 (clean-room recommended)**, no GPL code is copied. Each new module includes a top-of-file comment crediting Hermes Agent for visual/UX inspiration:

```python
"""<module purpose>.

Visual + UX modeled after hermes-agent's hermes_cli/<source>.py.
Independently re-implemented on prompt_toolkit + rich (no code copied).
"""
```

If § 10 O1 is overridden to (1) or (2), update each new file's header with the GPL attribution block and add a `LICENSE` file in the segregated subtree.
