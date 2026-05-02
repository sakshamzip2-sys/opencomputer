# OpenCLI Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenCLI-style recipe-driven browser commands (`oc browser <site> <verb>`) on top of OpenComputer's existing Playwright tools, with CDP attach mode so commands run against the user's logged-in Chrome.

**Architecture:** Five sub-projects under philosophy α' (recipes-first for `oc browser` CLI; LLM-first for the chat agent's existing browser tools — clean seam, no regression). v1 ships sub-projects 1-4 in this session; sub-project 5 (AI-driven recipe synthesis) is a documented next-session followup with skeleton scaffolding.

**Tech Stack:** Python 3.13, Typer (CLI), pytest, ruff, Playwright (existing in `extensions/browser-control/`), pydantic (schema), pyyaml, jinja2-style templating (already a dep).

**Working dir:** `/Users/saksham/.config/superpowers/worktrees/claude/quality-foundation/OpenComputer/`

**Spec:** `OpenComputer/docs/superpowers/specs/2026-05-02-opencli-integration-design.md`

**Branch:** `feat/opencli-integration` (off `origin/main`)

**Honest scope warning:** This is a 5-sub-project integration. Phases 1-4 ship the user-visible v1 (CDP + recipes + 3 starter sites + CLI + LLM-fallback error). Phase 5 lays scaffolding for AI-discovery but defers the substantive LLM work to a separate plan. Each phase ends with a commit and is independently shippable — partial completion is acceptable.

---

## Phase 1 — CDP attach mode for `browser-control`

### Task 1.1: Pre-flight contention check

- [ ] **Step 1: Re-survey for parallel-session contention**

```bash
cd /Users/saksham/.config/superpowers/worktrees/claude/quality-foundation/OpenComputer
git fetch origin --prune
git log origin/main..origin/feat/opus-4-7-migration -5 --oneline -- extensions/browser-control/browser.py extensions/browser-control/tools.py 2>/dev/null
git log origin/main..origin/spec/tool-use-contract-tightening -5 --oneline -- extensions/browser-control/browser.py extensions/browser-control/tools.py 2>/dev/null
```
Expected: empty output (no parallel-session edits to these files). If any commits returned, pause and re-evaluate insertion points.

### Task 1.2: Add `cdp_url` parameter to browser launcher

**Files:**
- Modify: `extensions/browser-control/browser.py` — add CDP-attach helper function

- [ ] **Step 0: Verify Playwright import path + existing function names**

```bash
grep -n "from playwright\|^class \|^def \|async def \|chromium\.launch\|connect_over_cdp" extensions/browser-control/browser.py | head -20
```

Expected: identifies (a) the exact Playwright import path used (`from playwright.async_api import async_playwright`), (b) the entry point function name (e.g. `get_browser`, `BrowserManager.get_browser`, `_browser_singleton`), and (c) any existing kwargs on the `chromium.launch()` call (like `headless=`) that the CDP path must NOT override.

If the entry point is a method on a class, adapt the test + implementation accordingly. If `chromium.launch()` has kwargs like `headless=False`, preserve them in the launch fallback path.

- [ ] **Step 2: Write failing test**

`tests/test_browser_control_cdp_attach.py`:

```python
"""CDP attach mode lets browser tools connect to the user's already-running Chrome."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _load_browser_module():
    repo = Path(__file__).resolve().parent.parent
    provider_path = repo / "extensions" / "browser-control" / "browser.py"
    module_name = f"_browser_control_under_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_get_browser_via_cdp_calls_connect_over_cdp(monkeypatch):
    """When OPENCOMPUTER_BROWSER_CDP_URL is set, get_browser() uses connect_over_cdp."""
    monkeypatch.setenv("OPENCOMPUTER_BROWSER_CDP_URL", "http://localhost:9222")
    mod = _load_browser_module()

    fake_browser = MagicMock()
    fake_chromium = MagicMock()
    fake_chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    fake_chromium.launch = AsyncMock(return_value=fake_browser)
    fake_pw = MagicMock()
    fake_pw.chromium = fake_chromium

    with patch.object(mod, "async_playwright") as mock_pw_factory:
        ctx = MagicMock()
        ctx.start = AsyncMock(return_value=fake_pw)
        mock_pw_factory.return_value = ctx

        browser = await mod.get_browser()

    fake_chromium.connect_over_cdp.assert_awaited_once_with("http://localhost:9222")
    fake_chromium.launch.assert_not_called()


@pytest.mark.asyncio
async def test_get_browser_without_cdp_url_launches_fresh(monkeypatch):
    """When OPENCOMPUTER_BROWSER_CDP_URL is unset, fall back to chromium.launch()."""
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_CDP_URL", raising=False)
    mod = _load_browser_module()

    fake_browser = MagicMock()
    fake_chromium = MagicMock()
    fake_chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    fake_chromium.launch = AsyncMock(return_value=fake_browser)
    fake_pw = MagicMock()
    fake_pw.chromium = fake_chromium

    with patch.object(mod, "async_playwright") as mock_pw_factory:
        ctx = MagicMock()
        ctx.start = AsyncMock(return_value=fake_pw)
        mock_pw_factory.return_value = ctx

        browser = await mod.get_browser()

    fake_chromium.launch.assert_awaited_once()
    fake_chromium.connect_over_cdp.assert_not_called()
```

- [ ] **Step 3: Run test, expect FAIL**

```bash
.venv/bin/pytest tests/test_browser_control_cdp_attach.py -v
```
Expected: FAIL with `AttributeError` (no CDP path in `browser.py` yet).

- [ ] **Step 4: Add CDP path to `browser.py`**

In `extensions/browser-control/browser.py`, find the `get_browser` (or equivalent launch) function. Modify it to read the env var and dispatch:

```python
import os

async def get_browser():
    """Return a Playwright Browser, attaching to user's Chrome if configured.

    Set ``OPENCOMPUTER_BROWSER_CDP_URL`` (e.g. ``http://localhost:9222``) to
    attach to a Chrome instance the user launched with ``--remote-debugging-port=9222``.
    The user keeps full control of their tabs; we never close them.

    Without the env var, falls back to ``chromium.launch()`` (existing behaviour:
    ephemeral browser, no persistent state).
    """
    pw = await async_playwright().start()
    cdp_url = os.environ.get("OPENCOMPUTER_BROWSER_CDP_URL")
    if cdp_url:
        return await pw.chromium.connect_over_cdp(cdp_url)
    return await pw.chromium.launch(headless=True)
```

(Adapt names to whatever Step 1's grep found. If `get_browser` is not the existing entry-point name, use the existing one.)

- [ ] **Step 5: Run test, expect PASS**

```bash
.venv/bin/pytest tests/test_browser_control_cdp_attach.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/extensions/browser-control/browser.py OpenComputer/tests/test_browser_control_cdp_attach.py
git commit -m "feat(browser-control): CDP attach mode via OPENCOMPUTER_BROWSER_CDP_URL

Set the env var to e.g. http://localhost:9222 and the existing
Playwright tools attach to the user's already-running Chrome instead
of launching a fresh ephemeral browser. Unlocks 'use my session, my
cookies, my logins' for both the chat agent's natural browsing and
the recipe layer (Phase 2).

User must launch Chrome with --remote-debugging-port=9222. The
'oc browser chrome' helper in Phase 3 prints the right command per OS.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 1.3: Helper for OS-specific Chrome launch command

**Files:**
- Create: `extensions/browser-control/chrome_launch.py`
- Test: `tests/test_browser_control_chrome_launch.py`

- [ ] **Step 1: Write failing test**

```python
"""OS-specific Chrome launch command helper."""
from extensions.browser_control.chrome_launch import (  # noqa: E402
    chrome_launch_command,
)


def test_macos_command():
    cmd = chrome_launch_command(platform="darwin")
    assert "Google Chrome" in cmd or "google-chrome" in cmd.lower()
    assert "--remote-debugging-port=9222" in cmd


def test_linux_command():
    cmd = chrome_launch_command(platform="linux")
    assert "google-chrome" in cmd or "chromium" in cmd
    assert "--remote-debugging-port=9222" in cmd


def test_windows_command():
    cmd = chrome_launch_command(platform="win32")
    assert "chrome.exe" in cmd.lower() or "chrome" in cmd.lower()
    assert "--remote-debugging-port=9222" in cmd


def test_unknown_platform_raises():
    import pytest
    with pytest.raises(NotImplementedError):
        chrome_launch_command(platform="freebsd")
```

NOTE: this test imports via `extensions.browser_control.chrome_launch` (alias) — that depends on whether the test conftest aliases extensions. If not, fall back to `_load_chrome_launch_module()` like the CDP test above. Verify by running and adapting.

- [ ] **Step 2: Run test, expect ImportError**

```bash
.venv/bin/pytest tests/test_browser_control_chrome_launch.py -v
```
Expected: ImportError (module doesn't exist yet).

- [ ] **Step 3: Write the helper**

`extensions/browser-control/chrome_launch.py`:

```python
"""OS-specific Chrome launch command for CDP attach mode."""

CHROME_LAUNCH_COMMANDS = {
    "darwin": (
        '/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome '
        '--remote-debugging-port=9222 '
        '--user-data-dir=$HOME/Library/Application\\ Support/Google/Chrome'
    ),
    "linux": (
        'google-chrome --remote-debugging-port=9222 '
        '--user-data-dir="$HOME/.config/google-chrome"'
    ),
    "win32": (
        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        '--remote-debugging-port=9222 '
        '--user-data-dir="%LOCALAPPDATA%\\Google\\Chrome\\User Data"'
    ),
}


def chrome_launch_command(platform: str | None = None) -> str:
    """Return the shell command to launch Chrome with CDP debugging enabled.

    Uses the user's existing Chrome profile so logins, cookies, and
    extensions are preserved. The command MUST be run by the user
    (we don't auto-launch — it's their browser, their choice).
    """
    import sys
    if platform is None:
        platform = sys.platform
    if platform not in CHROME_LAUNCH_COMMANDS:
        raise NotImplementedError(
            f"No Chrome launch command for platform {platform!r}. "
            "Pass --remote-debugging-port=9222 to chrome and set "
            "OPENCOMPUTER_BROWSER_CDP_URL=http://localhost:9222."
        )
    return CHROME_LAUNCH_COMMANDS[platform]
```

- [ ] **Step 4: Run test, expect PASS**

```bash
.venv/bin/pytest tests/test_browser_control_chrome_launch.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/browser-control/chrome_launch.py OpenComputer/tests/test_browser_control_chrome_launch.py
git commit -m "feat(browser-control): chrome_launch_command() helper for CDP

Returns the OS-specific shell command to launch Chrome with
--remote-debugging-port=9222 against the user's existing profile.
Used by 'oc browser chrome' (Phase 3 CLI) so users get a
copy-pasteable command for their platform. We don't auto-launch
Chrome — it's the user's browser, their choice.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 2 — YAML recipe schema, loader, runner

### Task 2.1: Recipe schema (pydantic models)

**Files:**
- Create: `opencomputer/recipes/__init__.py`
- Create: `opencomputer/recipes/schema.py`
- Create: `tests/recipes/__init__.py`
- Create: `tests/recipes/test_schema.py`

- [ ] **Step 1: Write failing test**

`tests/recipes/test_schema.py`:

```python
"""Recipe schema validation."""
import pytest
import yaml

from opencomputer.recipes.schema import Recipe, validate_recipe


VALID_HN_YAML = """
name: hackernews
description: Hacker News scrapers
commands:
  top:
    description: Top stories from HN
    pipeline:
      - fetch: https://hacker-news.firebaseio.com/v0/topstories.json
      - take: "{{ limit | default(10) }}"
      - map:
          fetch: https://hacker-news.firebaseio.com/v0/item/{{ item }}.json
      - format:
          fields: [title, url, score, by]
    formats: [json, table, md]
"""


def test_valid_recipe_loads():
    data = yaml.safe_load(VALID_HN_YAML)
    recipe = validate_recipe(data)
    assert recipe.name == "hackernews"
    assert "top" in recipe.commands
    assert recipe.commands["top"].pipeline


def test_recipe_requires_name():
    bad = {"description": "x", "commands": {}}
    with pytest.raises(Exception):  # ValidationError
        validate_recipe(bad)


def test_command_requires_pipeline():
    bad = {
        "name": "foo",
        "commands": {"hot": {"description": "x"}},  # no pipeline
    }
    with pytest.raises(Exception):
        validate_recipe(bad)


def test_pipeline_steps_must_be_known_kinds():
    """Pipeline steps must be one of: fetch, map, take, filter, format, eval."""
    bad = {
        "name": "foo",
        "commands": {
            "hot": {
                "pipeline": [{"unknown_step": "value"}],
            },
        },
    }
    with pytest.raises(Exception):
        validate_recipe(bad)
```

- [ ] **Step 2: Run test, expect ImportError**

```bash
mkdir -p tests/recipes
touch tests/recipes/__init__.py
.venv/bin/pytest tests/recipes/test_schema.py -v
```
Expected: ImportError (module doesn't exist).

- [ ] **Step 3: Write the schema**

`opencomputer/recipes/__init__.py`:

```python
"""OpenCLI-style recipe layer for browser scraping commands.

Public API:
    load_recipe(site)       -> Recipe
    run_recipe(site, verb, *, args, formats) -> dict
    list_recipes()          -> list[str]
"""
```

`opencomputer/recipes/schema.py`:

```python
"""Pydantic models for recipe YAML.

A recipe is a per-site scraping spec. Each command is a named verb
(e.g. 'top', 'hot', 'bookmarks') with a pipeline of steps.

Pipeline step kinds (v1):
  - fetch: <url>            HTTP GET, parse JSON if Content-Type matches
  - take: <int|template>    Slice the iterable to N items
  - map: <step>             Apply <step> to each item; replaces item with result
  - filter: <jinja-expr>    Keep items where the expression is truthy
  - format:                 Pick fields and shape output
      fields: [title, url]
  - eval: <jinja-expr>      Run a jinja expression on the current value

Templates use simple jinja2: {{ item }}, {{ limit | default(10) }}, etc.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

PipelineStepKind = Literal["fetch", "take", "map", "filter", "format", "eval"]
KNOWN_KINDS: set[str] = {"fetch", "take", "map", "filter", "format", "eval"}


class Command(BaseModel):
    """One verb on a site (e.g. 'top', 'hot', 'bookmarks')."""

    description: str = ""
    pipeline: list[dict[str, Any]] = Field(min_length=1)
    formats: list[Literal["json", "table", "md", "csv"]] = ["json"]

    @field_validator("pipeline")
    @classmethod
    def _validate_steps(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for i, step in enumerate(v):
            if not isinstance(step, dict) or len(step) != 1:
                raise ValueError(
                    f"step {i} must be a dict with exactly one key (the kind)"
                )
            (kind,) = step.keys()
            if kind not in KNOWN_KINDS:
                raise ValueError(
                    f"step {i} kind {kind!r} not in known kinds {sorted(KNOWN_KINDS)}"
                )
        return v


class Recipe(BaseModel):
    """A site's recipe — name, description, and a dict of named commands."""

    name: str
    description: str = ""
    commands: dict[str, Command]


def validate_recipe(data: dict[str, Any]) -> Recipe:
    """Construct a Recipe from a raw dict (e.g. yaml.safe_load output).

    Raises pydantic.ValidationError on malformed data.
    """
    return Recipe.model_validate(data)
```

- [ ] **Step 4: Run test, expect PASS**

```bash
.venv/bin/pytest tests/recipes/test_schema.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/recipes/ OpenComputer/tests/recipes/
git commit -m "feat(recipes): pydantic schema for recipe YAML

Recipe = name + description + dict[verb, Command].
Command = description + pipeline (list of step dicts) + formats.
Pipeline step kinds (v1): fetch, take, map, filter, format, eval.

Schema is the contract; subsequent tasks add the loader + runner.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 2.2: Recipe loader (profile-local + bundled)

**Files:**
- Create: `opencomputer/recipes/loader.py`
- Create: `tests/recipes/test_loader.py`

- [ ] **Step 1: Write failing test**

`tests/recipes/test_loader.py`:

```python
"""Loader: profile-local recipes override bundled ones; missing site raises."""
from pathlib import Path

import pytest
import yaml

from opencomputer.recipes.loader import load_recipe, list_recipes


def _write_recipe(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "name": name,
        "commands": {
            "ping": {
                "pipeline": [{"fetch": "https://example.com/ping"}],
            },
        },
    }))


def test_load_from_bundled(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    profile = tmp_path / "profile"
    _write_recipe(bundled / "site_a.yaml", "site_a")

    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(profile))

    recipe = load_recipe("site_a")
    assert recipe.name == "site_a"


def test_profile_overrides_bundled(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    profile = tmp_path / "profile"
    _write_recipe(bundled / "site_a.yaml", "site_a_bundled")
    _write_recipe(profile / "site_a.yaml", "site_a_profile")

    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(profile))

    recipe = load_recipe("site_a")
    assert recipe.name == "site_a_profile"  # profile wins


def test_unknown_site_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(tmp_path / "bundled"))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))

    with pytest.raises(KeyError):
        load_recipe("does_not_exist")


def test_list_recipes_combines_dirs(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    profile = tmp_path / "profile"
    _write_recipe(bundled / "alpha.yaml", "alpha")
    _write_recipe(profile / "beta.yaml", "beta")

    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(profile))

    names = sorted(list_recipes())
    assert names == ["alpha", "beta"]
```

- [ ] **Step 2: Run test, expect FAIL**

```bash
.venv/bin/pytest tests/recipes/test_loader.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement loader**

`opencomputer/recipes/loader.py`:

```python
"""Recipe file discovery + parsing.

Search order (highest priority first):
  1. OPENCOMPUTER_RECIPES_PROFILE_DIR  (default: ~/.opencomputer/<profile>/recipes/)
  2. OPENCOMPUTER_RECIPES_BUNDLED_DIR  (default: <repo>/extensions/browser-recipes/recipes/)

Each file is a single recipe ('site_name.yaml'). Filename stem is the site key.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from opencomputer.recipes.schema import Recipe, validate_recipe


def _profile_dir() -> Path:
    env = os.environ.get("OPENCOMPUTER_RECIPES_PROFILE_DIR")
    if env:
        return Path(env)
    home = Path.home() / ".opencomputer" / os.environ.get("OPENCOMPUTER_PROFILE", "default")
    return home / "recipes"


def _bundled_dir() -> Path:
    env = os.environ.get("OPENCOMPUTER_RECIPES_BUNDLED_DIR")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[2]  # opencomputer/recipes/loader.py -> repo
    return repo / "extensions" / "browser-recipes" / "recipes"


def _candidate_paths(site: str) -> list[Path]:
    """Profile-local first, then bundled."""
    return [
        _profile_dir() / f"{site}.yaml",
        _bundled_dir() / f"{site}.yaml",
    ]


def load_recipe(site: str) -> Recipe:
    """Find and parse the recipe for ``site``.

    Profile-local recipes override bundled ones. Raises KeyError if no
    recipe file is found in either dir.
    """
    for path in _candidate_paths(site):
        if path.exists():
            data = yaml.safe_load(path.read_text())
            return validate_recipe(data)
    raise KeyError(
        f"No recipe for site {site!r}. Searched: "
        + ", ".join(str(p) for p in _candidate_paths(site))
    )


def list_recipes() -> list[str]:
    """Return all recipe site names available in profile + bundled dirs.

    Profile-local names override bundled ones (set semantics, dedup'd).
    """
    seen: set[str] = set()
    for d in (_profile_dir(), _bundled_dir()):
        if d.exists():
            for f in d.glob("*.yaml"):
                seen.add(f.stem)
    return sorted(seen)
```

- [ ] **Step 4: Run test, expect PASS**

```bash
.venv/bin/pytest tests/recipes/test_loader.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/recipes/loader.py OpenComputer/tests/recipes/test_loader.py
git commit -m "feat(recipes): loader with profile-local override of bundled recipes

Search order: profile dir first, bundled dir second. Profile-local
recipes override bundled ones with the same name. list_recipes() dedups.

Env-var overrides for tests: OPENCOMPUTER_RECIPES_PROFILE_DIR and
OPENCOMPUTER_RECIPES_BUNDLED_DIR.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 2.3: Pipeline runner (fetch / take / map / format / eval)

**Files:**
- Create: `opencomputer/recipes/runner.py`
- Create: `tests/recipes/test_runner.py`

- [ ] **Step 1: Write failing test**

`tests/recipes/test_runner.py`:

```python
"""Pipeline runner — executes recipe pipelines against a fetcher mock."""
from unittest.mock import MagicMock

import pytest

from opencomputer.recipes.runner import run_pipeline
from opencomputer.recipes.schema import Recipe, validate_recipe


def _build_recipe(pipeline):
    return validate_recipe({
        "name": "test",
        "commands": {"go": {"pipeline": pipeline}},
    }).commands["go"]


def test_pipeline_with_static_take():
    """take: 3 truncates a list."""
    fake_fetcher = MagicMock(return_value=[1, 2, 3, 4, 5])
    cmd = _build_recipe([
        {"fetch": "https://example.com/list.json"},
        {"take": 3},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fake_fetcher)
    assert result == [1, 2, 3]


def test_pipeline_with_templated_take():
    """take: '{{ limit }}' uses the args dict."""
    fake_fetcher = MagicMock(return_value=list(range(20)))
    cmd = _build_recipe([
        {"fetch": "https://example.com/list.json"},
        {"take": "{{ limit }}"},
    ])
    result = run_pipeline(cmd, args={"limit": 5}, fetcher=fake_fetcher)
    assert result == [0, 1, 2, 3, 4]


def test_pipeline_map_then_format():
    """map a fetch over each item, then format with fields."""
    def fetch(url):
        if "list" in url:
            return [1, 2]
        # /item/<n>.json
        n = int(url.split("/")[-1].replace(".json", ""))
        return {"id": n, "title": f"item {n}", "extra": "ignored"}

    cmd = _build_recipe([
        {"fetch": "https://example.com/list.json"},
        {"map": {"fetch": "https://example.com/item/{{ item }}.json"}},
        {"format": {"fields": ["id", "title"]}},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetch)
    assert result == [
        {"id": 1, "title": "item 1"},
        {"id": 2, "title": "item 2"},
    ]


def test_pipeline_filter_keeps_truthy():
    fetcher = MagicMock(return_value=[
        {"score": 100}, {"score": 50}, {"score": 200}
    ])
    cmd = _build_recipe([
        {"fetch": "https://example.com/list.json"},
        {"filter": "{{ item.score >= 100 }}"},
    ])
    result = run_pipeline(cmd, args={}, fetcher=fetcher)
    assert result == [{"score": 100}, {"score": 200}]
```

- [ ] **Step 2: Run test, expect FAIL**

```bash
.venv/bin/pytest tests/recipes/test_runner.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write the runner**

`opencomputer/recipes/runner.py`:

```python
"""Pipeline executor.

Runs a recipe Command's pipeline against a fetcher (a callable
``fetch(url) -> dict | list``). The default fetcher uses Playwright's
page.evaluate() for HTML scrapes and httpx for raw JSON; for tests
inject a mock.

Templates (jinja2-shaped, simple syntax):
  {{ item }}                  current value in a map
  {{ limit | default(10) }}   args["limit"] or 10
"""

from __future__ import annotations

from typing import Any, Callable

from jinja2 import Environment, StrictUndefined

from opencomputer.recipes.schema import Command


def _render(template: Any, ctx: dict[str, Any]) -> Any:
    """Render a template string against ctx; pass-through non-strings."""
    if not isinstance(template, str):
        return template
    if "{{" not in template and "{%" not in template:
        return template
    env = Environment(undefined=StrictUndefined, autoescape=False)
    return env.from_string(template).render(**ctx)


def _coerce_int(s: Any) -> int:
    if isinstance(s, int):
        return s
    return int(str(s))


def _eval_truthy(template: str, ctx: dict[str, Any]) -> bool:
    rendered = _render(template, ctx)
    if isinstance(rendered, str):
        rendered = rendered.strip().lower()
        return rendered not in ("", "false", "0", "none")
    return bool(rendered)


def run_pipeline(
    cmd: Command,
    *,
    args: dict[str, Any],
    fetcher: Callable[[str], Any],
) -> Any:
    """Execute a recipe command's pipeline; return final value.

    ``fetcher`` is the URL → JSON-or-list-of-dicts callable. Tests inject
    a mock; production wires in a Playwright-page-aware fetcher.
    """
    value: Any = None
    for step in cmd.pipeline:
        (kind, spec), = step.items()
        ctx: dict[str, Any] = {**args, "value": value}
        if kind == "fetch":
            url = _render(spec, ctx)
            value = fetcher(url)
        elif kind == "take":
            n = _coerce_int(_render(spec, ctx))
            if not isinstance(value, list):
                raise TypeError(f"take requires list, got {type(value).__name__}")
            value = value[:n]
        elif kind == "map":
            inner_step = spec  # dict like {"fetch": "..."}
            (inner_kind, inner_spec), = inner_step.items()
            if inner_kind != "fetch":
                raise NotImplementedError(
                    f"map currently only supports inner kind 'fetch', got {inner_kind!r}"
                )
            if not isinstance(value, list):
                raise TypeError(f"map requires list, got {type(value).__name__}")
            mapped = []
            for item in value:
                item_ctx = {**args, "item": item}
                url = _render(inner_spec, item_ctx)
                mapped.append(fetcher(url))
            value = mapped
        elif kind == "filter":
            if not isinstance(value, list):
                raise TypeError(f"filter requires list, got {type(value).__name__}")
            value = [
                item for item in value
                if _eval_truthy(spec, {**args, "item": item})
            ]
        elif kind == "format":
            fields = (spec or {}).get("fields") or []
            if not isinstance(value, list):
                raise TypeError(f"format requires list, got {type(value).__name__}")
            value = [
                {f: item.get(f) for f in fields} for item in value
                if isinstance(item, dict)
            ]
        elif kind == "eval":
            value = _render(spec, ctx)
        else:
            raise ValueError(f"unknown pipeline step kind: {kind}")
    return value
```

- [ ] **Step 4: Run test, expect PASS**

```bash
.venv/bin/pytest tests/recipes/test_runner.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/recipes/runner.py OpenComputer/tests/recipes/test_runner.py
git commit -m "feat(recipes): pipeline runner — fetch/take/map/filter/format/eval

Synchronous pipeline executor. fetch is injected as a callable, so the
runner is decoupled from Playwright (testable with mocks; production
wires in a real fetcher). Templates use jinja2-shaped {{ item }} and
{{ limit | default(10) }}.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 2.4: Output formatters (json / table / md)

**Files:**
- Create: `opencomputer/recipes/formats.py`
- Create: `tests/recipes/test_formats.py`

- [ ] **Step 1: Write failing test**

`tests/recipes/test_formats.py`:

```python
"""Output formatters: json, table, md."""
import json

from opencomputer.recipes.formats import format_output


SAMPLE = [
    {"title": "First", "score": 100},
    {"title": "Second", "score": 50},
]


def test_json_formats_as_pretty_json():
    out = format_output(SAMPLE, fmt="json")
    parsed = json.loads(out)
    assert parsed == SAMPLE


def test_table_includes_headers_and_rows():
    out = format_output(SAMPLE, fmt="table")
    assert "title" in out
    assert "score" in out
    assert "First" in out
    assert "Second" in out


def test_md_formats_as_table():
    out = format_output(SAMPLE, fmt="md")
    # markdown table has | separators
    assert "|" in out
    assert "title" in out
    assert "First" in out


def test_unknown_format_raises():
    import pytest
    with pytest.raises(ValueError):
        format_output(SAMPLE, fmt="xml")


def test_empty_list_handled_for_each_format():
    for fmt in ("json", "table", "md"):
        out = format_output([], fmt=fmt)
        assert isinstance(out, str)
```

- [ ] **Step 2: Run test, expect FAIL**

```bash
.venv/bin/pytest tests/recipes/test_formats.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write formatters**

`opencomputer/recipes/formats.py`:

```python
"""Output formatting for recipe results."""

from __future__ import annotations

import json
from typing import Any, Literal


Fmt = Literal["json", "table", "md", "csv"]


def format_output(rows: list[dict[str, Any]] | Any, *, fmt: Fmt = "json") -> str:
    """Render a list of dicts (or any value) as ``fmt``."""
    if fmt == "json":
        return json.dumps(rows, indent=2, default=str)
    if not isinstance(rows, list):
        rows = [rows] if rows is not None else []

    if not rows:
        if fmt == "table" or fmt == "md":
            return "(no rows)\n"
        if fmt == "csv":
            return ""

    if fmt == "table":
        return _format_table(rows)
    if fmt == "md":
        return _format_md(rows)
    if fmt == "csv":
        return _format_csv(rows)
    raise ValueError(f"unknown format {fmt!r}; use one of: json, table, md, csv")


def _all_keys(rows: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for r in rows:
        if isinstance(r, dict):
            for k in r:
                if k not in seen_set:
                    seen_set.add(k)
                    seen.append(k)
    return seen


def _format_table(rows: list[dict[str, Any]]) -> str:
    keys = _all_keys(rows)
    widths = {k: max(len(k), max((len(str(r.get(k, ""))) for r in rows), default=0)) for k in keys}
    header = "  ".join(k.ljust(widths[k]) for k in keys)
    sep = "  ".join("-" * widths[k] for k in keys)
    lines = [header, sep]
    for r in rows:
        lines.append("  ".join(str(r.get(k, "")).ljust(widths[k]) for k in keys))
    return "\n".join(lines) + "\n"


def _format_md(rows: list[dict[str, Any]]) -> str:
    keys = _all_keys(rows)
    header = "| " + " | ".join(keys) + " |"
    sep = "| " + " | ".join("---" for _ in keys) + " |"
    body = [
        "| " + " | ".join(str(r.get(k, "")) for k in keys) + " |"
        for r in rows
    ]
    return "\n".join([header, sep, *body]) + "\n"


def _format_csv(rows: list[dict[str, Any]]) -> str:
    import csv
    import io
    keys = _all_keys(rows)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
```

- [ ] **Step 4: Run test, expect PASS**

```bash
.venv/bin/pytest tests/recipes/test_formats.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/recipes/formats.py OpenComputer/tests/recipes/test_formats.py
git commit -m "feat(recipes): output formatters (json/table/md/csv)

Renders pipeline results as JSON, padded text table, markdown table,
or CSV. Empty-list and missing-key handling consistent across formats.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 2.5: Public API + httpx default fetcher

**Files:**
- Modify: `opencomputer/recipes/__init__.py` — wire `run_recipe`
- Create: `opencomputer/recipes/fetcher.py`
- Create: `tests/recipes/test_run_recipe.py`

- [ ] **Step 1: Write failing test**

`tests/recipes/test_run_recipe.py`:

```python
"""Public run_recipe API end-to-end with mock fetcher."""
from pathlib import Path

import pytest
import yaml


def test_run_recipe_end_to_end(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "demo.yaml").write_text(yaml.safe_dump({
        "name": "demo",
        "commands": {
            "list": {
                "pipeline": [
                    {"fetch": "https://example.com/{{ topic }}.json"},
                    {"take": "{{ limit | default(2) }}"},
                ],
            },
        },
    }))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))

    from opencomputer.recipes import run_recipe

    captured_urls = []
    def fake_fetcher(url):
        captured_urls.append(url)
        return ["a", "b", "c", "d"]

    out = run_recipe(
        site="demo",
        verb="list",
        args={"topic": "things", "limit": 2},
        fetcher=fake_fetcher,
        fmt="json",
    )

    assert captured_urls == ["https://example.com/things.json"]
    assert "a" in out and "b" in out and "c" not in out  # take=2


def test_run_recipe_unknown_verb_raises(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "demo.yaml").write_text(yaml.safe_dump({
        "name": "demo",
        "commands": {"list": {"pipeline": [{"fetch": "https://x"}]}},
    }))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))

    from opencomputer.recipes import run_recipe

    with pytest.raises(KeyError):
        run_recipe(site="demo", verb="bogus", args={}, fetcher=lambda u: [])
```

- [ ] **Step 2: Run test, expect FAIL**

```bash
.venv/bin/pytest tests/recipes/test_run_recipe.py -v
```
Expected: ImportError on `run_recipe`.

- [ ] **Step 3: Wire public API**

Append to `opencomputer/recipes/__init__.py`:

```python
from typing import Any, Callable

from opencomputer.recipes.formats import Fmt, format_output
from opencomputer.recipes.loader import list_recipes, load_recipe
from opencomputer.recipes.runner import run_pipeline


def run_recipe(
    *,
    site: str,
    verb: str,
    args: dict[str, Any],
    fetcher: Callable[[str], Any],
    fmt: Fmt = "json",
) -> str:
    """Load + run + format. Raises KeyError for unknown site or unknown verb."""
    recipe = load_recipe(site)
    if verb not in recipe.commands:
        raise KeyError(
            f"site {site!r} has no verb {verb!r}. "
            f"Known: {sorted(recipe.commands)}"
        )
    cmd = recipe.commands[verb]
    rows = run_pipeline(cmd, args=args, fetcher=fetcher)
    return format_output(rows, fmt=fmt)


__all__ = ["list_recipes", "load_recipe", "run_recipe"]
```

- [ ] **Step 4: Default fetcher (httpx-based)**

`opencomputer/recipes/fetcher.py`:

```python
"""Default HTTP fetcher for recipes.

For v1 the fetcher just does GET + parse-JSON. Phase 5 (or future
work) can plug in a Playwright-page-aware fetcher that runs requests
through the user's logged-in Chrome via CDP.
"""

from __future__ import annotations

from typing import Any

import httpx


def httpx_fetcher(url: str) -> Any:
    """GET + parse JSON; raise on non-2xx.

    NOTE: this is a SYNC fetcher. Calling it from inside an async context
    (e.g. the agent loop) would block the event loop. v1 callers are CLI
    commands which are sync. For async callers, future work adds an
    async fetcher built on httpx.AsyncClient or routes through a
    Playwright page.
    """
    resp = httpx.get(url, follow_redirects=True, timeout=15.0)
    resp.raise_for_status()
    ct = resp.headers.get("content-type", "")
    if "application/json" in ct or url.endswith(".json"):
        return resp.json()
    return resp.text
```

- [ ] **Step 5: Run test, expect PASS**

```bash
.venv/bin/pytest tests/recipes/test_run_recipe.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/recipes/__init__.py OpenComputer/opencomputer/recipes/fetcher.py OpenComputer/tests/recipes/test_run_recipe.py
git commit -m "feat(recipes): public run_recipe API + httpx default fetcher

run_recipe(site, verb, args, fetcher, fmt) is the one-shot entry point.
Default fetcher (httpx_fetcher) handles HTTP GETs with JSON
auto-detection. Tests use mock fetchers for determinism.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3 — Three starter recipes

### Task 3.1: hackernews recipe

**Files:**
- Create: `extensions/browser-recipes/recipes/hackernews.yaml`
- Create: `tests/recipes/test_starter_hackernews.py`

- [ ] **Step 1: Write the recipe**

`extensions/browser-recipes/recipes/hackernews.yaml`:

```yaml
name: hackernews
description: "Hacker News scrapers (public Firebase API; no auth needed)"
commands:
  top:
    description: "Top stories"
    pipeline:
      - fetch: "https://hacker-news.firebaseio.com/v0/topstories.json"
      - take: "{{ limit | default(10) }}"
      - map:
          fetch: "https://hacker-news.firebaseio.com/v0/item/{{ item }}.json"
      - format:
          fields: [id, title, url, score, by, descendants]
    formats: [json, table, md]
  new:
    description: "Newest stories"
    pipeline:
      - fetch: "https://hacker-news.firebaseio.com/v0/newstories.json"
      - take: "{{ limit | default(10) }}"
      - map:
          fetch: "https://hacker-news.firebaseio.com/v0/item/{{ item }}.json"
      - format:
          fields: [id, title, url, score, by]
    formats: [json, table, md]
  show:
    description: "Show HN"
    pipeline:
      - fetch: "https://hacker-news.firebaseio.com/v0/showstories.json"
      - take: "{{ limit | default(10) }}"
      - map:
          fetch: "https://hacker-news.firebaseio.com/v0/item/{{ item }}.json"
      - format:
          fields: [id, title, url, score, by]
    formats: [json, table, md]
```

- [ ] **Step 2: Test that recipe parses + runs against mock fetcher**

`tests/recipes/test_starter_hackernews.py`:

```python
"""hackernews recipe loads + runs end-to-end."""
import os
from pathlib import Path

from opencomputer.recipes import load_recipe, run_recipe


def test_hackernews_recipe_loads(monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    bundled = repo / "extensions" / "browser-recipes" / "recipes"
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", "/dev/null")

    recipe = load_recipe("hackernews")
    assert recipe.name == "hackernews"
    assert "top" in recipe.commands
    assert "new" in recipe.commands
    assert "show" in recipe.commands


def test_hackernews_top_runs_with_mock_fetcher(monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    bundled = repo / "extensions" / "browser-recipes" / "recipes"
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", "/dev/null")

    def fake_fetcher(url):
        if url.endswith("topstories.json"):
            return [42, 43, 44]
        # /item/<n>.json
        n = int(url.split("/")[-1].replace(".json", ""))
        return {"id": n, "title": f"Story {n}", "url": f"https://x/{n}", "score": n, "by": "alice"}

    out = run_recipe(
        site="hackernews", verb="top", args={"limit": 2},
        fetcher=fake_fetcher, fmt="json",
    )
    assert "Story 42" in out
    assert "Story 43" in out
    assert "Story 44" not in out  # take=2
```

- [ ] **Step 3: Run test, expect PASS**

```bash
.venv/bin/pytest tests/recipes/test_starter_hackernews.py -v
```
Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add OpenComputer/extensions/browser-recipes/recipes/hackernews.yaml OpenComputer/tests/recipes/test_starter_hackernews.py
git commit -m "feat(browser-recipes): hackernews recipe (top/new/show)

Public Firebase API; no auth. Three commands: top, new, show. Each
returns id, title, url, score, by — supports json/table/md formats.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 3.2: reddit recipe

- [ ] **Step 1: Write the recipe**

`extensions/browser-recipes/recipes/reddit.yaml`:

```yaml
name: reddit
description: "Reddit public JSON endpoints (no login required for public subreddits)"
commands:
  hot:
    description: "Hot posts in a subreddit"
    pipeline:
      - fetch: "https://www.reddit.com/r/{{ subreddit | default('programming') }}/hot.json?limit={{ limit | default(10) }}"
      - eval: "{{ value['data']['children'] }}"
      - map:
          fetch: "data:application/json,{{ item['data'] | tojson }}"
      - format:
          fields: [title, url, score, num_comments, author, subreddit]
    formats: [json, table, md]
  new:
    description: "Newest posts in a subreddit"
    pipeline:
      - fetch: "https://www.reddit.com/r/{{ subreddit | default('programming') }}/new.json?limit={{ limit | default(10) }}"
      - eval: "{{ value['data']['children'] | map(attribute='data') | list }}"
      - format:
          fields: [title, url, score, num_comments, author]
    formats: [json, table, md]
```

NOTE: reddit's public JSON nests posts inside `data.children[].data`. The `eval` step extracts that. The `map → fetch data:` is one way to thread items through; an alternative is a dedicated `extract` step kind. For v1 we use `eval` to do the JSON path extraction, then `format` the resulting list.

REVISION: The `eval` step in v1 just renders a jinja template; it doesn't do JSON path extraction natively. Let's use a different shape that v1's runner supports. Replace the `hot` command above with:

```yaml
  hot:
    description: "Hot posts in a subreddit"
    pipeline:
      - fetch: "https://www.reddit.com/r/{{ subreddit | default('programming') }}/hot.json?limit={{ limit | default(10) }}"
      - eval: "{{ value['data']['children'] }}"
      - map:
          fetch: "https://www.reddit.com/by_id/{{ item['data']['name'] }}.json"
      - format:
          fields: [title, url, score, num_comments, author]
    formats: [json, table, md]
```

— but this still relies on eval producing a Python list, and our v1 runner's `eval` returns a string. We need to extend `eval` to handle jinja expressions that produce non-string values, OR add a new `select_path` step kind for JSON path extraction.

Cleanest fix: extend `eval` to keep the value as-is (not stringify) when the rendered result can be parsed as a Python expression. v1 can keep this simple by having `eval` use jinja's native object passing.

Actually the simplest path: skip Reddit for v1 (it requires JSON path traversal beyond what the simple pipeline supports). Use it as a v2 case after we extend the pipeline grammar.

REVISED PLAN: drop reddit from v1 starter recipes. Use only `hackernews` and `github_trending` as starters. Reddit is documented as a "v2 candidate that demonstrates pipeline-grammar expansion needs."

- [ ] **Step 2: REVISED — skip reddit entirely**

Don't create `reddit.yaml`. Document the reason in this commit's `docs/superpowers/notes/2026-05-02-opencli-discovery-notes.md`.

- [ ] **Step 3: Commit the documentation skip**

```bash
mkdir -p OpenComputer/docs/superpowers/notes
cat > OpenComputer/docs/superpowers/notes/2026-05-02-opencli-recipe-grammar-notes.md << 'EOF'
# Recipe Pipeline Grammar — v1 Limitations

While authoring starter recipes I found that Reddit's public JSON endpoints
need JSON-path traversal: `data.children[*].data.<field>`. The v1 pipeline
grammar (fetch / take / map / filter / format / eval) handles flat lists
and per-item map-fetch but doesn't natively support "extract this nested
list before continuing."

Workarounds that DON'T work in v1:
1. `eval` returns strings (jinja2 native), so `eval: "{{ data.children }}"`
   produces a stringified list, breaking subsequent `take`/`map`/`format`.
2. `map` only supports inner `fetch`, not inner `eval` or `select`.

v2 grammar additions to consider:
- `select` step: JSON path extraction (e.g. `select: "data.children[*].data"`)
- `eval` returning native Python objects (extend the runner to detect
  jinja2 expressions that evaluate to non-strings)

For v1, drop sites that need this (reddit). Stick with sites that have
flat list endpoints (hackernews, github_trending).
EOF

git add OpenComputer/docs/superpowers/notes/2026-05-02-opencli-recipe-grammar-notes.md
git commit -m "docs: recipe pipeline grammar v1 limits (reddit deferred to v2)

Documents why reddit isn't a v1 starter recipe — its endpoints need
JSON-path nesting beyond what fetch/take/map/filter/format/eval handles.
Lists v2 grammar additions (select step, native-object eval) that
unblock it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 3.3: github_trending recipe (HTML scrape)

For HTML-scraping recipes we need a DIFFERENT fetcher path that runs through Playwright with a CSS selector. v1's `fetch` step only does HTTP GET + JSON parse. To support HTML scraping we'd need to extend either:
- Add a `selector` step kind that runs Playwright and extracts elements
- Wire a Playwright-aware fetcher behind `fetch:` when URL is HTML

This is a substantial extension. Honest scope: github_trending also gets deferred to v2 alongside reddit. v1 ships ONE starter recipe (hackernews) that fully exercises the pipeline.

- [ ] **Step 1: Document the deferral**

Append to `OpenComputer/docs/superpowers/notes/2026-05-02-opencli-recipe-grammar-notes.md`:

```markdown
## github_trending — also deferred to v2

HTML-scraping recipes need a Playwright-aware fetcher that runs through the
user's CDP-attached Chrome and extracts via CSS selectors. v1's httpx-based
fetcher returns raw HTML strings; the pipeline can't traverse them without
a `selector` step kind.

v2 additions:
- `selector` step kind: `selector: 'article.Box-row a.markdown-title'` →
  list of element textContent + href
- Or: extend the default fetcher to dispatch on Content-Type (HTML →
  Playwright, JSON → httpx). When CDP attach mode is on, HTML fetches
  flow through the user's Chrome.

For v1 the only starter recipe is hackernews. github_trending and reddit
are v2.
```

- [ ] **Step 2: Commit**

```bash
git add OpenComputer/docs/superpowers/notes/2026-05-02-opencli-recipe-grammar-notes.md
git commit -m "docs: github_trending also deferred — v1 ships only hackernews

Single-starter v1 is honest about the pipeline grammar's current scope:
JSON endpoints with flat lists. Sites needing JSON-path traversal
(reddit) or HTML+selectors (github_trending) wait for v2 grammar
extensions documented here.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 4 — CLI dispatcher (`oc browser`)

### Task 4.1: `oc browser` Typer app skeleton

**Files:**
- Create: `opencomputer/cli_browser.py`
- Modify: `opencomputer/cli.py` — register the subcommand
- Create: `tests/test_cli_browser.py`

- [ ] **Step 1: Write failing test**

`tests/test_cli_browser.py`:

```python
"""oc browser CLI dispatch."""
from typer.testing import CliRunner

from opencomputer.cli_browser import browser_app


def test_browser_help():
    runner = CliRunner()
    result = runner.invoke(browser_app, ["--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "show" in result.stdout
    assert "chrome" in result.stdout


def test_browser_list_returns_recipes(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "alpha.yaml").write_text(
        "name: alpha\ncommands:\n  go:\n    pipeline:\n      - fetch: 'https://x'\n"
    )
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))

    runner = CliRunner()
    result = runner.invoke(browser_app, ["list"])
    assert result.exit_code == 0
    assert "alpha" in result.stdout


def test_browser_chrome_prints_command():
    runner = CliRunner()
    result = runner.invoke(browser_app, ["chrome"])
    assert result.exit_code == 0
    assert "--remote-debugging-port=9222" in result.stdout
```

- [ ] **Step 2: Run test, expect ImportError**

```bash
.venv/bin/pytest tests/test_cli_browser.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write the CLI**

`opencomputer/cli_browser.py`:

```python
"""'oc browser' subcommand — recipe-driven scrapes against logged-in Chrome.

Two layers:
  - Recipe-first: 'oc browser <site> <verb>' looks up a YAML recipe.
  - LLM-fallback (--llm-fallback): one-off LLM-driven scrape if no recipe.
"""

from __future__ import annotations

from typing import Any

import typer

browser_app = typer.Typer(
    help="Recipe-driven browser commands. 'oc browser list' to see installed recipes.",
    no_args_is_help=True,
)


@browser_app.command("list")
def list_command():
    """List all installed recipes (profile-local + bundled)."""
    from opencomputer.recipes import list_recipes

    names = list_recipes()
    if not names:
        typer.echo("No recipes installed. Add one to ~/.opencomputer/<profile>/recipes/.")
        return
    for name in names:
        typer.echo(name)


@browser_app.command("show")
def show_command(site: str = typer.Argument(...)):
    """Show a recipe's commands and pipeline."""
    from opencomputer.recipes import load_recipe

    try:
        recipe = load_recipe(site)
    except KeyError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Recipe: {recipe.name}")
    if recipe.description:
        typer.echo(f"  {recipe.description}")
    typer.echo("\nCommands:")
    for verb, cmd in recipe.commands.items():
        typer.echo(f"  {verb}: {cmd.description or '(no description)'}")
        typer.echo(f"    pipeline ({len(cmd.pipeline)} steps): "
                   f"{', '.join(list(s.keys())[0] for s in cmd.pipeline)}")
        typer.echo(f"    formats: {cmd.formats}")


@browser_app.command("chrome")
def chrome_command():
    """Print the Chrome launch command for CDP attach mode."""
    import importlib.util as _ilu
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    spec = _ilu.spec_from_file_location(
        "_chrome_launch_for_cli",
        str(repo / "extensions" / "browser-control" / "chrome_launch.py"),
    )
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    typer.echo("# Run this in a SEPARATE terminal to launch Chrome with CDP enabled:")
    typer.echo(mod.chrome_launch_command())
    typer.echo()
    typer.echo("# Then in your shell:")
    typer.echo("export OPENCOMPUTER_BROWSER_CDP_URL=http://localhost:9222")
    typer.echo()
    typer.echo("# Now 'oc browser <site> <verb>' will use your real Chrome.")


@browser_app.command()
def run(
    site: str = typer.Argument(...),
    verb: str = typer.Argument(...),
    limit: int = typer.Option(10, "--limit", "-n"),
    fmt: str = typer.Option("json", "--format", "-f"),
    llm_fallback: bool = typer.Option(False, "--llm-fallback"),
):
    """Run a recipe: 'oc browser run <site> <verb>'.

    NOTE on '--llm-fallback': v1 ships this flag as a STUB that exits 2
    with a "not yet implemented" message. Phase 5 (next-session) wires
    the real LLM-fallback path. Default behaviour (no flag, missing
    recipe) is exit 1 with helpful options.
    """
    from opencomputer.recipes import run_recipe
    from opencomputer.recipes.fetcher import httpx_fetcher

    try:
        out = run_recipe(
            site=site, verb=verb, args={"limit": limit},
            fetcher=httpx_fetcher, fmt=fmt,
        )
    except KeyError as e:
        if llm_fallback:
            typer.echo(f"# LLM fallback for {site}/{verb} not yet implemented (Phase 5).", err=True)
            raise typer.Exit(code=2)
        typer.echo(
            f"No recipe for {site}/{verb}. Options:\n"
            f"  - oc browser run {site} {verb} --llm-fallback   # one-off LLM scrape\n"
            f"  - Add a recipe to ~/.opencomputer/<profile>/recipes/{site}.yaml",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(out)
```

- [ ] **Step 4: Register in `cli.py`**

In `opencomputer/cli.py`, find `app.add_typer(...)` registrations and append:

```python
from opencomputer.cli_browser import browser_app  # noqa: E402

app.add_typer(browser_app, name="browser")
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_cli_browser.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/cli_browser.py OpenComputer/opencomputer/cli.py OpenComputer/tests/test_cli_browser.py
git commit -m "feat(cli): oc browser subcommand (list/show/chrome/run)

Four subcommands:
- list: enumerate installed recipes
- show <site>: display recipe commands + pipeline summary
- chrome: print OS-specific Chrome launch command for CDP attach mode
- run <site> <verb>: dispatch to recipe runner; --llm-fallback flag
  reserved for Phase 5

Default exit-1 with helpful options when site/verb missing.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 4.2: End-to-end smoke

- [ ] **Step 1: Smoke test the full chain**

```bash
.venv/bin/python -m opencomputer.cli browser list
.venv/bin/python -m opencomputer.cli browser show hackernews
.venv/bin/python -m opencomputer.cli browser chrome
```
Expected:
- `list` prints `hackernews`
- `show hackernews` prints recipe + commands
- `chrome` prints OS-appropriate Chrome launch command

- [ ] **Step 2: Run pipeline against real HN (network test)**

```bash
.venv/bin/python -m opencomputer.cli browser run hackernews top --limit 3 --format json
```
Expected: JSON output with 3 stories. (Skip if network unavailable.)

- [ ] **Step 3: No new commit needed (smoke test only).**

---

## Phase 5 — Skeleton for AI-driven recipe synthesis (next session)

### Task 5.1: Document the next-session work

Sub-project 5 is genuinely too large for this session. Lay scaffolding without implementing.

- [ ] **Step 1: Write the followup plan**

`OpenComputer/docs/superpowers/plans/2026-05-02-opencli-discovery-NEXT-SESSION.md`:

```markdown
# OpenCLI Discovery — NEXT SESSION

This is a placeholder plan for the AI-driven recipe synthesis sub-project
(originally Phase 5 of the OpenCLI integration spec).

## Why deferred

- Network capture via Playwright route() interception needs careful design
  for auth-token redaction
- LLM-driven recipe synthesis needs prompt engineering + iteration
- 'cascade' auth detection is a multi-strategy probe with state
- Pipeline grammar may need extending (see notes/2026-05-02-opencli-recipe-grammar-notes.md)

## Scope when picked up

Four subcommands:
- oc browser explore <url> --site <name>
- oc browser cascade <api-url>
- oc browser synthesize <site>
- oc browser generate <url> --goal <goal>

## Pre-requisites

- v1 (Phases 1-4 of opencli-integration plan) merged
- Pipeline grammar extensions for select / native-eval (see grammar notes)
- ANTHROPIC_API_KEY or OPENAI_API_KEY (LLM-driven synthesis)

## Estimated scope

2-3 weeks of careful work. Each subcommand is its own bite-sized plan.
```

- [ ] **Step 2: Commit**

```bash
git add OpenComputer/docs/superpowers/plans/2026-05-02-opencli-discovery-NEXT-SESSION.md
git commit -m "docs: AI-driven recipe synthesis deferred to next session

Phase 5 of the OpenCLI integration spec — explore/cascade/synthesize/
generate — is genuinely large (2-3 weeks). Documented as a separate
session with prerequisites and scope.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 6 — Final verification + push + PR + merge

### Task 6.1: Full verification

- [ ] **Step 1: All new tests pass**

```bash
.venv/bin/pytest tests/recipes/ tests/test_browser_control_cdp_attach.py tests/test_browser_control_chrome_launch.py tests/test_cli_browser.py -v
```
Expected: ALL pass.

- [ ] **Step 2: Ruff clean on touched files**

```bash
.venv/bin/ruff check opencomputer/recipes/ opencomputer/cli_browser.py opencomputer/cli.py extensions/browser-control/ tests/recipes/ tests/test_cli_browser.py tests/test_browser_control_cdp_attach.py tests/test_browser_control_chrome_launch.py
```
Expected: clean (or auto-fixable).

- [ ] **Step 3: Full pytest minus voice flakiness**

```bash
.venv/bin/pytest tests/ --tb=line -q --ignore=tests/test_voice_mode_audio_capture.py --ignore=tests/test_voice_mode_doctor.py --ignore=tests/test_voice_mode_no_egress.py --ignore=tests/test_voice_mode_orchestrator.py --ignore=tests/test_voice_mode_stt.py --ignore=tests/test_voice.py
```
Expected: 0 failures.

### Task 6.2: Push + PR

- [ ] **Step 1: Push**

```bash
git push origin feat/opencli-integration
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat: OpenCLI integration (CDP attach + recipes + oc browser CLI)" --body "$(cat <<'EOF'
## Summary

Adds OpenCLI-style recipe-driven browser commands on top of OpenComputer's existing Playwright tools.

## What ships (v1, Phases 1-4)

**Phase 1: CDP attach mode** — Set OPENCOMPUTER_BROWSER_CDP_URL=http://localhost:9222 and existing Playwright tools attach to user's already-running Chrome (real session, real cookies, real logins). chrome_launch_command() helper prints OS-appropriate Chrome launch command.

**Phase 2: Recipe layer** — opencomputer/recipes/ module with pydantic schema, profile-local-overrides-bundled loader, pipeline runner (fetch / take / map / filter / format / eval), and json/table/md/csv output formatters. httpx default fetcher for JSON endpoints.

**Phase 3: One starter recipe** — hackernews (top/new/show). Reddit and github_trending honestly deferred to v2 (need pipeline grammar extensions: select step + HTML/Playwright fetcher path).

**Phase 4: oc browser CLI** — list / show / chrome / run subcommands. Default exit-1 with helpful options when site/verb missing. --llm-fallback flag reserved (returns exit 2 today; Phase 5 wires it).

## What's deferred (Phase 5, next session)

AI-driven recipe synthesis (explore / cascade / synthesize / generate). Plan documented at docs/superpowers/plans/2026-05-02-opencli-discovery-NEXT-SESSION.md.

Pipeline grammar extensions (select step kind for JSON-path; HTML/Playwright fetcher) — needed before reddit and github_trending can ship as recipes.

## Tests

~25 new tests covering schema validation, loader precedence, pipeline runner, formats, end-to-end run_recipe, hackernews recipe, CLI dispatcher.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Wait for CI green**

Watch ruff + pytest 3.12/3.13 + cross-platform. Diagnose specific failures if any.

- [ ] **Step 4: Merge**

```bash
gh pr merge --squash
```

---

## Self-Review

**Spec coverage:**
- Sub-project 1 (CDP attach) → Tasks 1.2, 1.3 ✓
- Sub-project 2 (recipe layer) → Tasks 2.1-2.5 ✓
- Sub-project 2 starter recipes → Task 3.1 (hackernews); reddit + github_trending HONESTLY DEFERRED with documentation ✓
- Sub-project 3 (CLI dispatcher) → Tasks 4.1-4.2 ✓
- Sub-project 4 (--llm-fallback) → Task 4.1 ships exit-1 with helpful message; --llm-fallback flag reserved (returns exit 2). Honest partial coverage; full LLM-fallback is Phase 5 work ✓
- Sub-project 5 (AI-discovery) → Task 5.1 documents the next-session plan ✓

**Placeholder scan:** No "TBD" / "implement later" / "Add appropriate" / "Similar to Task N" patterns.

**Type consistency:** `Recipe.commands: dict[str, Command]` consistent. `Command.pipeline: list[dict]` consistent. `run_pipeline(cmd, *, args, fetcher) -> Any` consistent. `run_recipe(*, site, verb, args, fetcher, fmt)` matches in __init__.py and CLI.

**Honest scope acknowledgement:** Reddit + github_trending recipes documented as v2-grammar-needed rather than silently dropped. Phase 5 has its own next-session plan rather than half-implemented. Each ends with a real commit.
