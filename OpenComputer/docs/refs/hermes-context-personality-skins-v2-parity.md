# Hermes Context / Personality / Skins (v2) — OpenComputer Parity Status

**Spec:** `~/Downloads/files (1)/hermes-context-personality-skins-v2.md` (2026-05-08 reference)
**Last reviewed:** 2026-05-08 (production-grade closure — all 8 deferrals shipped)

This page maps each section of the Hermes v2 reference to OpenComputer's
implementation. Use it when porting future Hermes features so you can
see what's already shipped vs. what's a real delta and skip redundant
work.

**Status:** All deferrals from PR #510 / #512 closed in this PR.

## Context Files

| Hermes feature | OC status | Where |
|---|---|---|
| Hierarchical priority `.hermes.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules` | ✅ shipped — full priority chain | `prompt_builder.load_workspace_context` checks `OPENCOMPUTER.md`, `.hermes.md`, `CLAUDE.md`, `AGENTS.md`, `.cursorrules` (D3 added `.hermes.md`) |
| `SOUL.md` always-loaded slot #1 | ✅ shipped (D8) — SOUL now renders at the top of `base.j2` as Slot 1 per Hermes spec | `agent/prompts/base.j2:14-22`, `prompt_builder.PromptContext.soul` |
| `SOUL.md` per-profile + global fallback | ✅ shipped (D4) — per-profile `<home>/<profile>/SOUL.md` wins; falls back to `$OPENCOMPUTER_HOME/SOUL.md` (HERMES_HOME parity) when missing/empty | `agent/memory.MemoryManager.read_soul` + `global_soul_path` |
| `SOUL.md` empty/whitespace → fall back to built-in default | ✅ shipped — whitespace-only contents return `""` so the j2 omits the section and the built-in identity preamble carries the role | `agent/memory.MemoryManager.read_soul` |
| Progressive subdirectory discovery (5 ancestors, 8KB cap, dedupe) | ✅ shipped — also recognizes `.hermes.md` / `HERMES.md` in subdirs (D3) | `subdirectory_hints.SubdirectoryHintTracker` |
| Per-file 100KB cap with head/tail/marker truncation (70/20/10) | ✅ shipped (D1) — head 70K + tail 20K + marker preserves both intro and closing-section conventions | `prompt_builder._truncate_head_tail` + `_format_truncation_note` |
| Truncation marker shape `[...truncated NAME: kept Nh+Nt of N chars. Use file tools to read the full file.]` | ✅ shipped — Hermes spec example matches verbatim | `prompt_builder._format_truncation_note` |
| Security scan: instruction-override / hidden HTML / credentials / secrets | ✅ shipped — same shared scanner for startup + subdir hints | `opencomputer.security.context_scan.scan_workspace_context_content` |
| `[BLOCKED:]` rejection | ⚠️ different — OC quarantines instead of blocking | `<quarantined-untrusted-content>` envelope wraps poisoned content rather than discarding it; the agent sees what was attempted but is told it's untrusted, which preserves the audit trail and avoids silently dropping content |

## `@`-References

| Hermes feature | OC status | Where |
|---|---|---|
| `@file:` `@folder:` `@diff` `@staged` `@git:N` `@url:` | ✅ shipped | `opencomputer.agent.at_references.expand` |
| Soft 25% / hard 50% caps | ✅ shipped | `AtRefContext.soft_cap` / `hard_cap` |
| Folder 200-entry cap, git 1-10 clamp | ✅ shipped | `_FOLDER_MAX_ENTRIES`, `_GIT_MAX_COMMITS` |
| Blocked sensitive paths (`.ssh/`, `.aws/`, `.gnupg/`, `.kube/`, `.netrc`, `.pgpass`, full shell-profile set incl. `.zprofile` / `.zlogin` / `.zshenv` / `.bash_login`, key globs) | ✅ shipped | `at_references.is_path_blocked` |
| Path-traversal protection — references outside workspace root rejected | ✅ shipped | `at_references._is_outside_workspace` |
| Binary file detection — text-extension allowlist bypasses null-byte sniff (Hermes spec literal); binary-extension allowlist short-circuits without I/O; null-byte sniff for unknown extensions | ✅ shipped (D2) | `at_references._looks_binary`, `_TEXT_EXTENSIONS`, `_BINARY_EXTENSIONS` |
| Trailing-punctuation strip | ✅ shipped | `_TRAILING_PUNCT` |
| CLI tab completion | ✅ shipped | `cli_ui/file_completer.py`, `slash_completer.py` |
| Channel-adapter NOT-expanded policy | ✅ shipped | CLI input loop calls `expand`; channel adapters do not |

## Personality

| Hermes feature | OC status | Where |
|---|---|---|
| 14 built-in personalities (helpful, concise, technical, creative, teacher, kawaii, catgirl, pirate, shakespeare, surfer, noir, uwu, philosopher, hype) | ✅ shipped | `opencomputer.agent.personality.builtins.BUILTINS` |
| Custom personalities via `agent.personalities` config | ✅ shipped | `personality.loader.resolve` reads custom dict |
| `/personality` (show), `/personality NAME` (set), `/personality reset` | ✅ shipped | `slash_commands_impl.skin_personality_cmd.PersonalityCommand` |
| SOUL.md-as-baseline + `/personality`-as-overlay layering | ✅ shipped — SOUL is Slot 1 (top), `/personality` is Slot 7 (after timestamp) | `agent/prompts/base.j2` |
| Prompt stack order (SOUL → tool guidance → memory → skills → context-files → timestamp → /personality) | ✅ shipped (D8) — base.j2 reorganized to canonical Hermes order; 9 tests pin every slot boundary | `agent/prompts/base.j2`, `tests/test_prompt_slot_order.py` |

## Skins / Themes

| Hermes feature | OC status | Where |
|---|---|---|
| 9 built-in skins (default / ares / mono / slate / daylight / warm-lightmode / poseidon / sisyphus / charizard) | ✅ shipped | `opencomputer/cli_ui/skin/builtins/*.yaml` |
| `/skin` (show), `/skin NAME` (set) | ✅ shipped | `slash_commands_impl.skin_personality_cmd.SkinCommand` |
| Custom skins at `~/.opencomputer/skins/*.yaml` | ✅ shipped | `skin.loader.USER_SKINS_DIR` |
| Per-key inheritance from `default.yaml` | ✅ shipped | `skin.loader._merge_with_default` |
| `tool_emojis`, `banner_logo`, `banner_hero`, `tool_prefix` | ✅ shipped | `SkinSpec` fields |
| Spinner `wings` + `thinking_verbs` | ✅ shipped | `SkinSpec.spinner_wings`, `spinner_thinking_verbs` |
| Spinner `waiting_faces` + `thinking_faces` | ✅ shipped (D5 — data + renderer wiring) — every built-in defines both cycles; the streaming spinner uses `_skin_spinner_text(phase=...)` to pick `waiting_faces[0]` during the network round-trip and `thinking_faces[0]` once first reasoning content arrives | `SkinSpec.spinner_waiting_faces` / `_thinking_faces`, `cli_ui/skin/apply.py`, `cli_ui/streaming.py:_skin_spinner_text` (renderer consumer) |
| 22-key Hermes color palette (`response_border`, `session_label`, `voice_status_bg`, `selection_bg`, `completion_menu_*`, `banner_dim`, `banner_text`, `ui_accent`, `ui_label`, `ui_ok`, `ui_error`, `ui_warn`, `input_rule`, `session_border`) | ✅ shipped (D6 — data + renderer wiring) — every built-in skin defines all 22 keys; renderers consume them via `_skin_color()` (Rich panels — thinking/tool panel borders) and `_menu_dict_from_skin()` (prompt-toolkit completion menu); OC's legacy keys coexist for backward compat | `cli_ui/skin/builtins/*.yaml`, `cli_ui/streaming.py:_skin_color`, `cli_ui/style.py:_menu_dict_from_skin` |
| Live TUI repaint on `/skin` | ✅ shipped (D7 — full pipeline) — CLI puts live `Console` under `runtime.custom["live_console"]`; SkinCommand pushes the Rich theme onto it; PromptSession uses `DynamicStyle(current_menu_style)` so the completion menu re-resolves on every render and picks up the new skin without rebuilding the session | `slash_commands_impl/skin_personality_cmd.py:_apply_skin_with_live_console`, `cli_ui/input_loop.py` (DynamicStyle wiring) |

## Out of scope (not Hermes-equivalent by design)

- **Hermes Mod web UI** — community external tool, not a port target. OC's `~/.opencomputer/skins/<name>.yaml` is the equivalent surface (drop a YAML, set `display.skin` in `config.yaml`).

## Maintenance

When adding a new Hermes feature, update this table. When the upstream
Hermes spec changes, drop the new spec next to this file and re-diff.

The shared scanner at `opencomputer.security.context_scan` is the
single source of truth for context-file safety policy. When the
detector or redactor evolves, both startup loading and subdirectory
hint discovery pick up the change automatically.

The base.j2 slot order is pinned by `tests/test_prompt_slot_order.py`
and the skin parity is pinned by `tests/test_skin_spinner_faces.py` —
future template / skin edits that drift from the Hermes spec will
fail those tests loudly.
