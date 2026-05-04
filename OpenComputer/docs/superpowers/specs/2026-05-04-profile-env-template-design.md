# Phase 14.G — `oc profile env-template` — Design + Plan

**Date:** 2026-05-04
**Status:** combined spec+plan
**Reference:** CLAUDE.md §5 Tier-2 — Phase 14.G credential templates

---

## 1. Goal

Add `oc profile env-template` — generates a `.env.template` file in the active profile's home that lists every env var declared by enabled plugin manifests, with helpful comments (label + signup URL).

User flow:
```bash
oc profile env-template > /tmp/foo.env.template     # show on stdout
oc profile env-template --write                     # write to <profile>/.env.template
# user edits the template, fills in real values, renames to .env
# OC's existing main() calls load_for_profile() which picks up .env at startup
```

---

## 2. Karpathy verification

Verified before drafting:
- `setup.providers[].env_vars`, `setup.channels[].env_vars` already declared in plugin.json manifests
- `PluginManifest.setup.providers[].env_vars: tuple[str, ...]` already in `plugin_sdk/core.py`
- `discovery.discover()` returns `PluginCandidate` with `manifest.setup` populated
- Per-profile `.env` loading already wired in `main()` line 3812 (Phase 14.F)
- ~32 provider plugins + ~20 channel plugins each declare `env_vars` — concrete user value

---

## 3. Design

### 3.1 New module `opencomputer/profile_env_template.py`

Pure function — no I/O dependencies, easy to test:

```python
from plugin_sdk.core import PluginManifest

def render_env_template(
    plugins: list[PluginCandidate],
    *,
    enabled_ids: set[str] | None = None,
    include_disabled: bool = False,
) -> str:
    """Render a `.env.template` for the given plugins.
    
    Args:
        plugins: discovered plugin candidates (typically from discover()).
        enabled_ids: when set, only include enabled plugins. None = all.
        include_disabled: if True AND enabled_ids is set, include disabled
            plugins too but mark them clearly with `# [DISABLED]`.
    
    Returns:
        Multi-section template string. Each plugin gets a section with
        `# === <label> ===` header, followed by:
            # <description>
            # docs: <signup_url>
            VAR_NAME=
        for each env_var. Already-set env vars in os.environ get the
        existing value as a hint comment (`# currently: sk-...`).
    """
```

### 3.2 New CLI subcommand `oc profile env-template`

In `cli_profile.py`:

```python
@profile_app.command("env-template")
def profile_env_template(
    write: bool = typer.Option(
        False, "--write",
        help="Write to <profile_home>/.env.template instead of stdout.",
    ),
    include_disabled: bool = typer.Option(
        False, "--include-disabled",
        help="Include env vars from installed-but-disabled plugins (commented).",
    ),
) -> None:
    """Generate a .env template from plugin manifests."""
```

### 3.3 Output format

```
# ================================================================
# OpenComputer profile: default
# Generated: 2026-05-04 12:34:56 UTC
# Fill in values, rename to .env, OC will load on startup.
# ================================================================

# === Anthropic (Claude) ===
# anthropic-provider — Anthropic Claude models — supports native x-api-key…
# docs: https://console.anthropic.com/settings/keys
ANTHROPIC_API_KEY=

# === OpenAI ===
# openai-provider — OpenAI + OpenAI-compatible endpoints
# docs: https://platform.openai.com/api-keys
OPENAI_API_KEY=
```

When `os.environ` already has a key, comment hints at length:
```
ANTHROPIC_API_KEY=  # currently: sk-ant-…(98 chars)
```

(Don't echo full secret value — show only length + prefix to confirm "yes, set" without leaking.)

---

## 4. Tests

| Test | What |
|------|------|
| `test_render_includes_provider_env_vars` | feed manifest with provider env_vars → output contains them |
| `test_render_includes_channel_env_vars` | same for channels |
| `test_render_groups_per_plugin_with_label` | each plugin's section starts with its `# === <label> ===` header |
| `test_render_marks_disabled_plugins` | `include_disabled=True` + plugin not in enabled_ids → marked `# [DISABLED]` |
| `test_render_skips_empty_env_vars` | plugin with no env_vars → no section emitted |
| `test_render_hints_existing_env` | env var present in os.environ → comment shows truncated value + length |
| `test_render_no_secret_full_echo` | even if env value is short, only first ~5 chars + length shown |
| `test_render_includes_signup_url` | docs comment present when signup_url set |
| `test_render_handles_no_setup_block` | plugin without setup block → no crash, no section |

---

## 5. Out of scope (deferred)

- Auto-rotate / lifecycle of `.env.template` versioning
- Inline value editing (interactive form)
- Phase 14.H credential sharing (export with redaction) — separate PR

---

## 6. Self-audit

- **Risk: secret echo.** Counter: never echo full value. Comment shows `# currently: <first 5 chars>...(<N> chars)` so users know they have something set without dumping the secret to stdout/file.
- **Risk: writing template overwrites existing file.** Counter: `--write` flag is explicit; default to stdout. If `.env.template` exists, prompt OR add `.bak` (decision: just overwrite — it's a generated artifact, BAK adds noise).
- **Risk: discover() pulls disabled plugins.** Counter: filter via active-profile enabled set (read from profile.yaml).
- **Risk: plugins with `kind: tool` or `kind: skill` may not declare env_vars.** Counter: `manifest.setup.providers/channels` is iterated, both default empty if the plugin doesn't have those kinds. No-op for those.

### Defensible? Yes.
1 module + 1 CLI command + 9 tests, ~150 LOC, ~2-3h.
