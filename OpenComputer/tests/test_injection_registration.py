"""Tests for :func:`register_default_injection_providers`.

The helper consolidates registration of OC's four built-in injection
providers — ``ThinkingInjector`` (``thinking_tags_fallback``),
``PathGlobRulesProvider`` (``path_glob_rules``),
``HandoffInjectionProvider`` (``handoff_inbox``) and
``LifeEventInjectionProvider`` (``life_event_hint``) — into ONE
surface-parameterised call site, so every surface has a single call
rather than the scattered registration that left three providers
CLI-only.

The load-bearing test here is the handoff-resolver one: the gateway
surface MUST get a profile-home resolver based on
:func:`opencomputer.agent.config._home` (which honours the
``current_profile_home`` ContextVar that per-dispatch ``set_profile``
sets), and that resolver MUST point at the IDENTICAL directory the CLI
surface's sticky-active-profile resolver yields for the same profile.
A mismatch would make the gateway destructively archive the WRONG
profile's pending handoffs (``HandoffInbox.read_and_process_all`` does
``os.replace``).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from plugin_sdk.profile_context import set_profile

# The four built-in provider ids the helper must register.
_PROVIDER_IDS = (
    "thinking_tags_fallback",
    "path_glob_rules",
    "handoff_inbox",
    "life_event_hint",
)


@pytest.fixture
def clean_engine():
    """Yield the process-wide injection engine, scrubbed of the four
    built-in providers before AND after the test.

    The injection engine is a process-global singleton; without this
    teardown a registration from one test would leak into the next
    (``InjectionEngine.register`` raises ``ValueError`` on a duplicate
    ``provider_id``).
    """
    from opencomputer.agent.injection import engine

    for pid in _PROVIDER_IDS:
        engine.unregister(pid)
    try:
        yield engine
    finally:
        for pid in _PROVIDER_IDS:
            engine.unregister(pid)


def test_registers_all_four_for_cli(clean_engine) -> None:
    """``register_default_injection_providers("cli")`` leaves all four
    built-in providers on the injection engine."""
    from opencomputer.agent.injection_registration import (
        register_default_injection_providers,
    )

    register_default_injection_providers("cli")

    for pid in _PROVIDER_IDS:
        assert pid in clean_engine._providers, f"{pid} not registered"


def test_registers_all_four_for_gateway(clean_engine) -> None:
    """The gateway surface gets the same four providers as the CLI."""
    from opencomputer.agent.injection_registration import (
        register_default_injection_providers,
    )

    register_default_injection_providers("gateway")

    for pid in _PROVIDER_IDS:
        assert pid in clean_engine._providers, f"{pid} not registered"


def test_idempotent(clean_engine) -> None:
    """Calling the helper twice does not raise — each provider does an
    ``unregister`` before ``register`` so a re-registration replaces
    rather than collides."""
    from opencomputer.agent.injection_registration import (
        register_default_injection_providers,
    )

    register_default_injection_providers("gateway")
    # Second call must not raise the engine's duplicate-provider ValueError.
    register_default_injection_providers("gateway")

    for pid in _PROVIDER_IDS:
        assert pid in clean_engine._providers
    # Exactly one of each — no doubling.
    assert sum(1 for p in clean_engine._providers if p == "handoff_inbox") == 1


def test_handoff_resolver_gateway_honors_contextvar_not_sticky(
    clean_engine, tmp_path, monkeypatch
) -> None:
    """The load-bearing test.

    On the gateway surface the registered ``HandoffInjectionProvider``'s
    resolver MUST resolve via the ``current_profile_home`` ContextVar
    (set per-dispatch by the gateway's ``set_profile``), NOT via the
    sticky ``read_active_profile()`` marker file. AND it MUST resolve to
    the SAME directory the CLI surface's resolver yields for that same
    profile — otherwise the gateway destructively archives the wrong
    profile's handoffs.

    Setup: a sticky ``active_profile`` pointing at profile ``sticky``,
    but a ``set_profile`` scope binding the ContextVar to profile
    ``dispatched``'s root. The gateway resolver must follow
    ``dispatched``; the CLI resolver (which reads the sticky marker)
    follows ``sticky``.
    """
    from opencomputer import profiles as profiles_mod
    from opencomputer.agent.injection_registration import (
        register_default_injection_providers,
    )

    # Point the profile root at tmp so we don't touch the real ~/.opencomputer.
    root = tmp_path / ".opencomputer"
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(root))

    # Two real profiles on disk.
    sticky_dir = profiles_mod.get_profile_dir("sticky")
    dispatched_dir = profiles_mod.get_profile_dir("dispatched")
    for d in (sticky_dir, dispatched_dir):
        d.mkdir(parents=True, exist_ok=True)
    # Sticky marker says "sticky" is the active profile.
    profiles_mod.write_active_profile("sticky")

    # ---- gateway surface ----
    register_default_injection_providers("gateway")
    gw_provider = clean_engine._providers["handoff_inbox"]
    gw_resolver = gw_provider._resolver

    # Evaluated UNDER a set_profile scope binding the ContextVar to
    # `dispatched`'s root — exactly what the gateway's per-dispatch
    # set_profile() does.
    with set_profile(dispatched_dir):
        gw_resolved = gw_resolver()

    # The gateway resolver followed the ContextVar (dispatched), NOT the
    # sticky marker (sticky).
    assert isinstance(gw_resolved, Path)
    assert sticky_dir not in gw_resolved.parents
    assert gw_resolved != sticky_dir / "home"

    # ---- CLI surface (sticky-marker resolver) ----
    # Re-register for the CLI surface; its resolver reads read_active_profile().
    register_default_injection_providers("cli")
    cli_provider = clean_engine._providers["handoff_inbox"]
    cli_resolver = cli_provider._resolver

    # The CLI resolver for the `dispatched` profile must be reproduced by
    # temporarily making `dispatched` the sticky profile and evaluating it.
    profiles_mod.write_active_profile("dispatched")
    cli_resolved_for_dispatched = cli_resolver()

    # THE reconciliation assertion: the gateway resolver (ContextVar →
    # `dispatched`) lands on the SAME directory the CLI resolver yields
    # for `dispatched`. If these diverged the gateway would archive the
    # wrong profile's inbox.
    assert gw_resolved == cli_resolved_for_dispatched
    # And it is the documented `<profile_dir>/home` directory.
    assert gw_resolved == dispatched_dir / "home"


def test_handoff_resolver_cli_uses_sticky_active_profile(
    clean_engine, tmp_path, monkeypatch
) -> None:
    """The CLI resolver keeps the legacy sticky-active-profile behavior
    so a mid-session ``/handoff`` profile swap is honored."""
    from opencomputer import profiles as profiles_mod
    from opencomputer.agent.injection_registration import (
        register_default_injection_providers,
    )

    root = tmp_path / ".opencomputer"
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(root))

    named_dir = profiles_mod.get_profile_dir("coder")
    named_dir.mkdir(parents=True, exist_ok=True)
    profiles_mod.write_active_profile("coder")

    register_default_injection_providers("cli")
    resolver = clean_engine._providers["handoff_inbox"]._resolver

    assert resolver() == named_dir / "home"


def test_one_provider_failure_does_not_block_others(
    clean_engine, monkeypatch, caplog
) -> None:
    """If one provider's construction raises, the other three still
    register and a WARNING is logged — each provider is wrapped in its
    own try/except."""
    import opencomputer.agent.thinking_injector as thinking_mod
    from opencomputer.agent.injection_registration import (
        register_default_injection_providers,
    )

    class _BoomError(Exception):
        pass

    def _raise(*_args, **_kwargs):
        raise _BoomError("thinking injector construction blew up")

    # Make ThinkingInjector construction explode.
    monkeypatch.setattr(thinking_mod, "ThinkingInjector", _raise)

    with caplog.at_level(logging.WARNING):
        register_default_injection_providers("gateway")

    # The failing provider is absent...
    assert "thinking_tags_fallback" not in clean_engine._providers
    # ...but the other three registered fine.
    assert "path_glob_rules" in clean_engine._providers
    assert "handoff_inbox" in clean_engine._providers
    assert "life_event_hint" in clean_engine._providers
    # ...and a WARNING was logged for the failure.
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
