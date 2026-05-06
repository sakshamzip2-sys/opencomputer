"""Task 1 — verify HookEvent.BEFORE_INSTALL exists in the public SDK."""

from __future__ import annotations


def test_before_install_hook_event_exists():
    from plugin_sdk.hooks import ALL_HOOK_EVENTS, HookEvent

    assert HookEvent.BEFORE_INSTALL == "BeforeInstall"
    assert HookEvent.BEFORE_INSTALL in ALL_HOOK_EVENTS


def test_before_install_hook_context_fields():
    from plugin_sdk.hooks import HookContext, HookEvent

    ctx = HookContext(
        event=HookEvent.BEFORE_INSTALL,
        session_id="install-session",
        install_source="git",
        install_url="git+https://github.com/example/plugin.git",
        install_plugin_id="example-plugin",
        install_scan_report=None,
    )
    assert ctx.install_source == "git"
    assert ctx.install_url == "git+https://github.com/example/plugin.git"
    assert ctx.install_plugin_id == "example-plugin"
    assert ctx.install_scan_report is None
