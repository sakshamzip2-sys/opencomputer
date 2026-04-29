# Phase 0 Decisions — Skills Hub MVP (Tier 1.A)

**Branch:** `feat/skills-hub`
**Plan:** `docs/superpowers/plans/2026-04-28-hermes-tier1a-skills-hub.md`
**Date:** 2026-04-28
**Status:** Phase 0 complete; Phase 1 ready to start.

This document captures the answers to the six Phase 0 pre-flight verification tasks (0.1–0.6 in the plan). The plan was written speculatively; some assumptions turned out wrong. This file records the verified state so the rest of execution can proceed without speculation.

---

## D-0.1 — Branch (verified)

`feat/skills-hub` created from `main` at `8b44837e` (the channel-port spec commit). Untracked docs (gap audit + plan) committed as `02e6db67`.

Parallel session note: `feat/hermes-channel-feature-port` is mid-flight elsewhere implementing the channel-port spec (4 commits at Tier 1: channel_helpers, channel_utils, network_utils, format_converters). **We do not touch that branch.** Skills Hub work stays isolated on `feat/skills-hub`.

---

## D-0.2 — Skill loader path (REQUIRES PLAN CHANGE)

**Verified at `opencomputer/agent/memory.py:457-502`:**

```python
def list_skills(self) -> list[SkillMeta]:
    roots = [self.skills_path, *self.bundled_skills_paths]
    seen_ids: set[str] = set()
    out: list[SkillMeta] = []
    for root in roots:
        if not root.exists():
            continue
        for skill_dir in root.iterdir():    # ← ONE LEVEL ONLY
            if not skill_dir.is_dir() or skill_dir.name in seen_ids:
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            ...
```

**Finding:** the loader walks **one level only** (`root.iterdir()`, not `root.rglob("**/SKILL.md")`). If we install to `~/.opencomputer/<profile>/skills/.hub/well-known/foo/SKILL.md`, that file is at depth 3 from the user-skills root and **will not be discovered by the agent**.

**Plan-correcting fix (lock-stepping with Task 1 of Phase 1):** modify `MemoryManager.list_skills()` to additionally walk `<skills_path>/.hub/<source>/<skill-name>/SKILL.md`. The cleanest patch:

```python
roots = [self.skills_path, *self.bundled_skills_paths]
hub_root = self.skills_path / ".hub"
if hub_root.is_dir():
    for source_dir in hub_root.iterdir():
        if source_dir.is_dir():
            roots.append(source_dir)   # treats <hub>/<source>/ as just another root
```

This keeps the existing one-level-walk semantics and adds the hub source dirs as additional roots. **No risk to existing skill discovery.** Add a test that proves a hub-installed skill appears in `list_skills()`.

**Migration impact:** zero — non-`.hub/` paths behave identically.

---

## D-0.3 — Skills Guard public API (REQUIRES PLAN CHANGE)

**Verified at `opencomputer/skills_guard/__init__.py` and `scanner.py`:**

Public exports:
- `Finding(pattern_id, severity, category, file, line, match, description)` — `severity ∈ {"critical", "high", "medium", "low"}`
- `ScanResult(skill_name, source, trust_level, verdict, findings, scanned_at, summary)` — `verdict ∈ {"safe", "caution", "dangerous"}`
- `scan_file(file_path: Path, rel_path: str = "") -> list[Finding]`
- **`scan_skill(skill_path: Path, source: str = "community") -> ScanResult`** — operates on a directory path, not text
- `should_allow_install(result, ...) -> PolicyDecision`
- `format_scan_report(result) -> str`
- `resolve_trust_level(source) -> str`
- `content_hash(skill_path) -> str`

**Finding:** the plan assumed `scan(text) -> {.severity, .findings}`. **Wrong.** Real API takes a `Path` and returns `ScanResult` with `verdict` (not `severity`). `Finding` has its own `severity` per pattern hit.

**Plan-correcting fix:** Installer must:
1. Write the skill bundle to a **staging directory** first (e.g., `<hub_root>/_staging/<source>/<name>/`).
2. Call `scan_skill(staging_dir, source=meta.source)` → `ScanResult`.
3. Run the result through `should_allow_install(result, ...)` → `PolicyDecision`.
4. If `decision.allow == False`: `shutil.rmtree(staging_dir)`, audit `scan_blocked` with the decision reason, raise `InstallError`.
5. Else: atomically move staging dir to final location, update lockfile + audit.

This is **strictly better** than the plan's original wiring because:
- It uses the existing `should_allow_install` policy gate (same one used by `oc skill scan` already).
- Atomic install is now natural (staging → final move).
- Verdict thresholds align with existing OC convention.

**Plan task adjustments:**
- Task 3.1 test fixtures: replace `Mock(severity=..., findings=...)` with `Mock(verdict=..., findings=...)` and a `should_allow_install` mock returning a `PolicyDecision(allow=bool, reason=str)`.
- Installer signature: takes `skills_guard` parameter as a *module-like* object exposing `scan_skill` and `should_allow_install`. Tests can pass a stub.
- Audit log fields: `verdict` (not `guard_severity`), `decision_reason` on blocks.

---

## D-0.4 — Slash dispatcher shape (REQUIRES PLAN CHANGE)

**Verified at `opencomputer/agent/slash_dispatcher.py`:**

```python
async def dispatch(
    message: str,
    slash_commands: dict[str, Any],
    runtime: RuntimeContext,
) -> SlashCommandResult | None:
```

Slash commands are a `dict[str, Any]` where each value has an `.execute(args, runtime)` method. The dict is **owned by the agent loop** and registered there — `slash_dispatcher.py` itself doesn't hold a registry.

**Finding:** my plan assumed a module-level `SLASH_HANDLERS` registry. **Wrong.** Commands are objects with `.execute()`, registered into a dict that the agent loop passes to `dispatch()`. Looking at v6f patterns (referenced in the dispatcher docstring), this likely means slash commands are registered via `plugin_sdk.SlashCommand` subclasses or duck-typed objects assembled by the agent loop's setup code.

**Plan-correcting fix:** Task 4.3 should:
1. Find where the `slash_commands` dict is built (likely in `cli.py` or `agent/loop.py`). Grep for `slash_commands` or `SlashCommand`.
2. Add a `SkillsHubSlashCommand` class implementing the duck-typed `{name, description, execute}` shape (or `plugin_sdk.SlashCommand` subclass if it exists).
3. `execute(args, runtime)` parses `args.split(None, 1)` and dispatches to the shared `do_*` functions in `cli_skills_hub.py`. Returns a `SlashCommandResult(output=..., handled=True)`.
4. Register into the slash_commands dict at the same callsite that registers other slash commands.

**Plan task adjustments:**
- Task 4.3 file `slash_commands_impl/skills_hub_slash.py` becomes a `SkillsHubSlashCommand` *class* not just a function.
- Slash test in `test_cli.py` needs a fake `runtime` (`RuntimeContext`) to be passed through.
- The registration callsite is plan-finding work for Phase 4.

---

## D-0.5 — Active-profile resolver (verified)

**Verified at `opencomputer/profiles.py`:**

- `read_active_profile() -> str | None` (line 239) — reads `~/.opencomputer/active_profile`. Returns `None` for default.
- `get_profile_dir(name: str | None) -> Path` (line 80) — returns `~/.opencomputer/<name>/`. `None` resolves to `default`.
- `profile_home_dir(name: str) -> Path` (line 92) — returns `~/.opencomputer/<name>/home/` (the workspace-style HOME root).

**For Skills Hub use:** want the profile *config* directory (not the `home/` subdir). That's `get_profile_dir(read_active_profile())`.

**Plan-correcting fix:** `_profile_home()` in `cli_skills_hub.py`:

```python
from opencomputer.profiles import get_profile_dir, read_active_profile

def _profile_home() -> Path:
    """Resolve the active profile's directory."""
    return get_profile_dir(read_active_profile())
```

No special-casing of `OPENCOMPUTER_HOME` env var needed in normal use — `profiles.py` already respects it via `get_default_root()`. For tests, `monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))` will work IF `get_default_root()` reads that env var. **Verify in test setup**; if not, override profile lookup in tests directly.

---

## D-0.6 — Existing `oc skills` collision (REQUIRES PLAN CHANGE)

**Verified at `opencomputer/cli_skills.py` + `opencomputer/cli.py:1824-1872`:**

Two Typer apps cohabit `cli_skills.py`:
1. `skill_app` (singular) — registered as `oc skill` — Skills Guard interface (`oc skill scan <path>`).
2. `app` (imported as `skills_app`, plural) — registered as `oc skills` — Auto-skill-evolution review (`oc skills list/review/accept/reject/evolution`).

**Finding:** the plan assumed `oc skills` was unused. **Wrong.** It already hosts evolution-review commands. Direct collision on `list`.

**Decision (Option A — minimum-blast-radius):**
- **Keep all existing `oc skills <evolution-cmd>` registrations exactly as-is.** No deprecation, no migration. Existing tests stay green.
- **Add NEW hub commands to the same `skills_app`** (the plural one). The new commands don't collide with existing names:
  - new: `browse, search, install, inspect, installed, uninstall, audit, update`
  - existing: `list, review, accept, reject, evolution`
- **Critical naming choice:** use **`installed`** (not `list`) for "what hub skills are installed" so we avoid the `list` collision. Trade: small parity loss vs Hermes (`hermes skills list` shows installed; we use `oc skills installed`). Net: cleaner code, no migration burden.
- **`tap` subgroup** (`oc skills tap add|remove|list`) — `tap list` is a subcommand of the `tap` group, so its `list` doesn't collide with top-level `list`.

**Plan task adjustments:**
- Task 4.1: rename `do_list()` → `do_installed()` and `cmd_list` → `cmd_installed`. Update tests.
- Task 4.2: do NOT register a new `skills_hub_app` separately. **Add commands directly into the existing `skills_app`** in `cli_skills.py` to keep the namespace single. Either:
  - (a) Append the new commands to `cli_skills.py` (one file, two thematic groups — cohabit pattern continues), OR
  - (b) Define new commands in `cli_skills_hub.py`, then in `cli_skills.py` after the existing app construction add: `from opencomputer.cli_skills_hub import attach_hub_commands; attach_hub_commands(skills_app)` — keeps the new file separate but plumbs into the existing app.
  - **Decision: (b)** — preserves separation of concerns, keeps cli_skills.py mostly unchanged, makes the skills-hub PR a clear delta.
- README updates: document both surfaces. The `installed` command is the surprising one.

---

## Summary of plan-correcting changes (apply during execution)

| Plan task | Original | Corrected |
|---|---|---|
| 1.4 lockfile lock | `fcntl.flock` | use `filelock` package (cross-platform) — verify dep first |
| 2.0 (NEW) | — | seed `well_known_manifest.json` from 5-10 bundled skills |
| 3.1 installer | `guard.scan(text) -> {severity, findings}` | `scan_skill(staging_dir, source) -> ScanResult` then `should_allow_install(result)` |
| 3.1 atomic | direct write to final | staging dir → scan → atomic move |
| 4.1 list | `do_list` / `oc skills list` | `do_installed` / `oc skills installed` (avoids collision) |
| 4.2 registration | `app.add_typer(skills_hub_app, name="skills")` | `attach_hub_commands(skills_app)` in `cli_skills.py` |
| 4.3 slash | function-based `SLASH_HANDLERS` dict | `SkillsHubSlashCommand` class with `.execute(args, runtime)` |
| `_profile_home()` | hardcoded "default" | `get_profile_dir(read_active_profile())` |
| Phase 1.0 (NEW) | — | extend `MemoryManager.list_skills` to walk `<skills_path>/.hub/<source>/` |

**Resulting plan still ships in 7-10 dev-days; the corrections do not increase scope, they just route around three wrong API assumptions and one namespace collision. Net testing burden is similar (fewer bad mocks, more real wiring).**

---

## Greenlight to start Phase 1

Phase 0 verifications pass. The plan's Phase 1 tasks (1.1 through 1.6) can proceed with the corrections above folded in.

**First Phase 1 task to execute:** Task 1.0 (NEW) — extend `MemoryManager.list_skills` to walk `.hub/<source>/`. This is technically Phase 0 cleanup but slots cleanly into Phase 1.
