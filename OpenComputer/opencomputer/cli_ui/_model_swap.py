"""Model-cycling helper (Alt+M = scoped-models cycle).

Mirrors :mod:`opencomputer.cli_ui._profile_swap` — same shape so the
interactive ``input_loop`` can wire Alt+M alongside Ctrl+P with
matching consume-at-turn-boundary semantics.

Pi's ``scoped-models-selector.ts`` is the inspiration. The "scoped
models" idea is: a curated 2-3 model short list the user actually
wants to flip between (opus for hard work, haiku for cheap
follow-ups), separate from the 300+ entry model registry. Trying to
cycle through every registered model would be useless; cycling
through 3 favorites is muscle memory.

Storage: ``<profile_dir>/favorites.yaml`` with shape::

    models:
      - claude-opus-4-7
      - claude-sonnet-4-6
      - claude-haiku-4-5

Where ``<profile_dir>`` is resolved by
:func:`opencomputer.profiles.get_profile_dir` — for the ``default``
profile that's ``~/.opencomputer/favorites.yaml``; for a named
profile it's ``~/.opencomputer/profiles/<name>/favorites.yaml``.

Backwards-compat: when the file is missing, the cycle is a no-op and
the input_loop surfaces ``model_cycle_hint`` for one render tick to
tell the user how to populate it. No errors, no scary behaviour.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from opencomputer.profiles import get_profile_dir, read_active_profile

_log = logging.getLogger(__name__)

NO_OTHER_MODELS_HINT: str = (
    "no other models — add to <profile>/favorites.yaml: "
    'models: ["model-a", "model-b"]'
)


def _favorites_path(profile_id: str | None) -> Path:
    """Resolve the favorites file for a profile.

    Falls back to the currently-active profile when ``profile_id`` is
    ``None`` so callers from the keybinding (which only knows
    ``runtime``) don't have to thread profile resolution themselves.
    """
    if profile_id is None:
        profile_id = read_active_profile()
    return get_profile_dir(profile_id) / "favorites.yaml"


def list_favorite_models(*, profile_id: str | None = None) -> list[str]:
    """Read the scoped-models list for a profile.

    Returns an empty list when the file is missing, unreadable, or
    malformed. Non-string entries are filtered out. Never raises —
    the Alt+M keybinding must not crash the input loop because the
    user typed something invalid into favorites.yaml.
    """
    path = _favorites_path(profile_id)
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, ValueError) as exc:
        _log.debug("favorites.yaml unreadable at %s: %s", path, exc)
        return []
    if not isinstance(raw, dict):
        return []
    models = raw.get("models")
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, str) and m.strip()]


def consume_pending_model_swap(runtime: Any) -> str | None:
    """Apply ``pending_model_id`` if set. Called at turn entry by the
    agent loop. Returns the new model id or ``None`` if nothing was
    pending.

    Mirrors :func:`_profile_swap.consume_pending_profile_swap`. Pops
    the pending key (one-shot semantics) and surfaces the value so
    the caller can rebind ``self.config.model.model`` via the same
    code path that ``/model`` mid-session swap uses.
    """
    new_id = runtime.custom.pop("pending_model_id", None)
    if new_id:
        runtime.custom["model_cycle_hint"] = f"model → {new_id}"
        return str(new_id)
    return None


def cycle_model(runtime: Any) -> str | None:
    """Advance ``runtime.custom["pending_model_id"]`` to the next
    favorite. Mirrors :func:`_profile_swap.cycle_profile`.

    Returns the new pending id, or ``None`` when there's nothing to
    cycle to (zero or one favorite). On the no-op path,
    ``runtime.custom["model_cycle_hint"]`` is set so the input_loop
    can flash a one-render-tick message explaining how to populate
    the list.
    """
    profile = runtime.custom.get("active_profile_id")
    favorites = list_favorite_models(profile_id=profile)
    if len(favorites) <= 1:
        runtime.custom["model_cycle_hint"] = NO_OTHER_MODELS_HINT
        return None

    current = (
        runtime.custom.get("pending_model_id")
        or runtime.custom.get("active_model_id")
    )
    try:
        idx = favorites.index(current) if current in favorites else -1
    except ValueError:
        idx = -1
    nxt = favorites[(idx + 1) % len(favorites)]
    runtime.custom["pending_model_id"] = nxt
    return nxt
