"""code-modernization plugin — registers nothing in Python.

Skills under ``skills/<name>/SKILL.md`` and agents under
``agents/<name>.md`` are filesystem-discovered by the OC core. This
plugin's ``register(api)`` exists so ``opencomputer plugins`` can list
the plugin and so the manifest's ``kind=mixed`` declaration round-trips
through the loader without contract-warnings.

If/when this plugin grows tools or hooks, register them here.
"""

from __future__ import annotations


def register(api) -> None:  # noqa: D401 — duck-typed PluginAPI
    """Filesystem-only plugin — no-op registration."""
    return None
