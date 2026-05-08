# Hermes Context / Personality / Skins (v2) — OpenComputer Parity Status

**Spec:** `~/Downloads/files (1)/hermes-context-personality-skins-v2.md` (2026-05-08 reference)
**Last reviewed:** 2026-05-08

This page maps each section of the Hermes v2 reference to OpenComputer's
implementation. Use it when porting future Hermes features so you can
see what's already shipped vs. what's a real delta and skip redundant
work.

## Context Files

| Hermes feature | OC status | Where |
|---|---|---|
| Hierarchical priority `.hermes.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules` | ✅ shipped | `prompt_builder.load_workspace_context` checks `OPENCOMPUTER.md`, `CLAUDE.md`, `AGENTS.md`, `.cursorrules` (PR #500 + this PR for `.cursorrules`) |
| `SOUL.md` always-loaded slot #1 | ✅ shipped (per-profile, not `HERMES_HOME`) | `profiles._maybe_write_soul_md`, `prompt_builder.PromptContext.soul` (PR #24, Sub-project C) |
| Progressive subdirectory discovery (5 ancestors, 8KB cap, dedupe) | ✅ shipped | `subdirectory_hints.SubdirectoryHintTracker` |
| Per-file 100KB cap | ✅ shipped | `prompt_builder._WORKSPACE_FILE_CAP_BYTES` |
| Informative truncation marker (kept / total / hint to use file tools) | ✅ shipped (this PR) | `prompt_builder._format_truncation_note` |
| Security scan: instruction-override / hidden HTML / credentials / secrets | ✅ shipped (this PR for subdir hints; startup already had it) | `opencomputer.security.context_scan.scan_workspace_context_content` shared by startup + subdir-hint pipelines |
| `[BLOCKED:]` rejection | ⚠️ different — OC quarantines instead of blocking | `<quarantined-untrusted-content>` envelope wraps poisoned content rather than discarding it; the agent sees what was attempted but is told it's untrusted, which preserves the audit trail and avoids silently dropping content |

## `@`-References

| Hermes feature | OC status | Where |
|---|---|---|
| `@file:` `@folder:` `@diff` `@staged` `@git:N` `@url:` | ✅ shipped | `opencomputer.agent.at_references.expand` |
| Soft 25% / hard 50% caps | ✅ shipped | `AtRefContext.soft_cap` / `hard_cap` |
| Folder 200-entry cap, git 1-10 clamp | ✅ shipped | `_FOLDER_MAX_ENTRIES`, `_GIT_MAX_COMMITS` |
| Blocked sensitive paths (`.ssh/`, `.aws/`, `*.pem`, etc.) | ✅ shipped | `at_references.is_path_blocked` |
| Path-traversal protection | ✅ shipped | `Path.resolve` before block check |
| Trailing-punctuation strip | ✅ shipped | `_TRAILING_PUNCT` |
| CLI tab completion | ✅ shipped | provided by prompt-toolkit input loop |
| Channel-adapter NOT-expanded policy | ✅ shipped | CLI input loop calls `expand`; channel adapters do not |

## Personality

| Hermes feature | OC status | Where |
|---|---|---|
| 14 built-in personalities (helpful, concise, technical, creative, teacher, kawaii, catgirl, pirate, shakespeare, surfer, noir, uwu, philosopher, hype) | ✅ shipped | `opencomputer.agent.personality.builtins.BUILTINS` |
| Custom personalities via `agent.personalities` config | ✅ shipped | `personality.loader.resolve` reads custom dict |
| `/personality` (show), `/personality NAME` (set), `/personality reset` | ✅ shipped | `slash_commands_impl.skin_personality_cmd.PersonalityCommand` |
| SOUL.md-as-baseline + `/personality`-as-overlay layering | ✅ shipped | SOUL is slot #1 (`PromptContext.soul`); /personality is slot #7 |
| Prompt stack order (SOUL → tool guidance → memory → skills → context-files → timestamp → /personality) | ✅ shipped | `agent/prompts/base.j2` |

## Skins / Themes

| Hermes feature | OC status | Where |
|---|---|---|
| 9 built-in skins (default / ares / mono / slate / daylight / warm-lightmode / poseidon / sisyphus / charizard) | ✅ shipped | `opencomputer/cli_ui/skin/builtins/*.yaml` |
| `/skin` (show), `/skin NAME` (set) | ✅ shipped | `slash_commands_impl.skin_personality_cmd.SkinCommand` |
| Custom skins at `~/.opencomputer/skins/*.yaml` | ✅ shipped | `skin.loader.USER_SKINS_DIR` |
| Per-key inheritance from `default.yaml` | ✅ shipped | `skin.loader._merge_with_default` |
| `tool_emojis`, `banner_logo`, `banner_hero`, `tool_prefix` | ✅ shipped | `SkinSpec` fields |
| Spinner `wings` + `thinking_verbs` | ✅ shipped | `SkinSpec.spinner_wings`, `spinner_thinking_verbs` |
| Spinner `waiting_faces` + `thinking_faces` | ❌ not shipped — YAGNI | No current renderer site for animated faces. Add when an animated-face renderer exists. |
| 24-key Hermes color palette (`response_border`, `session_label`, `voice_status_bg`, `selection_bg`, completion-menu keys, ...) | ⚠️ partial — current 15 keys cover OC's render surfaces; remaining ~9 keys are decorative until renderer sites appear | `cli_ui/skin/builtins/default.yaml` |
| Live TUI repaint on `/skin` | ⚠️ partial — spinner + branding hot-swap; full color re-theme requires session restart | Documented in `skin_personality_cmd.SkinCommand.execute` |

## Out of scope (not Hermes-equivalent by design)

- **HERMES_HOME single-global SOUL.md** — OC is profile-first; SOUL is per-profile under `~/.opencomputer/<profile>/SOUL.md` (Sub-project C, PR #24). The per-profile design is a deliberate divergence so users can carry different identities across project contexts.
- **`.hermes.md` priority** — OC's project-context name is `OPENCOMPUTER.md`. `.hermes.md` is not in OC's discovery list.
- **Hermes Mod web UI** — community external tool, not a port target.

## Maintenance

When adding a new Hermes feature, update this table. When the upstream
Hermes spec changes, drop the new spec next to this file and re-diff.

The shared scanner at `opencomputer.security.context_scan` is the
single source of truth for context-file safety policy. When the
detector or redactor evolves, both startup loading and subdirectory
hint discovery pick up the change automatically.
