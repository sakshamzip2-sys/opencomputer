# AUDIT — Ship-Now 5 Picks (2026-04-29)

**Auditor verdict up front:** Plan is **YELLOW → leaning RED**. Six of the five sub-projects rest on API shapes that don't exist on `main`. The plan's Phase 0 anticipates *some* of this but multiple Sub-projects then specify code that contradicts what Phase 0 will discover. Fix the criticals below before Sub-project execution begins.

Evidence taken from: `extensions/openai-provider/provider.py`, `opencomputer/agent/config.py`, `opencomputer/agent/config_store.py`, `opencomputer/cli_ui/slash.py`, `opencomputer/cli_ui/slash_handlers.py`, `extensions/{telegram,discord,slack}/adapter.py`, `opencomputer/snapshot/quick.py`, `plugin_sdk/slash_command.py`, `opencomputer/plugins/loader.py`, `opencomputer/plugins/registry.py`.

---

## 1. CRITICAL defects (must fix before execution)

### C1. OpenRouterProvider env-swap is unnecessary AND breaks credential pooling

- **Where:** Sub-project A, Task A2, Step 3.
- **Defect:** `OpenAIProvider.__init__` already accepts `api_key=` and `base_url=` directly as kwargs (`extensions/openai-provider/provider.py:139-148`). The plan's "swap `OPENAI_*` in env, call `super().__init__()`, restore env" dance is dead code — and worse, it **defeats the credential-pool path**. `OpenAIProvider` parses the `api_key` argument for commas (`provider.py:151-158`) and builds a `CredentialPool` only when commas are present *in the value passed in*. The plan also overwrites `self.api_key = api_key` and `self.base_url = base_url` after `super().__init__()` runs, which collides with the parent's `self._api_key`/`self._base` private attributes and creates a public/private name split (the parent reads `self._api_key`; nothing else reads `self.api_key`).
- **Why it matters:** (a) Pool mode silently disabled for OpenRouter — users with `OPENROUTER_API_KEY=key1,key2` get only the first key. (b) The env-swap leaves a window during construction in which `OPENAI_API_KEY` is mutated; any concurrent thread reading env there sees the wrong value. (c) Tests in the plan (`test_default_base_url_is_openrouter`) assert `openrouter_provider.base_url == ...` — the parent stores it on `self._base`, not `self.base_url`, so the patched-on attribute is the only thing keeping the tests green and it is brittle.
- **Suggested fix:**
  ```python
  class OpenRouterProvider(OpenAIProvider):
      _api_key_env = "OPENROUTER_API_KEY"  # parent honors this for env lookup
      def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
          if not (api_key or os.environ.get("OPENROUTER_API_KEY")):
              raise RuntimeError("OPENROUTER_API_KEY is not set. ...")
          super().__init__(
              api_key=api_key,
              base_url=base_url or os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
          )
  ```
  Drop the env-swap entirely. Override `_api_key_env` so the parent reads `OPENROUTER_API_KEY`. Tests should assert against `provider._base` and `provider._api_key` (or expose typed properties).

### C2. `/debug` slash command registration shape is fabricated

- **Where:** Sub-project D, Task D2, Step 2.
- **Defect:** The plan writes `slash_commands["debug"] = SlashCommand(name=..., description=..., handler=...)`. Two problems:
  1. `plugin_sdk/slash_command.py:40-58` defines `SlashCommand` as an `ABC` whose `__init__` is the default and whose abstract method is `execute(self, args, runtime)` — there is no `handler` field and you cannot instantiate the ABC.
  2. The CLI's slash dispatch is in `opencomputer/cli_ui/slash.py` and uses `SLASH_REGISTRY: list[CommandDef]` (a flat list of metadata-only `CommandDef` dataclasses; `slash.py:44-128`). Handlers live in `opencomputer/cli_ui/slash_handlers.py` keyed by name in a `_HANDLERS` table (see `_handle_snapshot`, `slash_handlers.py:263+`). There is no `slash_commands` dict the plan can write to.
- **Why it matters:** The PR will not compile. Worse, the plan also references "register_slash_command" in passing — that's the `PluginAPI.register_slash_command` (loader.py:838) used for plugin-authored commands at load time, not for built-in CLI commands. They are two distinct registries.
- **Suggested fix:** Add a `CommandDef(name="debug", description="Sanitized diagnostic dump for bug reports.", category="meta")` to `SLASH_REGISTRY` and add a `_handle_debug(ctx, args)` to `slash_handlers.py` returning a `SlashResult(handled=True, message=build_debug_dump())`. Wire it in the `_HANDLERS` lookup. Don't touch `plugin_sdk.SlashCommand`.

### C3. Telegram/Discord/Slack adapters take `config: dict`, not kwargs

- **Where:** Sub-project C, Task C2/C3/C4 (all three).
- **Defect:** All three adapters share the signature `def __init__(self, config: dict[str, Any]) -> None` — see `extensions/telegram/adapter.py:161`, `extensions/discord/adapter.py:157`, `extensions/slack/adapter.py:84`. The plan's "add 5 new kwargs to `__init__`" rewrites the signature, which (a) breaks every existing `TelegramAdapter(config={...})` call site, including `extensions/telegram/plugin.py:28` (`TelegramAdapter(config={"bot_token": token})`), and (b) breaks the `BaseChannelAdapter` superclass contract (it calls `super().__init__(config)` — `adapter.py:162`).
- **Why it matters:** PR fails to import. Tests at `tests/extensions/test_telegram_*.py` (~15 files) instantiate the adapter with `config=` shape and would all break.
- **Suggested fix:** Read the same 5 fields from inside `__init__` via `config.get("streaming", {}).get("block_chunker", False)` etc., not via new kwargs. The plugin.py wiring change collapses to "stuff the streaming sub-block into the config dict before constructing adapter" — already the existing pattern (see `require_mention`, `mention_patterns` in `adapter.py:178-191`).

### C4. Stream-callback dispatch has unbounded backpressure + chat_id capture bug

- **Where:** Sub-project C, Task C1, Step 2.
- **Defect:** The plan's `_send` closure does `asyncio.create_task(_adapter.send(_chat_id, text))`. Three real problems:
  1. **Unbounded scheduling.** `BlockChunker.feed()` is sync; `wrap_stream_callback` invokes `_send` for every emitted block. If `adapter.send` is slow (Telegram bot 30 msg/sec global; 1 msg/sec same-chat — see Telegram Bot API limits documented in `extensions/telegram/network.py`), `create_task` queues unbounded coroutines. Block N+1 can be **submitted** before N completes, but Telegram serialises by chat anyway → out-of-order delivery is possible if N is delayed by a server-side rate-limit retry while N+1 waits on the asyncio scheduler queue.
  2. **No await of the resulting tasks.** Exceptions in `adapter.send` are swallowed by the asyncio default exception handler with a "Task exception was never retrieved" warning — silent message-loss to the user.
  3. **Discord rate limits:** `chat.postMessage`-equivalent route on Discord is 5 messages / 5 sec / channel; long replies will hit it. The plan asserts this is "fine" without evidence.
- **Why it matters:** A multi-paragraph reply on a flaky network reorders blocks, drops blocks silently, or blasts past the rate limit and the user sees a 429-truncated tail. This is the core UX feature; if it ships broken, sub-project C is net-negative.
- **Suggested fix:** Use an `asyncio.Queue(maxsize=8)` between `_send` and a single drain task; the chunker's sync `_send` does `loop.call_soon_threadsafe(queue.put_nowait, text)` (or blocks on a per-chat lock). The drain task awaits `adapter.send` serially and surfaces exceptions to the dispatch error path. Add a per-platform safety floor on `human_delay_min_ms` (Telegram 1000, Discord 1100, Slack 1100).

### C5. Tar-slip check is incomplete — symlinks and PAX bypass it

- **Where:** Sub-project E, Task E2, Step 2.
- **Defect:** The check `name.startswith("/") or ".." in name.split("/")` catches naive escapes but misses:
  1. **Symlinks** (`tarfile.SYMTYPE` / `LNKTYPE`) — a member with `name="ok.txt"` and `linkname="../../../etc/passwd"` extracts as a symlink that, on first dereference, escapes. `tarfile.extract` follows it.
  2. **Windows backslash paths** — `name="..\..\evil"` passes `name.split("/")` as a single token without `..`.
  3. **Drive letters / UNC paths** on Windows — `name="C:\evil"` passes the `startswith("/")` check.
  4. **PAX extended headers** with `LIBARCHIVE.xattr.*` or hard-link redirection — uncommon, but the standard `tarfile` module does honor them.
- **Why it matters:** `import_snapshot` is intended to accept archives from "any user, possibly hostile" — that's the threat model of an import feature. A user accepting a malicious snapshot loses arbitrary files outside the profile.
- **Suggested fix:** Use Python 3.12+'s built-in `tarfile.data_filter` (added Aug 2024) which handles all of the above: `tf.extractall(path=dest, filter="data")`. If the project supports Python 3.12 (it does — `pyproject.toml`), this is a one-liner and obsoletes the manual check. Add an explicit reject for `tarfile.SYMTYPE/LNKTYPE/CHRTYPE/BLKTYPE/FIFOTYPE/CONTTYPE` for defense-in-depth.

### C6. Config-aliases plumbing is over-specified — `_apply_overrides` already handles dicts

- **Where:** Sub-project B, Task B3.
- **Defect:** `config_store.py::load_config` (lines 244-266) does NOT call `ModelConfig(...)` directly. It calls `_apply_overrides(base, raw)`, which uses `asdict()` round-trip + nested-dict matching (lines 35-78). For a `dict[str, str]` field, the existing path works **without modification** — YAML `model: {model_aliases: {fast: x}}` lands in `cfg.model.model_aliases` automatically. The plan's "find the construction site, add `model_aliases = raw.get(...)`, pass into `ModelConfig(...)`" describes a code path that doesn't exist. A grep for `ModelConfig(` finds exactly **one** site (`opencomputer/setup_wizard.py:475`), unrelated to `load_config`.
- **Why it matters:** Engineers will spend hours hunting for a non-existent construction site and either (a) wedge defensive code in the wrong place or (b) add a YAML branch that conflicts with `_apply_overrides`.
- **Suggested fix:** Drop Task B3 entirely. Replace with a one-paragraph note: "Once `ModelConfig.model_aliases` exists (B2), YAML auto-loads it via `_apply_overrides`. Add a round-trip integration test only." The defensive type-coerce (str-keys, str-values) belongs in `model_resolver.resolve_model` itself or in a `__post_init__` validator on `ModelConfig`.

---

## 2. HIGH-priority concerns

- **H1. `read_active_profile()` import path is correct (verified at `opencomputer/profiles.py`), but the plan says `from opencomputer.profiles import read_active_profile`. Note `profiles.py` is a *module*, not a package — Phase 0.0 should record this so engineers don't try `from opencomputer.profiles.foo import ...`.

- **H2. Missing `~/.opencomputer/logs/error.log` convention.** A grep across `opencomputer/` for `error.log` returns nothing. The plan reads `~/.opencomputer/logs/error.log` in `build_debug_dump`, which today is *always* "(no error log found)". Either wire logging to write that file (out of scope for `/debug`) or read from `<profile_home>/agent.log` (which DOES exist — `FullSystemControlConfig.log_path`, `config.py:306`).

- **H3. `_TRACKED_ENV_VARS` list is incomplete.** Misses: `BRAVE_API_KEY`, `TAVILY_API_KEY`, `EXA_API_KEY`, `FIRECRAWL_API_KEY`, `GROQ_API_KEY`, `HONCHO_API_KEY`, `HONCHO_BASE_URL` (last one could leak local network info — keep it as set/unset only). Also the plan misnames `BRAVE_SEARCH_API_KEY`; the actual env name in `opencomputer/agent/config.py:265` and the WebSearch tool is `BRAVE_API_KEY`.

- **H4. `error.log` tail uses `read_text().splitlines()[-20:]`.** OOM risk on a multi-MB log. Use a bounded `seek(-65536, os.SEEK_END)` + last-newline-anchored read; or use the stdlib `collections.deque(f, maxlen=20)` pattern.

- **H5. `import_snapshot` collision check missing.** `uuid.uuid4().hex[:12]` (48 bits) is collision-resistant for human use, but `dest = snapshot_root / new_id; dest.mkdir(parents=True, exist_ok=True)` will silently merge into an existing snapshot if one exists. Use `mkdir(exist_ok=False)` and retry on collision (or just don't truncate the uuid).

- **H6. `chat_id` capture in `_send` is fine for 1:1 chats** but Telegram DM-Topics-aware code in `extensions/telegram/dm_topics.py` uses `(chat_id, message_thread_id)` tuples. Single-arg `adapter.send(chat_id, text)` will lose thread routing. Pass the original `MessageEvent` or its `metadata` through.

- **H7. Phase 0 / DECISIONS.md is itself a placeholder.** Tasks 0.1 / 0.2 / 0.3 / 0.4 say "Document in DECISIONS.md..." but Phase 0 verifications then need to be REFLECTED back into Sub-projects A/B/C/D — the plan never explicitly says "if Phase 0 finds X, revise Sub-project Y." Past chains (OpenClaw Tier 1) tripped exactly here. Add a "Phase 0.7 — gate" task that requires the engineer to update each Sub-project's failing-test step before any Sub-project starts.

- **H8. Test fixture references are abstract.** B4 test step says "Engineer follows existing AgentLoop test fixture patterns" without naming a specific fixture. C1 says "follows tests/test_dispatch.py fixture pattern" but `tests/test_dispatch.py` may not be where dispatch tests actually live. Verify before referencing.

---

## 3. MEDIUM observations

- **M1.** Plan calls `register_provider("openrouter", OpenRouterProvider)` with a class. Loader path (`loader.py:783-805`) accepts class OR instance and validates instance config; class path skips validation. Fine, but document "we register the class so config_schema validation defers to construction time" so future readers don't add an instance-time `config_schema`.

- **M2.** `OpenAIProvider.default_model = "gpt-5.4"` (`provider.py:135`). OpenRouter routes by full model id like `openai/gpt-4o-mini`. Either set `OpenRouterProvider.default_model` to a sensible default (`"openai/gpt-4o-mini"`) or document that users MUST configure `model:` in YAML.

- **M3.** Plan's commit messages embed `(A1)`, `(A2)` task tags. Existing convention (per `git log`) is conventional commits without sub-tags. Either align or note the deviation.

- **M4.** `import_snapshot` strips top-level dir but `create_snapshot` produces a `state-snapshots/<id>/` layout (note: directory is `state-snapshots`, not `snapshots` — plan's docs use both names interchangeably). `export_snapshot` uses `arcname=snapshot_id` which produces `<id>/...` in the archive, but readers should know this is NOT what `tarfile.add(dir)` does by default.

- **M5.** `model_aliases` resolution is depth-capped at 5 — fine. But unrelated chains can interact: `{"a":"b", "b":"a"}` is detected, but `{"a":"a"}` (self-reference) hits the seen-set check on iteration 2; verify the test covers it.

- **M6.** `/snapshot import` returns `f"imported as snapshot {new_id}"` but `new_id` includes the optional label suffix (E2: `new_id = f"{new_id}-{label[:40]}"`). Document that the user-visible id contains the label.

---

## 4. Stress tests

- **S1. Both `OPENROUTER_API_KEY` AND `OPENAI_API_KEY` set + `OPENAI_BASE_URL` overridden:** with the env-swap (defective C1), the OpenRouter provider would mutate the env DURING `super().__init__()` then restore. If a *concurrent* OpenAI provider is loading (parallel plugin discovery is opt-in but possible in Phase 6a), it sees the wrong base_url. With the fix, no env mutation, no problem.

- **S2. Telegram chunker fires while previous send hasn't ACK'd:** With `asyncio.create_task` (defective C4), block N+1's task is created but Telegram returns 429 on N. `asyncio.gather` is never called → 429 retry never propagates → user sees N succeeded, N+1 stuck forever. With queue-based fix, the drain task observes the 429 + retries.

- **S3. `model_aliases` references a model the provider doesn't support:** The current plan's `resolve_model` resolves "fast" → "claude-haiku-4-5-20251001" → passes to provider. If `provider="openai"` and the user has `model: fast` aliased to a Claude model id, OpenAI returns `404 model not found`. Error surfaces at provider call time, but the message is cryptic. Add a "validate aliases against provider" step in `oc doctor` (out of scope for ship-now, but log a follow-up).

- **S4. `/debug` from uninitialised profile:** `read_active_profile()` returns `None` on a fresh install (per `cli.py:179` reading) — plan handles this with `or "default"`. Verified safe.

- **S5. `import_snapshot` of cross-version archive:** A snapshot taken from an OC pre-Phase-14.A install may not have a `<id>/manifest.json` (the snapshot module gained that field at some point). Plan does no version check. Restore could leave the new profile with a half-imported snapshot. Add a minimal compat check: `if (dest / "manifest.json").exists(): validate; else: log warning + continue`.

---

## 5. Alternative approaches

- **Pick A — drop OpenRouter as a separate plugin entirely.** `extensions/openai-provider/provider.py:165` already reads `OPENAI_BASE_URL`. Document `export OPENAI_BASE_URL=https://openrouter.ai/api/v1; export OPENAI_API_KEY=$OPENROUTER_KEY`. Zero code, one README paragraph. The downside: setup wizard can't auto-register "openrouter" as a provider name, and users with both keys can't use both at once. **Recommendation: still ship the plugin, but as a 12-line file (per the C1 fix), not the 60-line env-swap variant.**

- **Pick B — runtime CLI flag instead of config field.** `oc chat --model fast` parsed at the CLI entry. Pros: no config schema change; aliases scoped to the invocation. Cons: not visible in `gateway` (Telegram/Discord) where there's no CLI. **Recommendation: keep config-based; aliases need to apply to channel routes too.**

- **Pick C — chunker as a plugin.** Could ship `opencomputer-chunker` plugin that registers a `wrap_stream_callback` factory and adapters opt in via `import streaming.chunker`. Cons: indirection users don't need; chunker is already in `plugin_sdk/streaming` (PR #247). **Recommendation: keep as adapter config.**

- **Pick D — reuse `oc doctor --json`.** `opencomputer/doctor.py` already prints diagnostic info; add `--json` and `--redact` flags. Pros: one source of truth. Cons: `oc doctor` doesn't run inside a chat session. **Recommendation: implement `/debug` as a thin wrapper that calls `doctor.run_health_checks(redact=True, format="markdown")` so the two stay in sync.**

- **Pick E — CLI subcommand instead of slash.** `oc snapshot export <id> /path/out.tar.gz` and `oc snapshot import /path/in.tar.gz`. Pros: no slash plumbing complexity; pipe-friendly for cron backups. Cons: users-in-chat have to drop to a shell. **Recommendation: ship BOTH — subcommand for ops, slash for ergonomics. They share the `export_snapshot`/`import_snapshot` core.**

---

## 6. Final verdict

| Pick | Verdict | One-line rationale |
|---|---|---|
| A — OpenRouter | **REVISE** | C1: drop env-swap; subclass via `_api_key_env` + super kwargs. |
| B — Model aliases | **REVISE** | C6: drop B3 YAML-loader work — `_apply_overrides` already handles it. |
| C — Adapter wiring | **REVISE (the most)** | C3 + C4: keep `config: dict` shape; replace `create_task` with a per-chat queue. |
| D — `/debug` | **REVISE** | C2: register via `SLASH_REGISTRY` + `slash_handlers._handle_debug`, not `plugin_sdk.SlashCommand`. |
| E — `/snapshot export\|import` | **REVISE** | C5: use `tarfile` `data_filter`. Add symlink-type rejection. Pick fix in #5. |

**Overall plan readiness:** **YELLOW** — every pick is salvageable without re-scoping; the criticals are surface-level (wrong API shape, wrong env-swap pattern) not architectural. None of them require a redesign.

**Recommended next action:** Insert "Phase 0.7 — apply audit fixes to all 5 sub-projects" as a hard gate before any Sub-project starts. Specifically: (a) rewrite Task A2 per C1; (b) delete Task B3 per C6; (c) rewrite Tasks C2/C3/C4 to mutate the `config` dict, not `__init__` kwargs; (d) rewrite Task D2 to use `SLASH_REGISTRY`; (e) replace tar-slip handcheck with `data_filter` in Task E2; (f) lock per-platform rate-limit floors in C1; (g) wire a per-chat asyncio.Queue between chunker and adapter.send. Once those land in the plan doc, plan goes GREEN.

— end of audit —
