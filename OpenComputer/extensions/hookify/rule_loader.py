"""Load Hookify rules from disk.

Two roots searched (later overrides earlier on name collision — same
shadowing convention OC uses for skills):

1. ``$OPENCOMPUTER_PROFILE_HOME/hookify/*.md`` — per-profile rules
2. ``<cwd>/.opencomputer/hookify/*.md`` — per-project rules (optional)

Each ``.md`` file is parsed as Markdown-with-YAML-frontmatter via the
``frontmatter`` package (same dependency the bundled-corpus validator
uses). Files without frontmatter or with ``enabled: false`` are
skipped. Parse errors are logged at WARN and the file is skipped — a
broken rule file should never wedge the agent loop.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import frontmatter
from rule import Rule  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)


def _profile_hookify_dir() -> Path:
    """Per-profile rules directory."""
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME") or os.environ.get(
        "CLAUDE_PLUGIN_ROOT"
    )
    base = Path(env) if env else Path.home() / ".opencomputer" / "default"
    return base / "hookify"


def _project_hookify_dir() -> Path:
    """Per-project rules directory under cwd."""
    return Path.cwd() / ".opencomputer" / "hookify"


def _load_dir(d: Path) -> list[Rule]:
    if not d.is_dir():
        return []
    rules: list[Rule] = []
    for md in sorted(d.glob("*.md")):
        try:
            post = frontmatter.load(str(md))
        except Exception as exc:
            logger.warning("hookify: failed to parse %s: %s", md, exc)
            continue
        if not post.metadata:
            continue
        try:
            rule = Rule.from_frontmatter(
                dict(post.metadata), str(post.content or "")
            )
        except Exception as exc:
            logger.warning("hookify: failed to build rule from %s: %s", md, exc)
            continue
        if not rule.enabled:
            continue
        rules.append(rule)
    return rules


def load_rules(event: str | None = None) -> list[Rule]:
    """Load every enabled rule. If ``event`` is given, filter to that family.

    Project-level rules shadow profile-level rules on name collision.
    """
    profile_rules = _load_dir(_profile_hookify_dir())
    project_rules = _load_dir(_project_hookify_dir())
    seen: dict[str, Rule] = {r.name: r for r in profile_rules}
    for r in project_rules:
        seen[r.name] = r  # project wins
    out = list(seen.values())
    if event is None:
        return out
    return [r for r in out if r.event in (event, "all")]
