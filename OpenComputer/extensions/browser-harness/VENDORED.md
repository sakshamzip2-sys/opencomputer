# Vendored code — provenance & re-sync notes

This plugin lifts Hermes Agent's `tools/browser_tool.py`, `tools/browser_camofox.py`,
`tools/browser_camofox_state.py`, and `tools/browser_providers/*` directly. The lift is
intentionally byte-identical to Hermes for those files **except for imports**, which are
swapped for OC equivalents via `compat.py`. This makes future re-sync against Hermes
upstream a small, mechanical job.

## Source

- Repo: `nousresearch/hermes-agent`
- Local working copy used for the initial port: `/Users/architsakri/Downloads/Harnesses/hermes-agent-main`
- Initial port date: 2026-05-08

## Files lifted (Hermes path → OC path)

| Hermes path | OC path | Status |
|---|---|---|
| `tools/browser_tool.py` | `dispatcher.py` | LIFTED — imports swapped + file renamed (see "Divergences") |
| `tools/browser_camofox.py` | `browser_camofox.py` | LIFTED — imports swapped |
| `tools/browser_camofox_state.py` | `browser_camofox_state.py` | LIFTED — imports swapped |
| `tools/browser_providers/__init__.py` | `browser_providers/__init__.py` | LIFTED unchanged |
| `tools/browser_providers/base.py` | `browser_providers/base.py` | LIFTED — imports swapped |
| `tools/browser_providers/browser_use.py` | `browser_providers/browser_use.py` | LIFTED — imports swapped + Nous-managed-gateway code path removed (see "Divergences" below) |
| `tools/browser_providers/browserbase.py` | `browser_providers/browserbase.py` | LIFTED — imports swapped |
| `tools/browser_providers/firecrawl.py` | `browser_providers/firecrawl.py` | LIFTED — imports swapped |

## OC-specific code (NOT from Hermes)

| File | Purpose |
|---|---|
| `plugin.json` | OC plugin manifest |
| `plugin.py` | `register(api)` entry — wires tools into OC's agent loop |
| `compat.py` | Tiny shims for every Hermes-specific import (config, redact, url_safety, registry, etc.) |
| `tools.py` | OC `BaseTool` subclasses that call into the lifted dispatcher |
| `config.py` | Per-OC-profile backend selection |
| `VENDORED.md` | this file |

## Divergences from Hermes upstream

### `dispatcher.py` — renamed from `browser_tool.py`

OC's plugin loader does not clear ``browser_tool`` from ``sys.modules``
between plugin loads, and OC's ``dev-tools`` plugin already exposes a
top-level ``browser_tool.py`` (with a ``BrowserTool`` class). Loading both
plugins under the same module name caused dev-tools's
``from browser_tool import BrowserTool`` to grab THIS plugin's module
and fail at import. The fix was renaming our copy to ``dispatcher.py``.

File contents are byte-identical to Hermes ``tools/browser_tool.py``
EXCEPT for the imports already documented below; only the filename
changed. References inside ``browser_camofox.py`` and ``tools.py`` were
updated to ``import dispatcher`` accordingly.

### `browser_providers/browser_use.py` — Nous-managed-gateway path removed

Hermes's `BrowserUseProvider._get_config_or_none()` falls back to a Nous-hosted
managed-tool gateway when `BROWSER_USE_API_KEY` is unset. That gateway is
internal to Nous's infrastructure and irrelevant to OC users — they always
provide their own API key. The fallback branch was removed during the port:

- imports: `from tools.managed_tool_gateway import resolve_managed_tool_gateway` and
  `from tools.tool_backend_helpers import managed_nous_tools_enabled` are gone.
- `_get_config_or_none()` now returns `None` immediately when the API key is unset.
- `_get_config()` raises a single error message (no managed-mode variant).

Downstream code in `BrowserUseProvider.create_session()` still references
`config.get("managed_mode")` for branching — those branches stay as silent dead
code (they evaluate to falsy because we never set `managed_mode=True`). This was
intentional: keeping the branches reduces the diff against upstream and makes
re-sync easier. If a future re-sync brings non-managed-mode code changes, they
land cleanly; if it brings managed-mode-only changes, they're inert.

When re-syncing, regenerate `_get_config_or_none()` and `_get_config()` from
upstream with the same simplification.

## How to re-sync against a new Hermes commit

1. `git diff` Hermes upstream `tools/browser_tool.py` against our `browser_tool.py`.
2. Apply the upstream diff to our copy.
3. Re-run the import-swap pass (only ~12-15 lines at top of file).
4. Run `pytest tests/` in the plugin to verify shims still match.
5. Bump the version in `plugin.json` and update this file's "Initial port date" header
   to "Last re-sync date".
