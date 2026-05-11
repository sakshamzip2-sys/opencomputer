"""Tests for the import-time dependency-warning filter.

Filters are installed by ``opencomputer._early_init`` as a side effect
of importing the package. The function is exposed so tests can re-invoke
it inside a ``warnings.catch_warnings`` block and assert it still
suppresses the targeted UserWarnings.
"""
from __future__ import annotations

import warnings


def test_urllib3_version_mismatch_warning_is_suppressed() -> None:
    """The filter set up by _early_init must keep urllib3 noise out."""
    from opencomputer._early_init import _install_dependency_warning_filters

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        _install_dependency_warning_filters()
        warnings.warn(
            "urllib3 (2.6.2) or chardet (7.4.4)/charset_normalizer "
            "(3.4.4) doesn't match a supported version!",
            Warning,
            stacklevel=2,
        )

    assert caught == [], (
        f"urllib3 version-mismatch warning leaked through the filter: "
        f"{[str(w.message) for w in caught]}"
    )


def test_charset_normalizer_version_mismatch_warning_is_suppressed() -> None:
    """charset_normalizer ships its own variant of the version-mismatch warning."""
    from opencomputer._early_init import _install_dependency_warning_filters

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        _install_dependency_warning_filters()
        warnings.warn(
            "charset_normalizer (3.4.4) doesn't match a supported version!",
            Warning,
            stacklevel=2,
        )

    assert caught == []


def test_unrelated_warning_still_fires() -> None:
    """The filter must not be a blanket suppressor — unrelated warnings still leak through."""
    from opencomputer._early_init import _install_dependency_warning_filters

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        _install_dependency_warning_filters()
        warnings.warn("an unrelated user warning", UserWarning, stacklevel=2)

    assert len(caught) == 1
    assert "unrelated" in str(caught[0].message)


def test_filter_is_idempotent() -> None:
    """Calling the installer twice must not raise or double-suppress unrelated warnings."""
    from opencomputer._early_init import _install_dependency_warning_filters

    _install_dependency_warning_filters()
    _install_dependency_warning_filters()  # second call must be a no-op


def test_filter_is_registered_in_a_fresh_python_subprocess() -> None:
    """In a fresh Python interpreter, ``import opencomputer`` alone must
    leave the urllib3 + charset_normalizer filters in ``warnings.filters``.

    We can't introspect ``warnings.filters`` inside the test process
    because pytest resets the filter chain per-test (by design — so a
    test's installed filters don't leak into the next test). The only
    reliable way to verify the side-effect side of ``_early_init.py`` is
    to spawn a clean subprocess that imports the package fresh and prints
    its filter state. This mirrors what happens at real ``oc`` startup.
    """
    import subprocess
    import sys

    # NOTE on `-W default`: we explicitly reset Python's command-line
    # warning filters to the default before importing opencomputer.
    # That isolates the test from any inherited PYTHONWARNINGS env var
    # and ensures we measure ONLY what _early_init contributes.
    snippet = (
        "import warnings\n"
        "import opencomputer  # imports _early_init as a side effect\n"
        "needles = ('urllib3', 'charset_normalizer')\n"
        "found = set()\n"
        "for action, pattern, _cat, _mod, _line in warnings.filters:\n"
        "    if action == 'ignore' and pattern is not None:\n"
        "        for n in needles:\n"
        "            if n in pattern.pattern:\n"
        "                found.add(n)\n"
        "missing = set(needles) - found\n"
        "if missing:\n"
        "    raise SystemExit(f'MISSING:{sorted(missing)}')\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-W", "default", "-c", snippet],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess failed:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK", (
        f"unexpected subprocess output: {result.stdout!r}"
    )
