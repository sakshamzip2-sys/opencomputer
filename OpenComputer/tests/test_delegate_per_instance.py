"""DelegateTool per-instance factory + templates — Phase 2 Task 2.3.

Multi-profile correctness: each AgentLoop has its own DelegateTool
instance with its own factory closure binding profile_id+profile_home.
Sharing factories at the class level would mean profile A's setup
clobbers the factory profile B's loop uses.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencomputer.tools.delegate import DelegateTool


def test_set_factory_is_per_instance() -> None:
    """Two DelegateTool instances must hold their own factories so a
    second AgentLoop's setup doesn't clobber the first's."""
    a = DelegateTool()
    b = DelegateTool()
    f_a = MagicMock(name="factory-a")
    f_b = MagicMock(name="factory-b")

    DelegateTool.set_factory(f_a, instance=a)
    DelegateTool.set_factory(f_b, instance=b)

    # Each instance has its own factory; they don't share.
    # Direct read may return either the function or a staticmethod
    # wrapper depending on implementation; we accept either.
    a_factory = a._factory if not hasattr(a._factory, "__func__") else a._factory.__func__
    b_factory = b._factory if not hasattr(b._factory, "__func__") else b._factory.__func__
    assert a_factory is f_a
    assert b_factory is f_b


def test_set_factory_class_level_fallback_still_works() -> None:
    """Legacy CLI bootstrap calls DelegateTool.set_factory(...) at
    class level. New instances should pick that up as a fallback."""
    legacy_factory = MagicMock(name="legacy-cli-factory")
    DelegateTool.set_factory(legacy_factory)

    new_instance = DelegateTool()
    # The instance should see the class-level factory as its own
    # default unless overridden via instance=...
    instance_factory = (
        new_instance._factory.__func__
        if hasattr(new_instance._factory, "__func__")
        else new_instance._factory
    )
    assert instance_factory is legacy_factory

    # Cleanup so other tests aren't affected
    DelegateTool._factory_class_level = None


def test_set_templates_is_per_instance() -> None:
    """Same as factory but for templates."""
    a = DelegateTool()
    b = DelegateTool()
    DelegateTool.set_templates({"agent_a": MagicMock()}, instance=a)
    DelegateTool.set_templates({"agent_b": MagicMock()}, instance=b)
    assert "agent_a" in a._templates
    assert "agent_b" not in a._templates
    assert "agent_b" in b._templates
    assert "agent_a" not in b._templates
