"""T70 — subagents inherit the parent's CredentialPool instance.

Without inheritance, a 401 quarantine in the parent doesn't affect the
child's pool, which races to discover the same dead key. Worse, JWT
refresh state diverges. Inheritance fixes both: the SAME pool object
is shared so quarantine, key selection, and refresh state are unified.
"""

from __future__ import annotations

from opencomputer.tools.delegate import inherit_credential_pool


class _Provider:
    def __init__(self, pool=None):
        self._credential_pool = pool


def test_inheritance_copies_when_both_providers_present():
    pool = object()
    parent = _Provider(pool=pool)
    child = _Provider(pool=None)
    inherit_credential_pool(parent, child)
    assert child._credential_pool is pool


def test_inheritance_skips_when_parent_has_no_pool():
    parent = _Provider(pool=None)
    child = _Provider(pool=None)
    inherit_credential_pool(parent, child)
    assert child._credential_pool is None


def test_inheritance_does_not_overwrite_existing_child_pool():
    """If the child already has its own pool, leave it alone."""
    parent_pool = object()
    child_pool = object()
    parent = _Provider(pool=parent_pool)
    child = _Provider(pool=child_pool)
    inherit_credential_pool(parent, child)
    assert child._credential_pool is child_pool  # unchanged


def test_inheritance_handles_missing_attr_gracefully():
    """Providers without _credential_pool attr should not crash."""

    class _Bare:
        pass

    parent = _Bare()
    child = _Bare()
    inherit_credential_pool(parent, child)
    # Implicit assertion: no exception. No attribute set on child.
    assert not hasattr(child, "_credential_pool")


def test_inheritance_skips_when_provider_classes_differ():
    """An OpenAI parent shouldn't hand its keys to an Anthropic child."""

    class _A:
        def __init__(self, pool=None):
            self._credential_pool = pool

    class _B:
        def __init__(self, pool=None):
            self._credential_pool = pool

    pool = object()
    parent = _A(pool=pool)
    child = _B(pool=None)
    inherit_credential_pool(parent, child)
    assert child._credential_pool is None  # different classes — no inherit
