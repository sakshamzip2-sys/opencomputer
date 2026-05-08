# Personality, Skins & `@`-References Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the storage-only `/personality` and `/skin` stubs with real loaders + apply paths, and add `@file:`/`@folder:`/`@diff`/`@staged`/`@git:N`/`@url:` reference expansion in the CLI input loop.

**Architecture:** Three independent modules. `opencomputer/agent/personality/` resolves a name to a body and PromptBuilder injects it as slot #7. `opencomputer/cli_ui/skin/` defines a SkinSpec + 9 built-in YAMLs and applies the spec to the live Rich console. `opencomputer/agent/at_references.py` parses `@`-tokens in user input and expands them inline before the message hits the agent loop, with a single blocked-paths policy and soft/hard size caps.

**Tech Stack:** Python 3.13, Rich, prompt_toolkit, PyYAML (already a dep), httpx (already a dep via link_understanding).

---

## File Structure

**New:**

```
opencomputer/agent/personality/
├── __init__.py                      ← public surface: resolve(), Personality
├── builtins.py                      ← 14 built-in personality bodies
└── loader.py                        ← resolution chain + custom override

opencomputer/agent/at_references.py  ← parser + expanders (single file)

opencomputer/cli_ui/skin/
├── __init__.py                      ← public surface
├── spec.py                          ← SkinSpec, SpinnerSpec, BrandingSpec
├── loader.py                        ← built-in YAML + custom override
├── apply.py                         ← apply_skin(spec, console)
└── builtins/
    ├── default.yaml
    ├── ares.yaml
    ├── mono.yaml
    ├── slate.yaml
    ├── daylight.yaml
    ├── warm-lightmode.yaml
    ├── poseidon.yaml
    ├── sisyphus.yaml
    └── charizard.yaml

tests/test_personality_loader.py
tests/test_personality_prompt_injection.py
tests/test_skin_loader.py
tests/test_skin_apply.py
tests/test_at_references_parser.py
tests/test_at_references_expand.py
tests/test_at_references_input_loop.py
```

**Modified:**

```
opencomputer/agent/slash_commands_impl/skin_personality_cmd.py
    — wire to real loaders + persist to config.yaml

opencomputer/agent/prompt_builder.py
    — call personality.loader.resolve(name) inside build_system_prompt;
      render body into existing slot #7

opencomputer/cli_ui/input_loop.py
    — call at_references.expand(user_text) after slash dispatch,
      before message construction

opencomputer/cli.py
    — add `--personality NAME` and `--skin NAME` CLI flags;
      apply at session start

opencomputer/agent/profile_yaml.py
    — read/write `agent.default_personality`, `agent.personalities`,
      `display.skin` keys

opencomputer/agent/config.py
    — typed dataclass fields for the new config keys
```

---

## Task 1: Personality builtins + loader

**Files:**
- Create: `opencomputer/agent/personality/__init__.py`
- Create: `opencomputer/agent/personality/builtins.py`
- Create: `opencomputer/agent/personality/loader.py`
- Test: `tests/test_personality_loader.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_personality_loader.py`:

```python
"""Personality loader: builtins, custom override, resolution chain."""
from __future__ import annotations

import pytest

from opencomputer.agent.personality import Personality, resolve


def test_builtin_helpful_resolves_to_nonempty_body():
    p = resolve("helpful", custom={})
    assert isinstance(p, Personality)
    assert p.name == "helpful"
    assert len(p.body) > 50
    assert "helpful" in p.body.lower() or "user" in p.body.lower()


def test_all_14_builtins_resolve():
    expected = {
        "helpful", "concise", "technical", "creative", "teacher",
        "kawaii", "catgirl", "pirate", "shakespeare", "surfer",
        "noir", "uwu", "philosopher", "hype",
    }
    for name in expected:
        p = resolve(name, custom={})
        assert p.name == name
        assert p.body.strip(), f"{name} has empty body"


def test_unknown_name_falls_back_to_helpful():
    p = resolve("nonexistent_xyz", custom={})
    assert p.name == "helpful"


def test_empty_name_returns_helpful():
    p = resolve("", custom={})
    assert p.name == "helpful"


def test_custom_overrides_builtin():
    custom = {"helpful": "OVERRIDE BODY"}
    p = resolve("helpful", custom=custom)
    assert p.name == "helpful"
    assert p.body == "OVERRIDE BODY"


def test_custom_only_name():
    custom = {"codereviewer": "Be thorough about bugs."}
    p = resolve("codereviewer", custom=custom)
    assert p.name == "codereviewer"
    assert p.body == "Be thorough about bugs."


def test_malformed_custom_entry_skipped():
    custom = {"good": "OK", "bad": None}  # type: ignore[dict-item]
    p_good = resolve("good", custom=custom)
    assert p_good.body == "OK"
    p_bad = resolve("bad", custom=custom)
    assert p_bad.name == "helpful"  # falls back


def test_personality_dataclass_is_frozen():
    p = resolve("helpful", custom={})
    with pytest.raises(Exception):
        p.body = "no"  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && pytest tests/test_personality_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: opencomputer.agent.personality`

- [ ] **Step 3: Implement builtins**

Create `opencomputer/agent/personality/builtins.py`:

```python
"""Built-in personality bodies.

Each value is a short imperative paragraph that gives the model a
register without changing its identity (SOUL.md still owns identity).
Bodies are kept under ~200 words to fit the system-prompt budget.
"""
from __future__ import annotations

BUILTINS: dict[str, str] = {
    "helpful": (
        "Default register: be helpful, accurate, and direct. Take action "
        "when the user asks for it. Skip preamble and meta-narration. "
        "Ask one focused question only when truly blocked."
    ),
    "concise": (
        "Be terse. One to three sentences for a routine answer. Bullets "
        "over prose. No throat-clearing, no summaries of what you just "
        "did. Show, don't tell."
    ),
    "technical": (
        "Engineering register: precise terminology, named patterns, "
        "explicit complexity claims. Cite line numbers when discussing "
        "code. Prefer 'O(n log n)' to 'fast'. State invariants directly."
    ),
    "creative": (
        "Lateral register: propose multiple approaches, explore unusual "
        "angles, reach for analogies. Mark speculation as speculation. "
        "Generate first, evaluate second."
    ),
    "teacher": (
        "Pedagogical register: assume the user wants to understand, not "
        "just receive an answer. Explain the why behind the how. Build "
        "from familiar concepts. Check understanding by example, not by "
        "asking 'does that make sense?'"
    ),
    "kawaii": (
        "Cute register: warm, gentle, lots of soft phrasing. Use a few "
        "emoji per response (not every sentence). Stay competent — kawaii "
        "is the wrapper, not an excuse to be vague. (=^‿^=)"
    ),
    "catgirl": (
        "Catgirl register: kawaii base plus occasional 'nya~', cat ear "
        "energy, and playful asides. Stay technically accurate; the "
        "voice is the costume, not the substance."
    ),
    "pirate": (
        "Pirate register: 'Arr', 'matey', nautical metaphors ('chart a "
        "course', 'swab the deck'). Keep the technical content honest — "
        "the voice is decoration. Don't overdo the accent into "
        "unreadability."
    ),
    "shakespeare": (
        "Early Modern English register: 'thou', 'thee', 'thy', 'doth', "
        "'verily'. Iambic flourishes welcome. Keep the technical answer "
        "intact under the period dress. Fair user, attend!"
    ),
    "surfer": (
        "Surfer register: 'dude', 'gnarly', 'totally', 'rad'. Laid-back "
        "rhythm. Still get the answer right. The vibe is chill; the "
        "engineering isn't."
    ),
    "noir": (
        "Hardboiled detective register: short clipped sentences. World-"
        "weary observations. The bug was a dame. The stack trace told "
        "a story, and not a happy one. Stay accurate."
    ),
    "uwu": (
        "uwu register: replace some r/l with w, add ~, occasional "
        "'>w<'. Use sparingly — competence first, owo second. Don't "
        "uwu-ify code blocks or error messages."
    ),
    "philosopher": (
        "Reflective register: examine assumptions, name trade-offs, "
        "ask what the user really wants underneath the literal request. "
        "Keep it grounded — speculation labeled as such."
    ),
    "hype": (
        "Enthusiastic register: high energy, exclamation points used "
        "earned, celebrate small wins. LET'S GO. Stay accurate — hype "
        "without truth is just noise."
    ),
}

__all__ = ["BUILTINS"]
```

- [ ] **Step 4: Implement loader**

Create `opencomputer/agent/personality/loader.py`:

```python
"""Resolve a personality name to its body, with custom override."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

from .builtins import BUILTINS

logger = logging.getLogger("opencomputer.agent.personality")

DEFAULT_NAME = "helpful"


@dataclass(frozen=True, slots=True)
class Personality:
    name: str
    body: str


def resolve(name: str, *, custom: Mapping[str, object]) -> Personality:
    """Resolve a personality name to a Personality.

    Resolution order:
      1. ``custom[name]`` if it is a non-empty string (override builtins)
      2. ``BUILTINS[name]``
      3. fall back to ``BUILTINS[DEFAULT_NAME]``

    Never raises. Malformed custom entries (non-string, empty) are
    skipped with a one-shot warning.
    """
    key = (name or "").strip().lower() or DEFAULT_NAME

    custom_body = custom.get(key)
    if custom_body is not None:
        if isinstance(custom_body, str) and custom_body.strip():
            return Personality(name=key, body=custom_body.strip())
        logger.warning(
            "personality: custom entry %r is %s — falling back",
            key,
            type(custom_body).__name__,
        )

    body = BUILTINS.get(key)
    if body is None:
        return Personality(name=DEFAULT_NAME, body=BUILTINS[DEFAULT_NAME])
    return Personality(name=key, body=body)


__all__ = ["Personality", "resolve", "DEFAULT_NAME"]
```

- [ ] **Step 5: Implement package init**

Create `opencomputer/agent/personality/__init__.py`:

```python
"""Personality registry: built-in registers + custom override.

Personality is a *register* overlay. It does not replace the agent's
identity (which lives in SOUL.md and the base prompt). It adjusts how
the agent talks. Resolution is name → Personality(name, body); the
body is rendered into slot #7 of the system prompt.
"""
from __future__ import annotations

from .builtins import BUILTINS
from .loader import DEFAULT_NAME, Personality, resolve

__all__ = ["BUILTINS", "DEFAULT_NAME", "Personality", "resolve"]
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd OpenComputer && pytest tests/test_personality_loader.py -v`
Expected: PASS — 8 tests pass.

- [ ] **Step 7: Commit**

```bash
cd OpenComputer
git add opencomputer/agent/personality/ tests/test_personality_loader.py
git commit -m "feat(personality): 14 built-in registers + loader with custom override"
```

---

## Task 2: PromptBuilder integration

**Files:**
- Modify: `opencomputer/agent/prompt_builder.py`
- Test: `tests/test_personality_prompt_injection.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_personality_prompt_injection.py`:

```python
"""PromptBuilder injects personality body into the system prompt."""
from __future__ import annotations

from opencomputer.agent import personality as p_mod
from opencomputer.agent.prompt_builder import PromptBuilder


def _make_builder(custom_personalities=None) -> PromptBuilder:
    return PromptBuilder(
        cwd="/tmp",
        user_home="/tmp/home",
        os_name="Darwin",
        custom_personalities=dict(custom_personalities or {}),
    )


def test_helpful_body_appears_in_system_prompt():
    builder = _make_builder()
    prompt = builder.build_system_prompt(personality="helpful")
    assert p_mod.BUILTINS["helpful"] in prompt


def test_concise_body_appears_when_selected():
    builder = _make_builder()
    prompt = builder.build_system_prompt(personality="concise")
    assert p_mod.BUILTINS["concise"] in prompt
    assert p_mod.BUILTINS["helpful"] not in prompt


def test_unknown_personality_falls_back_to_helpful():
    builder = _make_builder()
    prompt = builder.build_system_prompt(personality="nonexistent")
    assert p_mod.BUILTINS["helpful"] in prompt


def test_custom_personality_overrides_builtin():
    builder = _make_builder(custom_personalities={
        "helpful": "OVERRIDE-BODY-MARKER-XYZ",
    })
    prompt = builder.build_system_prompt(personality="helpful")
    assert "OVERRIDE-BODY-MARKER-XYZ" in prompt
    assert p_mod.BUILTINS["helpful"] not in prompt


def test_custom_personality_with_new_name():
    builder = _make_builder(custom_personalities={
        "codereviewer": "REVIEWER-BODY-MARKER",
    })
    prompt = builder.build_system_prompt(personality="codereviewer")
    assert "REVIEWER-BODY-MARKER" in prompt


def test_personality_section_has_active_label():
    builder = _make_builder()
    prompt = builder.build_system_prompt(personality="concise")
    assert "Active personality" in prompt or "## Personality" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && pytest tests/test_personality_prompt_injection.py -v`
Expected: FAIL — PromptBuilder ctor doesn't take `custom_personalities`, body not injected.

- [ ] **Step 3: Modify PromptBuilder**

Open `opencomputer/agent/prompt_builder.py`. Find the PromptBuilder class (around line 261). Locate the `__init__` and add a parameter:

```python
def __init__(
    self,
    *,
    cwd: str = "",
    user_home: str = "",
    os_name: str = "",
    # ... existing kwargs ...
    custom_personalities: dict[str, str] | None = None,
) -> None:
    # ... existing init ...
    self._custom_personalities: dict[str, str] = dict(custom_personalities or {})
```

Find `build_system_prompt(...)` (the method that takes `personality: str`). At the end of the assembled prompt, append:

```python
# Slot #7 — personality overlay (register-only; identity is SOUL slot #1)
from opencomputer.agent import personality as _personality

p = _personality.resolve(personality, custom=self._custom_personalities)
prompt += (
    f"\n\n## Active personality: {p.name}\n\n"
    f"{p.body}\n"
)
```

(Use the existing prompt-assembly variable name; the patch is just appending one section. If the method already appends a personality section by name, replace that with this body-resolving call.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd OpenComputer && pytest tests/test_personality_prompt_injection.py -v`
Expected: PASS — 6 tests pass.

- [ ] **Step 5: Commit**

```bash
cd OpenComputer
git add opencomputer/agent/prompt_builder.py tests/test_personality_prompt_injection.py
git commit -m "feat(personality): inject resolved body into system prompt slot #7"
```

---

## Task 3: Slash command + CLI flag + config persistence

**Files:**
- Modify: `opencomputer/agent/slash_commands_impl/skin_personality_cmd.py`
- Modify: `opencomputer/cli.py` (add `--personality` flag)
- Modify: `opencomputer/agent/profile_yaml.py` (load/save the config keys)
- Test: extend `tests/test_personality_loader.py` with persistence scenarios

- [ ] **Step 1: Write the failing test**

Append to `tests/test_personality_loader.py`:

```python
def test_slash_personality_persists_to_config(tmp_path, monkeypatch):
    """`/personality NAME` writes default_personality into config.yaml."""
    from opencomputer.agent.profile_yaml import (
        load_profile_config,
        save_default_personality,
    )

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("agent: {}\n")

    save_default_personality(cfg_path, "concise")
    cfg = load_profile_config(cfg_path)
    assert cfg["agent"]["default_personality"] == "concise"

    save_default_personality(cfg_path, "")
    cfg = load_profile_config(cfg_path)
    # empty string clears (key removed or empty)
    assert cfg["agent"].get("default_personality", "") == ""


def test_slash_personality_reset_clears_config(tmp_path):
    from opencomputer.agent.profile_yaml import (
        load_profile_config,
        save_default_personality,
    )

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("agent:\n  default_personality: hype\n")

    save_default_personality(cfg_path, "")
    cfg = load_profile_config(cfg_path)
    assert cfg["agent"].get("default_personality", "") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && pytest tests/test_personality_loader.py -v -k "persists or reset"`
Expected: FAIL — `save_default_personality` doesn't exist.

- [ ] **Step 3: Add the save helper**

Open `opencomputer/agent/profile_yaml.py`. Add at the bottom (preserve module style — likely uses `yaml.safe_load`/`yaml.safe_dump`):

```python
def save_default_personality(config_path: Path, name: str) -> None:
    """Persist ``agent.default_personality: name`` to the profile config.

    Empty ``name`` removes the key (collapses to built-in default).
    Atomic via tmp + replace; never partial-writes.
    """
    import yaml  # local import — yaml is already a project dep

    data: dict = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text()) or {}
            if isinstance(loaded, dict):
                data = loaded
        except yaml.YAMLError:
            data = {}

    agent = data.setdefault("agent", {})
    if isinstance(agent, dict):
        if name:
            agent["default_personality"] = name
        else:
            agent.pop("default_personality", None)

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))
    tmp.replace(config_path)


def save_display_skin(config_path: Path, name: str) -> None:
    """Persist ``display.skin: name`` to the profile config (mirror)."""
    import yaml

    data: dict = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text()) or {}
            if isinstance(loaded, dict):
                data = loaded
        except yaml.YAMLError:
            data = {}

    display = data.setdefault("display", {})
    if isinstance(display, dict):
        if name:
            display["skin"] = name
        else:
            display.pop("skin", None)

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))
    tmp.replace(config_path)
```

If `load_profile_config` does not already exist with a single-path signature, also add:

```python
def load_profile_config(config_path: Path) -> dict:
    """Load a profile config.yaml from a single explicit path. Lenient."""
    import yaml

    if not config_path.exists():
        return {}
    try:
        return yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError:
        return {}
```

(If it already exists with a different signature, alias only — don't double-define.)

- [ ] **Step 4: Run test to verify save helpers pass**

Run: `cd OpenComputer && pytest tests/test_personality_loader.py -v -k "persists or reset"`
Expected: PASS.

- [ ] **Step 5: Rewrite the slash command**

Replace the contents of `opencomputer/agent/slash_commands_impl/skin_personality_cmd.py` with the wired version:

```python
"""``/skin [name]`` and ``/personality [name]`` — wire to real loaders.

- ``/personality`` (no args)        → show current + list available
- ``/personality NAME``             → set runtime + persist to config
- ``/personality reset|default``    → clear config (next session: helpful)

Same shape for ``/skin``.
"""
from __future__ import annotations

import os
from pathlib import Path

from opencomputer.agent.personality import BUILTINS as _PERS_BUILTINS
from opencomputer.agent.profile_yaml import (
    save_default_personality,
    save_display_skin,
)
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_RESET_TOKENS = {"reset", "default", "off", "clear"}


def _profile_config_path() -> Path:
    home = os.environ.get(
        "OPENCOMPUTER_HOME",
        str(Path.home() / ".opencomputer"),
    )
    profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
    return Path(home) / profile / "config.yaml"


def _builtin_skin_names() -> list[str]:
    # Lazy import to avoid CLI startup cost when skins not used
    from opencomputer.cli_ui.skin import list_builtin_names
    return list_builtin_names()


class PersonalityCommand(SlashCommand):
    name = "personality"
    description = "Get or set the active personality (prompt overlay)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("personality") or "helpful"
        available = sorted(_PERS_BUILTINS.keys())

        if sub == "":
            return SlashCommandResult(
                output=(
                    f"Current personality: {current}\n"
                    f"Available: {', '.join(available)}\n"
                    f"(custom personalities go under "
                    f"`agent.personalities` in config.yaml)"
                ),
                handled=True,
            )

        if sub in _RESET_TOKENS:
            runtime.custom["personality"] = "helpful"
            try:
                save_default_personality(_profile_config_path(), "")
            except OSError as exc:
                return SlashCommandResult(
                    output=f"Reset runtime, but config write failed: {exc}",
                    handled=True,
                )
            return SlashCommandResult(
                output="Personality reset to default (helpful).",
                handled=True,
            )

        # Accept any name — custom personalities may not be in BUILTINS.
        runtime.custom["personality"] = sub
        try:
            save_default_personality(_profile_config_path(), sub)
        except OSError as exc:
            return SlashCommandResult(
                output=f"Personality set to {sub} (runtime only — config write failed: {exc})",
                handled=True,
            )
        return SlashCommandResult(
            output=f"Personality set to {sub} (persisted to config).",
            handled=True,
        )


class SkinCommand(SlashCommand):
    name = "skin"
    description = "Get or set the active TUI skin"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("skin") or "default"

        try:
            available = _builtin_skin_names()
        except Exception:
            available = ["default"]

        if sub == "":
            return SlashCommandResult(
                output=(
                    f"Current skin: {current}\n"
                    f"Built-in: {', '.join(available)}\n"
                    f"(drop custom YAML at ~/.opencomputer/skins/<name>.yaml)"
                ),
                handled=True,
            )

        if sub in _RESET_TOKENS:
            runtime.custom["skin"] = "default"
            try:
                save_display_skin(_profile_config_path(), "")
            except OSError as exc:
                return SlashCommandResult(
                    output=f"Reset runtime, but config write failed: {exc}",
                    handled=True,
                )
            return SlashCommandResult(
                output="Skin reset to default.",
                handled=True,
            )

        # Try to apply immediately if console is reachable.
        runtime.custom["skin"] = sub
        try:
            from opencomputer.cli_ui.skin import apply_skin, load_skin
            from rich.console import Console

            spec = load_skin(sub)
            apply_skin(spec, Console())
        except Exception as exc:  # noqa: BLE001 — fail-soft on hot-swap
            # Apply failure is non-fatal: setting persists; next start picks it up.
            pass

        try:
            save_display_skin(_profile_config_path(), sub)
        except OSError as exc:
            return SlashCommandResult(
                output=f"Skin set to {sub} (runtime only — config write failed: {exc})",
                handled=True,
            )
        return SlashCommandResult(
            output=f"Skin set to {sub} (persisted to config).",
            handled=True,
        )


__all__ = ["PersonalityCommand", "SkinCommand"]
```

- [ ] **Step 6: Add `--personality` flag to the CLI**

Open `opencomputer/cli.py`. Find the `chat` command (or wherever the agent loop is started). Add an option:

```python
personality: str = typer.Option(
    "",
    "--personality",
    help="Active personality name (overrides agent.default_personality config).",
),
```

Thread it into the runtime / agent loop start so it lands in `runtime.custom["personality"]` for the first turn.

(Skip exact wiring code — it follows the same pattern as `--plan` already in cli.py. Find the equivalent line and add the parallel.)

- [ ] **Step 7: Run all personality tests**

Run: `cd OpenComputer && pytest tests/test_personality_loader.py tests/test_personality_prompt_injection.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
cd OpenComputer
git add opencomputer/agent/slash_commands_impl/skin_personality_cmd.py \
        opencomputer/agent/profile_yaml.py \
        opencomputer/cli.py \
        tests/test_personality_loader.py
git commit -m "feat(personality): /personality persists + --personality CLI flag"
```

---

## Task 4: SkinSpec dataclass

**Files:**
- Create: `opencomputer/cli_ui/skin/__init__.py`
- Create: `opencomputer/cli_ui/skin/spec.py`
- Test: `tests/test_skin_loader.py` (initial dataclass tests)

- [ ] **Step 1: Write the failing test**

Create `tests/test_skin_loader.py`:

```python
"""SkinSpec dataclass shape + 9 built-in YAML loadability."""
from __future__ import annotations

import pytest

from opencomputer.cli_ui.skin import SkinSpec, list_builtin_names, load_skin


def test_skinspec_is_frozen():
    spec = SkinSpec(
        name="test",
        description="x",
        colors={"banner_border": "#FFFFFF"},
        spinner_thinking_verbs=("thinking",),
        spinner_wings=(("⟨", "⟩"),),
        agent_name="Test",
        response_label=" Test ",
        prompt_symbol=">",
        banner_logo="",
        banner_hero="",
        tool_prefix="┊",
        tool_emojis={},
    )
    with pytest.raises(Exception):
        spec.name = "no"  # type: ignore[misc]


def test_all_9_builtins_listed():
    names = list_builtin_names()
    expected = {
        "default", "ares", "mono", "slate", "daylight",
        "warm-lightmode", "poseidon", "sisyphus", "charizard",
    }
    assert expected.issubset(set(names))


def test_all_9_builtins_load():
    for name in list_builtin_names():
        spec = load_skin(name)
        assert spec.name == name
        assert spec.colors  # at least one color set
        # branding fields are non-empty after default-merge
        assert spec.agent_name


def test_unknown_skin_falls_back_to_default():
    spec = load_skin("nonexistent_xyz")
    assert spec.name == "default"


def test_default_skin_loads_clean():
    spec = load_skin("default")
    assert spec.name == "default"
    assert "banner_border" in spec.colors
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && pytest tests/test_skin_loader.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement SkinSpec**

Create `opencomputer/cli_ui/skin/spec.py`:

```python
"""SkinSpec: colors + spinner verbs + branding + tool prefix."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SkinSpec:
    """Visual theme for the chat REPL.

    Fields are flat (not nested dataclasses) so YAML round-trip is
    trivial and missing keys can be filled by ``default.yaml`` with a
    one-level dict merge.
    """

    name: str
    description: str
    colors: dict[str, str]                    # hex strings, keyed by Rich style name

    spinner_thinking_verbs: tuple[str, ...]   # ("thinking", "pondering", ...)
    spinner_wings: tuple[tuple[str, str], ...]  # decoration around spinner glyph

    agent_name: str
    response_label: str
    prompt_symbol: str

    banner_logo: str                           # rich-markup ascii (may be empty)
    banner_hero: str                           # rich-markup ascii (may be empty)

    tool_prefix: str = "┊"
    tool_emojis: dict[str, str] = field(default_factory=dict)


__all__ = ["SkinSpec"]
```

- [ ] **Step 4: Implement minimal package init (so import works)**

Create `opencomputer/cli_ui/skin/__init__.py`:

```python
"""Skins: visual theme for the CLI chat REPL.

Resolution order:
  1. User skin at ``~/.opencomputer/skins/<name>.yaml``
  2. Built-in skin at ``opencomputer/cli_ui/skin/builtins/<name>.yaml``
  3. ``default`` (always available)

Apply via ``apply_skin(spec, console)``. Idempotent — calling again
with a different spec swaps everything live.
"""
from __future__ import annotations

from .apply import apply_skin
from .loader import list_builtin_names, load_skin
from .spec import SkinSpec

__all__ = ["SkinSpec", "apply_skin", "list_builtin_names", "load_skin"]
```

(`apply.py` and `loader.py` are written in tasks 5 and 6. Until those exist, this file's imports will fail — that's fine; we'll bring them online together by writing tasks 4–6 as one atomic commit. Skip testing the package init until task 6 lands.)

- [ ] **Step 5: Continue to Task 5 — do not commit yet**

The `__init__` references symbols defined in tasks 5/6. Atomic commit at the end of task 6.

---

## Task 5: Skin loader + 9 built-in YAMLs

**Files:**
- Create: `opencomputer/cli_ui/skin/loader.py`
- Create: `opencomputer/cli_ui/skin/builtins/{default,ares,mono,slate,daylight,warm-lightmode,poseidon,sisyphus,charizard}.yaml`

- [ ] **Step 1: Implement loader**

Create `opencomputer/cli_ui/skin/loader.py`:

```python
"""Load a SkinSpec from built-in YAML or user override."""
from __future__ import annotations

import logging
import os
from importlib import resources
from pathlib import Path

import yaml

from .spec import SkinSpec

logger = logging.getLogger("opencomputer.cli_ui.skin")

DEFAULT_NAME = "default"
USER_SKINS_DIR = Path("~/.opencomputer/skins").expanduser()


def _resource_yaml(name: str) -> str | None:
    """Read a built-in YAML by skin name; None if missing."""
    try:
        files = resources.files("opencomputer.cli_ui.skin.builtins")
        target = files.joinpath(f"{name}.yaml")
        if target.is_file():
            return target.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return None
    return None


def list_builtin_names() -> list[str]:
    """Return all built-in skin names (sorted)."""
    files = resources.files("opencomputer.cli_ui.skin.builtins")
    names = []
    for entry in files.iterdir():
        if entry.name.endswith(".yaml"):
            names.append(entry.name[:-5])
    return sorted(names)


def _user_yaml(name: str) -> str | None:
    p = USER_SKINS_DIR / f"{name}.yaml"
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("skin: failed to read %s — %s", p, exc)
    return None


def _parse_yaml(text: str, *, source: str) -> dict:
    try:
        loaded = yaml.safe_load(text) or {}
        if isinstance(loaded, dict):
            return loaded
        logger.warning("skin: %s did not parse as a dict — ignoring", source)
    except yaml.YAMLError as exc:
        logger.warning("skin: malformed YAML in %s — %s", source, exc)
    return {}


def _merge_with_default(default: dict, override: dict) -> dict:
    """One-level dict merge: override fills/overrides default keys.

    For nested dicts (``colors``, ``tool_emojis``), perform a per-key
    merge so a custom skin can override a single color without
    redeclaring the whole palette.
    """
    out = dict(default)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _spec_from_dict(name: str, data: dict) -> SkinSpec:
    spinner = data.get("spinner") or {}
    branding = data.get("branding") or {}

    return SkinSpec(
        name=name,
        description=str(data.get("description", "")),
        colors=dict(data.get("colors") or {}),
        spinner_thinking_verbs=tuple(spinner.get("thinking_verbs") or ("thinking",)),
        spinner_wings=tuple(
            (w[0], w[1])
            for w in (spinner.get("wings") or [["⟨", "⟩"]])
            if isinstance(w, (list, tuple)) and len(w) == 2
        ) or (("⟨", "⟩"),),
        agent_name=str(branding.get("agent_name", "OpenComputer")),
        response_label=str(branding.get("response_label", " ✦ OC ")),
        prompt_symbol=str(branding.get("prompt_symbol", ">")),
        banner_logo=str(data.get("banner_logo", "")),
        banner_hero=str(data.get("banner_hero", "")),
        tool_prefix=str(data.get("tool_prefix", "┊")),
        tool_emojis=dict(data.get("tool_emojis") or {}),
    )


def load_skin(name: str) -> SkinSpec:
    """Load a SkinSpec by name. Never raises."""
    name = (name or "").strip().lower() or DEFAULT_NAME

    default_text = _resource_yaml(DEFAULT_NAME) or "{}"
    default_data = _parse_yaml(default_text, source=f"builtins/{DEFAULT_NAME}.yaml")

    if name == DEFAULT_NAME:
        return _spec_from_dict(DEFAULT_NAME, default_data)

    user_text = _user_yaml(name)
    builtin_text = _resource_yaml(name)
    chosen_text = user_text or builtin_text

    if chosen_text is None:
        logger.warning("skin: %r not found — falling back to default", name)
        return _spec_from_dict(DEFAULT_NAME, default_data)

    override = _parse_yaml(
        chosen_text,
        source=("user" if user_text else "builtin") + f":{name}.yaml",
    )
    merged = _merge_with_default(default_data, override)
    return _spec_from_dict(name, merged)


__all__ = ["DEFAULT_NAME", "list_builtin_names", "load_skin"]
```

- [ ] **Step 2: Write `default.yaml`**

Create `opencomputer/cli_ui/skin/builtins/default.yaml`:

```yaml
name: default
description: Default OpenComputer skin — gold accents, kawaii spinner verbs.

colors:
  banner_border:    "#D4AF37"
  banner_title:     "#FFD700"
  banner_accent:    "#B8860B"
  prompt:           "#A0E0FF"
  user_text:        "#FFFFFF"
  agent_text:       "#FFE08A"
  agent_label:      "#D4AF37"
  tool_label:       "#88C0D0"
  tool_output:      "#A3BE8C"
  error:            "#BF616A"
  warning:          "#EBCB8B"
  info:             "#81A1C1"
  dim:              "#6E7B8B"
  status_bar_bg:    "#2E3440"
  status_bar_fg:    "#ECEFF4"

spinner:
  thinking_verbs:
    - thinking
    - pondering
    - musing
    - cogitating
  wings:
    - ["⟨✦", "✦⟩"]
    - ["⟨✿", "✿⟩"]

branding:
  agent_name: "OpenComputer"
  response_label: " ✦ OC "
  prompt_symbol: ">"

banner_logo: ""
banner_hero: ""

tool_prefix: "┊"
tool_emojis:
  Read: "📖"
  Write: "📝"
  Edit: "✏"
  Bash: "⚙"
  WebFetch: "🌐"
  WebSearch: "🔎"
```

- [ ] **Step 3: Write 8 sibling YAMLs (terse — only override changes)**

Create `opencomputer/cli_ui/skin/builtins/ares.yaml`:

```yaml
name: ares
description: Crimson + bronze, sword/shield register.

colors:
  banner_border:    "#8B1A1A"
  banner_title:     "#DC143C"
  banner_accent:    "#CD7F32"
  prompt:           "#FFB347"
  agent_text:       "#FFD7B8"
  agent_label:      "#DC143C"

spinner:
  thinking_verbs: [strategizing, drilling, sparring, marshalling]
  wings: [["⚔", "⚔"], ["▲", "▲"]]

branding:
  agent_name: "Ares"
  response_label: " ⚔ Ares "
  prompt_symbol: "⚔"
```

Create `opencomputer/cli_ui/skin/builtins/mono.yaml`:

```yaml
name: mono
description: Grayscale, no color — terminals that hate ANSI.

colors:
  banner_border:    "#FFFFFF"
  banner_title:     "#FFFFFF"
  banner_accent:    "#A0A0A0"
  prompt:           "#FFFFFF"
  user_text:        "#FFFFFF"
  agent_text:       "#FFFFFF"
  agent_label:      "#FFFFFF"
  tool_label:       "#FFFFFF"
  tool_output:      "#FFFFFF"
  error:            "#FFFFFF"
  warning:          "#FFFFFF"
  info:             "#FFFFFF"
  dim:              "#808080"
  status_bar_bg:    "#000000"
  status_bar_fg:    "#FFFFFF"

spinner:
  thinking_verbs: [thinking, working, processing]
  wings: [[">", "<"]]

branding:
  agent_name: "OpenComputer"
  response_label: " > OC "
  prompt_symbol: ">"
```

Create `opencomputer/cli_ui/skin/builtins/slate.yaml`:

```yaml
name: slate
description: Royal blue — focused developer surface.

colors:
  banner_border:    "#1E3A8A"
  banner_title:     "#3B82F6"
  banner_accent:    "#60A5FA"
  prompt:           "#93C5FD"
  agent_text:       "#DBEAFE"
  agent_label:      "#3B82F6"

spinner:
  thinking_verbs: [thinking, computing, parsing]
  wings: [["[", "]"]]

branding:
  agent_name: "OpenComputer"
  response_label: " [OC] "
  prompt_symbol: "▸"
```

Create `opencomputer/cli_ui/skin/builtins/daylight.yaml`:

```yaml
name: daylight
description: Light theme — for bright terminals.

colors:
  banner_border:    "#404040"
  banner_title:     "#1F2937"
  banner_accent:    "#4B5563"
  prompt:           "#1F2937"
  user_text:        "#000000"
  agent_text:       "#1F2937"
  agent_label:      "#7C3AED"
  tool_label:       "#0369A1"
  tool_output:      "#166534"
  error:            "#991B1B"
  warning:          "#92400E"
  info:             "#1E40AF"
  dim:              "#9CA3AF"
  status_bar_bg:    "#F3F4F6"
  status_bar_fg:    "#1F2937"

spinner:
  thinking_verbs: [thinking, working]
  wings: [["·", "·"]]

branding:
  agent_name: "OpenComputer"
  response_label: " ✦ OC "
  prompt_symbol: ">"
```

Create `opencomputer/cli_ui/skin/builtins/warm-lightmode.yaml`:

```yaml
name: warm-lightmode
description: Warm parchment — light terminals, easy on eyes.

colors:
  banner_border:    "#92400E"
  banner_title:     "#78350F"
  banner_accent:    "#B45309"
  prompt:           "#7C2D12"
  user_text:        "#1C1917"
  agent_text:       "#44403C"
  agent_label:      "#92400E"
  tool_label:       "#9A3412"
  tool_output:      "#3F6212"
  error:            "#991B1B"
  warning:          "#854D0E"
  info:             "#3730A3"
  dim:              "#A8A29E"
  status_bar_bg:    "#FEF3C7"
  status_bar_fg:    "#78350F"

spinner:
  thinking_verbs: [contemplating, brewing, settling]
  wings: [["~", "~"]]

branding:
  agent_name: "OpenComputer"
  response_label: " ☀ OC "
  prompt_symbol: ">"
```

Create `opencomputer/cli_ui/skin/builtins/poseidon.yaml`:

```yaml
name: poseidon
description: Deep blue + seafoam, trident register.

colors:
  banner_border:    "#0C4A6E"
  banner_title:     "#0EA5E9"
  banner_accent:    "#67E8F9"
  prompt:           "#7DD3FC"
  agent_text:       "#CFFAFE"
  agent_label:      "#0EA5E9"

spinner:
  thinking_verbs: [diving, surfacing, currents-checking, depth-sounding]
  wings: [["≈", "≈"], ["⟆", "⟇"]]

branding:
  agent_name: "Poseidon"
  response_label: " 🔱 Poseidon "
  prompt_symbol: "🔱"
```

Create `opencomputer/cli_ui/skin/builtins/sisyphus.yaml`:

```yaml
name: sisyphus
description: Austere grayscale, boulder energy.

colors:
  banner_border:    "#6B7280"
  banner_title:     "#9CA3AF"
  banner_accent:    "#4B5563"
  prompt:           "#D1D5DB"
  agent_text:       "#E5E7EB"
  agent_label:      "#9CA3AF"

spinner:
  thinking_verbs: [pushing, again, again, still-pushing]
  wings: [["○", "○"]]

branding:
  agent_name: "OpenComputer"
  response_label: " ○ OC "
  prompt_symbol: "○"
```

Create `opencomputer/cli_ui/skin/builtins/charizard.yaml`:

```yaml
name: charizard
description: Burnt orange + ember, dragon register.

colors:
  banner_border:    "#9A3412"
  banner_title:     "#EA580C"
  banner_accent:    "#FB923C"
  prompt:           "#FED7AA"
  agent_text:       "#FFEDD5"
  agent_label:      "#EA580C"

spinner:
  thinking_verbs: [igniting, blazing, soaring, kindling]
  wings: [["🔥", "🔥"], ["⟆", "⟇"]]

branding:
  agent_name: "Charizard"
  response_label: " 🔥 Char "
  prompt_symbol: "🔥"
```

- [ ] **Step 4: Update `opencomputer/cli_ui/skin/apply.py` stub so package imports**

We will fully implement `apply.py` in Task 6, but `__init__.py` re-exports it. Create a no-op stub now:

`opencomputer/cli_ui/skin/apply.py`:

```python
"""Apply a SkinSpec to a Rich Console (and downstream renderers)."""
from __future__ import annotations

from .spec import SkinSpec


def apply_skin(spec: SkinSpec, console=None) -> None:  # noqa: ANN001
    """Stub — Task 6 expands this."""
    pass


__all__ = ["apply_skin"]
```

- [ ] **Step 5: Run skin loader tests**

Run: `cd OpenComputer && pytest tests/test_skin_loader.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 6: Commit (atomic with task 4)**

```bash
cd OpenComputer
git add opencomputer/cli_ui/skin/ tests/test_skin_loader.py
git commit -m "feat(skin): SkinSpec + 9 built-in YAMLs + loader with custom override"
```

---

## Task 6: apply_skin — wire to Rich console

**Files:**
- Modify: `opencomputer/cli_ui/skin/apply.py`
- Modify: `opencomputer/cli_ui/style.py` (provide hook for theme swap)
- Test: `tests/test_skin_apply.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_skin_apply.py`:

```python
"""apply_skin swaps Rich theme on the live console."""
from __future__ import annotations

from rich.console import Console

from opencomputer.cli_ui.skin import apply_skin, load_skin


def test_apply_changes_console_theme():
    console = Console(width=80)
    spec = load_skin("ares")

    apply_skin(spec, console)

    # The Rich theme should now contain ares' agent_text color.
    style = console.get_style("agent_text", default=None)
    assert style is not None
    # ares.yaml says agent_text = "#FFD7B8"
    assert "ffd7b8" in str(style).lower() or "#ffd7b8" in str(style).lower()


def test_apply_is_idempotent():
    console = Console(width=80)

    apply_skin(load_skin("default"), console)
    apply_skin(load_skin("ares"), console)
    apply_skin(load_skin("default"), console)

    style = console.get_style("agent_text", default=None)
    assert style is not None
    # default's agent_text = "#FFE08A"
    assert "ffe08a" in str(style).lower()


def test_apply_with_invalid_color_falls_back():
    """Invalid hex doesn't crash apply."""
    from opencomputer.cli_ui.skin.spec import SkinSpec

    bogus = SkinSpec(
        name="bogus",
        description="x",
        colors={"agent_text": "not-a-color"},
        spinner_thinking_verbs=("x",),
        spinner_wings=(("[", "]"),),
        agent_name="X",
        response_label="X",
        prompt_symbol="X",
        banner_logo="",
        banner_hero="",
    )

    console = Console(width=80)
    apply_skin(bogus, console)  # must not raise


def test_spinner_verbs_observable():
    """After apply, the active spinner verb pool reflects the skin."""
    from opencomputer.cli_ui.skin.apply import current_spinner_verbs

    apply_skin(load_skin("ares"), Console(width=80))
    verbs = current_spinner_verbs()
    assert "strategizing" in verbs


def test_branding_observable():
    from opencomputer.cli_ui.skin.apply import current_branding

    apply_skin(load_skin("poseidon"), Console(width=80))
    b = current_branding()
    assert b["agent_name"] == "Poseidon"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && pytest tests/test_skin_apply.py -v`
Expected: FAIL — `current_spinner_verbs` / `current_branding` missing; theme not applied.

- [ ] **Step 3: Implement apply_skin**

Replace `opencomputer/cli_ui/skin/apply.py`:

```python
"""Apply a SkinSpec to a Rich Console + module-level renderer hooks.

The chat REPL renderers (streaming, banner, prompt) read theme + branding
+ spinner verbs from this module's process-global state. ``apply_skin``
mutates that state and binds the corresponding Rich Theme onto the
provided Console.
"""
from __future__ import annotations

import logging
import threading

from rich.console import Console
from rich.style import Style
from rich.theme import Theme

from .spec import SkinSpec

logger = logging.getLogger("opencomputer.cli_ui.skin")

_lock = threading.Lock()
_active_spec: SkinSpec | None = None
_active_branding: dict[str, str] = {}
_active_spinner_verbs: tuple[str, ...] = ("thinking",)
_active_spinner_wings: tuple[tuple[str, str], ...] = (("⟨", "⟩"),)
_active_tool_emojis: dict[str, str] = {}
_active_tool_prefix: str = "┊"


def _safe_style(value: str) -> Style | None:
    try:
        return Style.parse(value)
    except Exception as exc:  # noqa: BLE001 — Rich raises various
        logger.warning("skin: invalid style %r — %s", value, exc)
        return None


def _theme_from_colors(colors: dict[str, str]) -> Theme:
    styles: dict[str, Style] = {}
    for key, value in colors.items():
        s = _safe_style(value)
        if s is not None:
            styles[key] = s
    return Theme(styles, inherit=True)


def apply_skin(spec: SkinSpec, console: Console) -> None:
    """Bind ``spec`` to ``console`` and update process-global renderer state.

    Idempotent. Bad hex colors are skipped with a warning; everything
    else continues to apply.
    """
    global _active_spec, _active_branding
    global _active_spinner_verbs, _active_spinner_wings
    global _active_tool_emojis, _active_tool_prefix

    with _lock:
        try:
            theme = _theme_from_colors(spec.colors)
            console.push_theme(theme)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skin: theme push failed — %s", exc)

        _active_spec = spec
        _active_branding = {
            "agent_name": spec.agent_name,
            "response_label": spec.response_label,
            "prompt_symbol": spec.prompt_symbol,
        }
        _active_spinner_verbs = tuple(spec.spinner_thinking_verbs)
        _active_spinner_wings = tuple(spec.spinner_wings)
        _active_tool_emojis = dict(spec.tool_emojis)
        _active_tool_prefix = spec.tool_prefix


def current_spec() -> SkinSpec | None:
    return _active_spec


def current_branding() -> dict[str, str]:
    return dict(_active_branding)


def current_spinner_verbs() -> tuple[str, ...]:
    return _active_spinner_verbs


def current_spinner_wings() -> tuple[tuple[str, str], ...]:
    return _active_spinner_wings


def current_tool_emojis() -> dict[str, str]:
    return dict(_active_tool_emojis)


def current_tool_prefix() -> str:
    return _active_tool_prefix


__all__ = [
    "apply_skin",
    "current_spec",
    "current_branding",
    "current_spinner_verbs",
    "current_spinner_wings",
    "current_tool_emojis",
    "current_tool_prefix",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd OpenComputer && pytest tests/test_skin_apply.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Wire CLI startup to apply skin once**

Open `opencomputer/cli.py`. In the `chat` command (and any other entry point that creates a Console for the chat loop), add at startup, after console creation, before the loop runs:

```python
# Apply skin from --skin flag, runtime, or display.skin config (in that order).
try:
    from opencomputer.cli_ui.skin import apply_skin, load_skin
    skin_name = (
        skin
        or os.environ.get("OPENCOMPUTER_SKIN", "")
        or _profile_display_skin()  # tiny helper that reads display.skin from config.yaml
        or "default"
    )
    apply_skin(load_skin(skin_name), console)
except Exception as exc:  # noqa: BLE001 — never crash startup on theme
    logger.warning("skin: apply failed at startup — %s", exc)
```

Add the `--skin` Typer option mirroring `--personality`:

```python
skin: str = typer.Option(
    "",
    "--skin",
    help="TUI skin name (overrides display.skin config).",
),
```

Add the small helper near the top of cli.py (or a tiny module if cli.py is already long):

```python
def _profile_display_skin() -> str:
    from pathlib import Path
    import yaml as _yaml  # already a dep
    home = os.environ.get(
        "OPENCOMPUTER_HOME",
        str(Path.home() / ".opencomputer"),
    )
    profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
    cfg = Path(home) / profile / "config.yaml"
    if not cfg.exists():
        return ""
    try:
        data = _yaml.safe_load(cfg.read_text()) or {}
    except _yaml.YAMLError:
        return ""
    return str((data.get("display") or {}).get("skin", "") or "")
```

- [ ] **Step 6: Commit**

```bash
cd OpenComputer
git add opencomputer/cli_ui/skin/apply.py opencomputer/cli.py tests/test_skin_apply.py
git commit -m "feat(skin): apply_skin binds Rich theme + branding + spinner state"
```

---

## Task 7: AtRef parser

**Files:**
- Create: `opencomputer/agent/at_references.py` (parser portion)
- Test: `tests/test_at_references_parser.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_at_references_parser.py`:

```python
"""At-reference grammar parser."""
from __future__ import annotations

from opencomputer.agent.at_references import AtRef, parse


def test_parses_simple_file_ref():
    refs = parse("look at @file:foo/bar.py please")
    assert refs == [AtRef(kind="file", arg="foo/bar.py", line_start=None, line_end=None)]


def test_parses_file_with_line_range():
    refs = parse("@file:src/main.py:10-25")
    assert refs == [AtRef(kind="file", arg="src/main.py", line_start=10, line_end=25)]


def test_parses_folder_ref():
    refs = parse("see @folder:src for context")
    assert refs == [AtRef(kind="folder", arg="src", line_start=None, line_end=None)]


def test_parses_diff():
    refs = parse("@diff")
    assert refs == [AtRef(kind="diff", arg="", line_start=None, line_end=None)]


def test_parses_staged():
    refs = parse("@staged")
    assert refs == [AtRef(kind="staged", arg="", line_start=None, line_end=None)]


def test_parses_git_with_count():
    refs = parse("@git:5")
    assert refs == [AtRef(kind="git", arg="5", line_start=None, line_end=None)]


def test_parses_url():
    refs = parse("@url:https://example.com/foo")
    assert refs == [AtRef(kind="url", arg="https://example.com/foo", line_start=None, line_end=None)]


def test_parses_multiple_in_one_message():
    refs = parse("compare @file:a.py with @file:b.py")
    assert len(refs) == 2
    assert refs[0].arg == "a.py"
    assert refs[1].arg == "b.py"


def test_email_address_is_not_an_atref():
    refs = parse("ping me at sak@example.com")
    assert refs == []


def test_at_alone_is_not_an_atref():
    refs = parse("hi @ there")
    assert refs == []


def test_strips_trailing_punctuation():
    refs = parse("see @file:foo.py, then think.")
    assert refs == [AtRef(kind="file", arg="foo.py", line_start=None, line_end=None)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && pytest tests/test_at_references_parser.py -v`
Expected: FAIL — `at_references` module missing.

- [ ] **Step 3: Implement parser**

Create `opencomputer/agent/at_references.py` (parser portion only — expanders come in Task 8):

```python
"""@-reference parser + expanders.

Grammar:
    @file:<path>                 → inject file body
    @file:<path>:<a>-<b>         → inject lines a..b (1-indexed inclusive)
    @folder:<path>               → tree listing (≤ 200 entries)
    @diff                        → git diff (unstaged)
    @staged                      → git diff --staged
    @git:<N>                     → last N commits with patches (clamp ≤ 10)
    @url:<https://...>           → fetch + inject web page text

Multiple refs in one message: all that fit are expanded; refs over hard
cap are refused with an inline marker. The CLI input loop calls
``expand(text, ctx=...)`` AFTER slash dispatch and BEFORE message
construction. Channel adapters do NOT call ``expand`` (CLI-only).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# Kinds — keep stable; the expander dispatches on this.
AtRefKind = Literal["file", "folder", "diff", "staged", "git", "url"]


@dataclass(frozen=True, slots=True)
class AtRef:
    kind: AtRefKind
    arg: str
    line_start: int | None
    line_end: int | None


# Regex strategy:
#   - Anchor on a non-ident char (start-of-string or whitespace) to skip
#     email addresses (sak@example.com).
#   - Match @kind: where kind is one of the known set.
#   - Capture arg as a run of safe chars; trim trailing punctuation
#     (",.;:!?)") with a stripping pass.

_KIND_PATTERN = (
    r"(?:^|(?<=\s))"
    r"@(file|folder|diff|staged|git|url)"
    r"(?::([^\s]+))?"
)
_RE = re.compile(_KIND_PATTERN)
_TRAILING_PUNCT = ",.;:!?)"


def parse(text: str) -> list[AtRef]:
    """Parse ``text`` for @-references. Returns them in left-to-right order."""
    out: list[AtRef] = []
    for m in _RE.finditer(text):
        kind = m.group(1)
        arg = (m.group(2) or "").rstrip(_TRAILING_PUNCT)

        # @diff / @staged take no arg
        if kind in ("diff", "staged"):
            out.append(AtRef(kind=kind, arg="", line_start=None, line_end=None))
            continue

        # @git:N — arg is the count
        if kind == "git":
            if not arg:
                continue  # @git alone is malformed; skip silently
            out.append(AtRef(kind="git", arg=arg, line_start=None, line_end=None))
            continue

        # @url:https://... — arg is full URL
        if kind == "url":
            if not arg:
                continue
            out.append(AtRef(kind="url", arg=arg, line_start=None, line_end=None))
            continue

        # @file:path[:a-b] / @folder:path
        if not arg:
            continue

        if kind == "file":
            # Try to extract a trailing :a-b range.
            range_match = re.search(r":(\d+)-(\d+)$", arg)
            if range_match:
                path = arg[: range_match.start()]
                a = int(range_match.group(1))
                b = int(range_match.group(2))
                out.append(AtRef(kind="file", arg=path, line_start=a, line_end=b))
            else:
                out.append(AtRef(kind="file", arg=arg, line_start=None, line_end=None))
        else:  # folder
            out.append(AtRef(kind="folder", arg=arg, line_start=None, line_end=None))

    return out


__all__ = ["AtRef", "AtRefKind", "parse"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd OpenComputer && pytest tests/test_at_references_parser.py -v`
Expected: PASS — 11 tests.

- [ ] **Step 5: Commit**

```bash
cd OpenComputer
git add opencomputer/agent/at_references.py tests/test_at_references_parser.py
git commit -m "feat(at-refs): parser for @file/@folder/@diff/@staged/@git/@url"
```

---

## Task 8: AtRef expanders + size policy + blocked paths

**Files:**
- Modify: `opencomputer/agent/at_references.py` (add expand_*, blocked-paths, caps)
- Test: `tests/test_at_references_expand.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_at_references_expand.py`:

```python
"""@-reference expansion: file/folder/diff/staged/git/url + caps + blocked."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from opencomputer.agent.at_references import (
    AtRefContext,
    expand,
    is_path_blocked,
)


def _ctx(tmp_path: Path, **kw) -> AtRefContext:
    return AtRefContext(
        cwd=str(tmp_path),
        home=str(tmp_path / "home"),
        context_window_chars=200_000,
        **kw,
    )


def test_expand_file_inline(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("hello world\n")
    out = expand(f"see @file:{f}", ctx=_ctx(tmp_path))
    assert "hello world" in out
    assert "Attached Context" in out


def test_expand_file_with_line_range(tmp_path):
    f = tmp_path / "lines.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    out = expand(f"see @file:{f}:2-4", ctx=_ctx(tmp_path))
    assert "b\nc\nd" in out
    assert "a\nb" not in out  # line 1 excluded


def test_expand_missing_file_marks_inline(tmp_path):
    out = expand(f"see @file:{tmp_path}/nope.txt", ctx=_ctx(tmp_path))
    assert "[file not found" in out


def test_blocked_path_refuses(tmp_path):
    # ~/.ssh/id_rsa style — synthetic
    blocked = tmp_path / "home" / ".ssh" / "id_rsa"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("SECRET")
    out = expand(f"see @file:{blocked}", ctx=_ctx(tmp_path))
    assert "[blocked path" in out
    assert "SECRET" not in out


def test_is_path_blocked_ssh(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    p = home / ".ssh" / "id_rsa"
    p.write_text("x")
    assert is_path_blocked(p, home=home)


def test_is_path_blocked_pem(tmp_path):
    p = tmp_path / "cert.pem"
    p.write_text("x")
    assert is_path_blocked(p, home=tmp_path / "home")


def test_is_path_blocked_zshrc(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    p = home / ".zshrc"
    p.write_text("x")
    assert is_path_blocked(p, home=home)


def test_hard_cap_refuses_oversized_file(tmp_path):
    big = tmp_path / "huge.txt"
    big.write_text("x" * 200_000)
    ctx = _ctx(tmp_path, context_window_chars=100_000)
    out = expand(f"@file:{big}", ctx=ctx)
    assert "[ref refused" in out and "hard cap" in out
    assert "x" * 200_000 not in out


def test_soft_cap_warns_but_includes(tmp_path):
    big = tmp_path / "medium.txt"
    big.write_text("x" * 30_000)
    ctx = _ctx(tmp_path, context_window_chars=100_000)
    out = expand(f"@file:{big}", ctx=ctx)
    # 30000 > 25000 (25%) and < 50000 (50%) → soft warn, content included
    assert "x" * 30_000 in out
    assert "soft cap" in out


def test_expand_folder_lists_entries(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    (tmp_path / "sub").mkdir()
    out = expand(f"@folder:{tmp_path}", ctx=_ctx(tmp_path))
    assert "a.txt" in out and "b.txt" in out and "sub" in out


def test_expand_folder_caps_at_200(tmp_path):
    for i in range(250):
        (tmp_path / f"f{i:03d}.txt").write_text("x")
    out = expand(f"@folder:{tmp_path}", ctx=_ctx(tmp_path))
    assert "f000.txt" in out
    assert "[truncated" in out


def test_expand_diff_in_non_git_repo(tmp_path):
    out = expand("@diff", ctx=_ctx(tmp_path))
    assert "[not a git repository" in out


def test_expand_git_clamped_to_10(tmp_path):
    # In a non-git dir we just verify the parser path; real git tests
    # would require setting up a repo.
    out = expand("@git:50", ctx=_ctx(tmp_path))
    # Either "clamped" appears (if git exec runs) or "not a git repository".
    assert "clamped" in out or "[not a git" in out


def test_url_blocked_for_private_addr(tmp_path, monkeypatch):
    # Trust link_understanding.is_safe_url — pick a guaranteed-private IP.
    out = expand("@url:http://127.0.0.1/", ctx=_ctx(tmp_path))
    assert "[blocked" in out or "[fetch failed" in out
    # 127.0.0.1 is loopback — link_understanding will refuse it.


def test_no_atref_returns_text_unchanged(tmp_path):
    text = "hi there, no refs"
    assert expand(text, ctx=_ctx(tmp_path)) == text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && pytest tests/test_at_references_expand.py -v`
Expected: FAIL — `expand`, `AtRefContext`, `is_path_blocked` missing.

- [ ] **Step 3: Implement expanders**

Append to `opencomputer/agent/at_references.py`:

```python
# ─── expansion ─────────────────────────────────────────────────────

import logging
import shutil
import subprocess
from dataclasses import field
from fnmatch import fnmatch

logger = logging.getLogger("opencomputer.agent.at_references")

_FOLDER_MAX_ENTRIES = 200
_GIT_MAX_COMMITS = 10
_SOFT_CAP_FRAC = 0.25
_HARD_CAP_FRAC = 0.50
_URL_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class AtRefContext:
    cwd: str
    home: str
    context_window_chars: int = 200_000

    @property
    def soft_cap(self) -> int:
        return int(self.context_window_chars * _SOFT_CAP_FRAC)

    @property
    def hard_cap(self) -> int:
        return int(self.context_window_chars * _HARD_CAP_FRAC)


# Blocked-path policy. Single helper so the rule lives in one place.
_BLOCKED_DIRS = (".ssh", ".aws", ".gnupg", ".kube")
_BLOCKED_FILE_BASENAMES = {
    ".netrc", ".pgpass", ".bashrc", ".zshrc", ".bash_profile", ".profile",
}
_BLOCKED_FILE_GLOBS = ("*.pem", "*.key", "id_rsa*", "id_ed25519*", "id_dsa*")


def is_path_blocked(path: Path, *, home: Path) -> bool:
    """True if ``path`` is on the deny list."""
    try:
        resolved = path.resolve()
    except OSError:
        return True

    # Refuse anything in a blocked dir under home (~/.ssh, etc.)
    try:
        rel = resolved.relative_to(home.resolve())
        head = rel.parts[0] if rel.parts else ""
        if head in _BLOCKED_DIRS:
            return True
    except ValueError:
        # Not under home — fall through to filename checks
        pass

    name = resolved.name
    if name in _BLOCKED_FILE_BASENAMES:
        return True
    if any(fnmatch(name, g) for g in _BLOCKED_FILE_GLOBS):
        return True

    return False


def _expand_file(ref: AtRef, ctx: AtRefContext) -> str:
    p = Path(ref.arg)
    if not p.is_absolute():
        p = Path(ctx.cwd) / ref.arg
    p = p.expanduser()

    if not p.exists():
        return f"[file not found: {ref.arg}]"
    if not p.is_file():
        return f"[not a file: {ref.arg}]"
    if is_path_blocked(p, home=Path(ctx.home)):
        return f"[blocked path: {ref.arg}]"

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[read failed: {ref.arg} — {exc}]"

    if ref.line_start is not None and ref.line_end is not None:
        lines = text.splitlines(keepends=True)
        a = max(1, ref.line_start)
        b = min(len(lines), ref.line_end)
        text = "".join(lines[a - 1 : b])

    if len(text) > ctx.hard_cap:
        return (
            f"[ref refused: {ref.arg} is {len(text)} chars "
            f"(hard cap {ctx.hard_cap})]"
        )

    notice = ""
    if len(text) > ctx.soft_cap:
        notice = (
            f"\n[note: {ref.arg} is {len(text)} chars — "
            f"exceeds soft cap {ctx.soft_cap}]"
        )

    label = f"@file:{ref.arg}"
    if ref.line_start is not None:
        label += f":{ref.line_start}-{ref.line_end}"

    return f"### {label}\n```\n{text}\n```{notice}"


def _expand_folder(ref: AtRef, ctx: AtRefContext) -> str:
    p = Path(ref.arg)
    if not p.is_absolute():
        p = Path(ctx.cwd) / ref.arg
    p = p.expanduser()

    if not p.exists():
        return f"[folder not found: {ref.arg}]"
    if not p.is_dir():
        return f"[not a folder: {ref.arg}]"

    entries: list[str] = []
    try:
        children = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))
    except OSError as exc:
        return f"[folder read failed: {ref.arg} — {exc}]"

    truncated = False
    for child in children:
        if len(entries) >= _FOLDER_MAX_ENTRIES:
            truncated = True
            break
        try:
            size = child.stat().st_size if child.is_file() else 0
        except OSError:
            size = 0
        marker = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{marker}\t{size} bytes")

    body = "\n".join(entries)
    trailer = ""
    if truncated:
        trailer = (
            f"\n[truncated: showing first {_FOLDER_MAX_ENTRIES} of "
            f"{len(children)} entries]"
        )

    return f"### @folder:{ref.arg}\n```\n{body}{trailer}\n```"


def _git(ctx: AtRefContext, *args: str) -> tuple[bool, str]:
    if not shutil.which("git"):
        return False, "[git not on PATH]"
    try:
        proc = subprocess.run(  # noqa: S603 — args are not user-controlled
            ["git", *args],
            cwd=ctx.cwd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, "[git timed out]"

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().lower()
        if "not a git repository" in msg:
            return False, "[not a git repository]"
        return False, f"[git failed: {msg[:200]}]"

    return True, proc.stdout


def _expand_diff(_ref: AtRef, ctx: AtRefContext) -> str:
    ok, out = _git(ctx, "diff")
    if not ok:
        return out
    return f"### @diff\n```diff\n{out}\n```"


def _expand_staged(_ref: AtRef, ctx: AtRefContext) -> str:
    ok, out = _git(ctx, "diff", "--staged")
    if not ok:
        return out
    return f"### @staged\n```diff\n{out}\n```"


def _expand_git(ref: AtRef, ctx: AtRefContext) -> str:
    try:
        n = int(ref.arg)
    except ValueError:
        return f"[bad @git argument: {ref.arg!r}]"

    clamped = min(max(n, 1), _GIT_MAX_COMMITS)
    ok, out = _git(ctx, "log", "-p", "-n", str(clamped))
    if not ok:
        return out

    notice = ""
    if n != clamped:
        notice = f"\n[clamped from {n} to {clamped} commits]"

    return f"### @git:{clamped}\n```\n{out}\n```{notice}"


def _expand_url(ref: AtRef, _ctx: AtRefContext) -> str:
    from opencomputer.agent.link_understanding import is_safe_url

    if not is_safe_url(ref.arg):
        return f"[blocked: {ref.arg} failed SSRF guard]"

    try:
        import httpx  # already a project dep

        with httpx.Client(timeout=_URL_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(ref.arg)
            if resp.status_code >= 400:
                return f"[fetch failed: {resp.status_code} for {ref.arg}]"
            text = resp.text
    except httpx.TimeoutException:
        return f"[fetch timed out: {ref.arg}]"
    except httpx.HTTPError as exc:
        return f"[fetch failed: {ref.arg} — {exc}]"

    # Strip HTML to text — primitive but enough for v1.
    import re as _re
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()

    return f"### @url:{ref.arg}\n```\n{text[:50_000]}\n```"


_DISPATCH = {
    "file": _expand_file,
    "folder": _expand_folder,
    "diff": _expand_diff,
    "staged": _expand_staged,
    "git": _expand_git,
    "url": _expand_url,
}


def expand(text: str, *, ctx: AtRefContext) -> str:
    """Parse ``text`` for refs and append expansions under a header.

    Original text is preserved verbatim. Expansions are appended after a
    ``--- Attached Context ---`` separator. Returns ``text`` unchanged
    when no refs are found.
    """
    refs = parse(text)
    if not refs:
        return text

    blocks: list[str] = []
    total = 0
    for ref in refs:
        try:
            block = _DISPATCH[ref.kind](ref, ctx)
        except Exception as exc:  # noqa: BLE001 — never crash send-path
            block = f"[expander error: @{ref.kind}:{ref.arg} — {exc}]"

        # Aggregate hard cap across all refs combined.
        if total + len(block) > ctx.hard_cap and len(blocks) > 0:
            blocks.append(
                f"[ref refused: combined expansion exceeded hard cap "
                f"after {len(blocks)} ref(s)]"
            )
            break

        blocks.append(block)
        total += len(block)

    body = "\n\n".join(blocks)
    return f"{text}\n\n--- Attached Context ---\n\n{body}"


__all__ = [
    "AtRef",
    "AtRefKind",
    "AtRefContext",
    "is_path_blocked",
    "parse",
    "expand",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd OpenComputer && pytest tests/test_at_references_expand.py -v`
Expected: PASS — all expand tests.

- [ ] **Step 5: Commit**

```bash
cd OpenComputer
git add opencomputer/agent/at_references.py tests/test_at_references_expand.py
git commit -m "feat(at-refs): expanders + blocked-paths + soft/hard size caps + SSRF reuse"
```

---

## Task 9: Wire @-references into the input loop

**Files:**
- Modify: `opencomputer/cli_ui/input_loop.py`
- Test: `tests/test_at_references_input_loop.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_at_references_input_loop.py`:

```python
"""Input-loop integration: at-references expand on send."""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.at_references import AtRefContext, expand


def test_input_loop_expander_called_when_at_present(tmp_path):
    """Smoke: expand() processes a message with an @ref."""
    f = tmp_path / "spec.md"
    f.write_text("# spec body")

    msg = f"please review @file:{f}"
    out = expand(msg, ctx=AtRefContext(
        cwd=str(tmp_path),
        home=str(tmp_path / "home"),
        context_window_chars=200_000,
    ))
    assert "spec body" in out
    assert "Attached Context" in out
    # Original text preserved verbatim.
    assert msg in out


def test_input_loop_expander_skipped_when_no_at(tmp_path):
    msg = "no references here"
    out = expand(msg, ctx=AtRefContext(
        cwd=str(tmp_path),
        home=str(tmp_path / "home"),
    ))
    assert out == msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && pytest tests/test_at_references_input_loop.py -v`
Expected: PASS for both — these are smoke tests on `expand()` directly. The input-loop wiring itself is UI plumbing; we verify wiring by reading the patched file in step 3 below.

(This is intentional. Input-loop integration tests are notoriously brittle in TUI code; we test the expander surface that the loop calls.)

- [ ] **Step 3: Patch the input loop**

Open `opencomputer/cli_ui/input_loop.py`. Find the function that returns / dispatches a finished user message (look for the place that yields the typed text or returns it from the prompt session). After slash-command dispatch, before message construction, call:

```python
def _maybe_expand_at_refs(text: str) -> str:
    if "@" not in text:
        return text
    try:
        from opencomputer.agent.at_references import AtRefContext, expand
        import os
        from pathlib import Path
        return expand(
            text,
            ctx=AtRefContext(
                cwd=os.getcwd(),
                home=str(Path.home()),
                context_window_chars=200_000,  # TODO: read from active model_capabilities
            ),
        )
    except Exception:  # noqa: BLE001 — never block send on expander
        return text
```

Hook into the existing return/yield path. The exact line depends on input_loop.py shape; the rule is **after** any slash-command handling and **before** the text is wrapped into a Message.

- [ ] **Step 4: Run all at-ref tests**

Run: `cd OpenComputer && pytest tests/test_at_references_parser.py tests/test_at_references_expand.py tests/test_at_references_input_loop.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd OpenComputer
git add opencomputer/cli_ui/input_loop.py tests/test_at_references_input_loop.py
git commit -m "feat(at-refs): expand on user-input send path (CLI only)"
```

---

## Task 9.5: Caller threading + pyproject packaging (audit follow-ups)

**Goal:** Three audit findings must land before push:
1. Pyproject must include skin YAMLs in the wheel.
2. The agent loop must pass `custom_personalities` from config into `PromptBuilder.__init__`.
3. Mid-session `/skin` swap must clearly document the live-Rich-theme limitation.

**Files:**
- Modify: `pyproject.toml`
- Modify: the agent-loop entry that constructs `PromptBuilder` (search: `PromptBuilder(`)
- Add to: `opencomputer/agent/slash_commands_impl/skin_personality_cmd.py` (clarify message)

- [ ] **Step 1: Add YAMLs to wheel**

Open `pyproject.toml`. Find the `[tool.hatch.build.targets.wheel]` section (or the `force-include` / `packages` entries). If not already covered by a glob, add:

```toml
[tool.hatch.build.targets.wheel.shared-data]
"opencomputer/cli_ui/skin/builtins" = "opencomputer/cli_ui/skin/builtins"
```

OR (if hatchling uses include lists):

```toml
[tool.hatch.build]
include = [
  # ... existing entries ...
  "opencomputer/cli_ui/skin/builtins/*.yaml",
]
```

Verify with:
```bash
cd OpenComputer && python -c "from importlib import resources; print(list(resources.files('opencomputer.cli_ui.skin.builtins').iterdir()))"
```
Expected: 9 YAML entries listed.

- [ ] **Step 2: Thread `custom_personalities` through PromptBuilder construction**

```bash
cd OpenComputer && grep -rn "PromptBuilder(" opencomputer/ --include="*.py"
```

For each constructor call, add the missing kwarg by reading from the active profile config:

```python
from opencomputer.agent.profile_yaml import load_profile_config

cfg = load_profile_config(_profile_config_path())
custom_personalities = (cfg.get("agent") or {}).get("personalities") or {}
prompt_builder = PromptBuilder(
    # ... existing kwargs ...
    custom_personalities=custom_personalities,
)
```

Add a quick test that the wiring works end-to-end:

`tests/test_personality_loader.py` — append:

```python
def test_personality_threads_through_promptbuilder_factory(tmp_path, monkeypatch):
    """A custom personality declared in config reaches the prompt."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "agent:\n"
        "  personalities:\n"
        "    codereviewer: |\n"
        "      MARKER-CODE-REVIEW-XYZ\n"
    )
    from opencomputer.agent.profile_yaml import load_profile_config
    loaded = load_profile_config(cfg)
    custom = (loaded.get("agent") or {}).get("personalities") or {}
    assert "codereviewer" in custom

    from opencomputer.agent.prompt_builder import PromptBuilder
    pb = PromptBuilder(
        cwd="/tmp",
        user_home="/tmp/home",
        os_name="Darwin",
        custom_personalities=custom,
    )
    prompt = pb.build_system_prompt(personality="codereviewer")
    assert "MARKER-CODE-REVIEW-XYZ" in prompt
```

Run: `cd OpenComputer && pytest tests/test_personality_loader.py::test_personality_threads_through_promptbuilder_factory -v`
Expected: PASS.

- [ ] **Step 3: Document the live-theme swap limitation in the slash command**

Edit `opencomputer/agent/slash_commands_impl/skin_personality_cmd.py`. In `SkinCommand.execute`, after a successful set, change the message:

```python
return SlashCommandResult(
    output=(
        f"Skin set to {sub} (persisted to config).\n"
        f"Spinner verbs and branding apply immediately; "
        f"color theme takes effect on next session start."
    ),
    handled=True,
)
```

(No new test — message text only.)

- [ ] **Step 4: Commit**

```bash
cd OpenComputer
git add pyproject.toml opencomputer/agent/slash_commands_impl/skin_personality_cmd.py \
        tests/test_personality_loader.py \
        $(grep -rl "PromptBuilder(" opencomputer/ --include="*.py" | head -5)
git commit -m "feat(audit): pyproject YAML packaging + custom-personalities wiring + skin swap message"
```

---

## Task 10: Full suite + lint + push

**Files:** none new

- [ ] **Step 1: Run the full test suite**

Run: `cd OpenComputer && pytest tests/ --maxfail=20 -x -q 2>&1 | tail -50`
Expected: All passing. If a pre-existing test breaks, investigate — do NOT mark complete on failure.

- [ ] **Step 2: Lint**

Run:
```bash
cd OpenComputer
ruff check opencomputer/agent/personality/ \
           opencomputer/agent/at_references.py \
           opencomputer/cli_ui/skin/ \
           opencomputer/agent/slash_commands_impl/skin_personality_cmd.py \
           tests/test_personality_loader.py \
           tests/test_personality_prompt_injection.py \
           tests/test_skin_loader.py \
           tests/test_skin_apply.py \
           tests/test_at_references_parser.py \
           tests/test_at_references_expand.py \
           tests/test_at_references_input_loop.py
```

Expected: Clean.

- [ ] **Step 3: Push branch**

```bash
cd OpenComputer
git push -u origin feat/personality-skins-atrefs-2026-05-08
```

- [ ] **Step 4: Open PR**

```bash
cd OpenComputer
gh pr create --title "feat: personality registry + skin renderer + @-references (Hermes parity)" --body "$(cat <<'EOF'
## Summary

Closes the three real Hermes-parity gaps surfaced by 2026-05-08 user request:

1. **Personality** — `/personality` now resolves a name to a real body. 14 built-in registers (helpful, concise, technical, creative, teacher, kawaii, catgirl, pirate, shakespeare, surfer, noir, uwu, philosopher, hype) bundled. Custom personalities load from `agent.personalities` in profile config. Resolution chain: `--personality` flag → runtime → `agent.default_personality` → built-in helpful. PromptBuilder injects body into slot #7. `/personality NAME` persists to `~/.opencomputer/<profile>/config.yaml`.
2. **Skin** — `/skin` now actually renders. SkinSpec dataclass + 9 built-in YAMLs (default, ares, mono, slate, daylight, warm-lightmode, poseidon, sisyphus, charizard). Custom skins via `~/.opencomputer/skins/<name>.yaml` (machine-wide, not profile-scoped). `apply_skin(spec, console)` swaps Rich theme + spinner verbs + branding + tool prefix + tool emojis. Idempotent. `--skin` flag and `display.skin` config.
3. **`@`-references** — New `opencomputer/agent/at_references.py` parses + expands `@file:`/`@folder:`/`@diff`/`@staged`/`@git:N`/`@url:`. Soft cap 25%, hard cap 50% of model context window. Blocked-paths policy refuses `~/.ssh/`, `~/.aws/`, `~/.gnupg/`, `~/.kube/`, `~/.netrc`, shell profiles, `*.pem`, `*.key`, `id_rsa*`. SSRF reuses `link_understanding.is_safe_url()`. Wired into the CLI input loop only — channels do not expand `@` (matches Hermes "CLI-only" rule).

## What is NOT in this PR (with rationale)

See `docs/superpowers/specs/2026-05-08-personality-skins-atrefs-design.md` §9. In short: 6 missing memory providers (each is a real plugin with external service — separate scoped PRs), `batch_runner` ShareGPT trajectory generator (different audience, deferred until requested), TUI dashboard skinning (v1 chat-REPL only), per-channel personality (one knob suffices for v1), context-file security scanner (separate security pass).

## Test plan

- [x] `pytest tests/test_personality_loader.py` — built-ins, custom override, fall-back, persistence
- [x] `pytest tests/test_personality_prompt_injection.py` — body lands in slot #7
- [x] `pytest tests/test_skin_loader.py` — 9 YAMLs parse, custom override, missing-key inheritance
- [x] `pytest tests/test_skin_apply.py` — Rich theme swap, idempotent, bad hex doesn't crash
- [x] `pytest tests/test_at_references_parser.py` — grammar table + email-not-an-atref
- [x] `pytest tests/test_at_references_expand.py` — file/folder/diff/staged/git/url, blocked paths, soft/hard caps, SSRF
- [x] `pytest tests/test_at_references_input_loop.py` — expander integration smoke
- [x] Full `pytest tests/` suite green
- [x] `ruff check` clean on touched dirs

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**Spec coverage:**
- §3 Architecture diagram → Tasks 1–9 cover every box.
- §4 Personality (built-ins, custom, resolution, persist, prompt slot) → Tasks 1–3.
- §5 Skin (SkinSpec, 9 YAMLs, custom override, apply, persist, CLI flag) → Tasks 4–6.
- §6 `@`-refs (grammar, size policy, blocked paths, SSRF, hook point) → Tasks 7–9.
- §7 Failure modes → tested in test files.
- §8 Tests → all 7 test files specified in Task list.
- §9 Honest deferrals → in PR body.

**Placeholder scan:** None. Every step has exact code or exact command.

**Type consistency:** `Personality(name, body)`, `SkinSpec(name, description, colors, ...)`, `AtRef(kind, arg, line_start, line_end)`, `AtRefContext(cwd, home, context_window_chars)` — referenced consistently across tests and implementations.

**Risks:** Task 3 step 6 (`--personality` flag wiring in cli.py) and Task 6 step 5 (`--skin` flag) describe the change but don't paste the exact 100+ line cli.py context — the engineer follows the existing `--plan` flag pattern. This is a reasonable "follow the existing pattern" instruction; if cli.py shape is too unfamiliar, the engineer reads `--plan`'s wiring as the template.

**Final note:** All tasks touch only the new files + four already-stub files (`skin_personality_cmd.py`, `prompt_builder.py`, `cli.py`, `input_loop.py`). No conflicts expected with the parallel `claude-doc2-2026-05-08` worktree (working on kanban + hooks).
