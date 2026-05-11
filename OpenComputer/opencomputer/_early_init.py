"""Side-effect module that hardens import-time warnings.

This module is intentionally imported for its side effects from
``opencomputer/__init__.py`` as the very first thing the package does.
Any entry point (``oc`` CLI, ``oc gateway``, ``oc wire``, tests, library
embedders) therefore gets the same warning suppression before any
transitive import can emit a noisy dependency-version line.

Rationale
---------
``urllib3`` and ``charset_normalizer`` ship a ``UserWarning`` when their
detected runtime version doesn't match a supported pin. The exact
strings (and the underlying check) live deep inside ``requests`` /
``urllib3``'s package init and fire the first time those modules are
imported — usually well before our CLI prints anything. The warning is
purely cosmetic noise for end users but it leaks into clean ``oc chat``
sessions.

Filtering at the top of ``cli.py`` worked for ``oc`` but not for
``opencomputer.gateway``, embedders, or tests that import any
``opencomputer.*`` submodule directly. Hoisting the filter call into a
no-cost side-effect module loaded by ``opencomputer/__init__.py``
guarantees the filter is active before any urllib3-pulling code runs,
regardless of how ``opencomputer`` was entered.

Why not just register via ``[tool.ruff.lint.per-file-ignores]``
---------------------------------------------------------------
We previously suppressed the 36 ``E402`` errors in ``cli.py`` with a
per-file-ignore. That was a band-aid: it papered over the lint signal
without addressing the underlying ordering dependency. With the filter
hoisted here, ``cli.py``'s imports are all at the top of the file and
``E402`` never fires — the rule stays active for legitimate violations.

This module deliberately exposes ``_install_dependency_warning_filters``
as part of its public surface so tests can re-invoke it inside a
``warnings.catch_warnings`` block to assert the filter still suppresses
the targeted strings.
"""

from __future__ import annotations

import warnings


def _install_dependency_warning_filters() -> None:
    """Hide noisy dependency-version warnings that leak at import time.

    Idempotent: ``warnings.filterwarnings`` builds up a filter list, and
    duplicate entries are harmless — the matching engine still short-
    circuits on the first hit.
    """
    warnings.filterwarnings(
        "ignore",
        message=r"urllib3 .*doesn't match a supported version!",
        category=Warning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r"charset_normalizer .*doesn't match a supported version!",
        category=Warning,
    )


# Module-import side effect: install filters now, before any sibling
# ``opencomputer`` submodule has a chance to transitively import
# ``urllib3`` / ``requests`` / ``aiohttp`` and fire the warning.
_install_dependency_warning_filters()
