"""E2E smoke: run_setup() with all menu primitives mocked, end to end."""
from __future__ import annotations


def test_e2e_first_run_picks_first_provider_and_skips_messaging(
    monkeypatch, tmp_path,
):
    """Drive the full wizard: pick provider 0, skip messaging, deferred
    sections stub out. Assert config file is written with expected shape."""
    from opencomputer.cli_setup import wizard

    monkeypatch.setattr(wizard, "_resolve_config_path",
                         lambda: tmp_path / "config.yaml")

    # M1 — prior_install_detect is now LIVE; mock detection to find
    # nothing so the section returns SKIPPED_FRESH without prompting.
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.prior_install._detect_prior_installs",
        lambda: [],
    )

    # Sequence of radiolist returns at the wizard level. The two LIVE
    # sections (inference_provider, messaging_platforms) each invoke
    # their own radiolist via the section-handler module-level binding.
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.inference_provider.radiolist",
        lambda *a, **kw: 0,  # pick first provider
    )
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.messaging_platforms.radiolist",
        lambda *a, **kw: 1,  # skip messaging
    )
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.inference_provider._discover_providers",
        lambda: [{"name": "anthropic", "label": "Anthropic", "description": "x"}],
    )
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.inference_provider._invoke_provider_setup",
        lambda name, ctx: (
            ctx.config.setdefault("model", {}).update({"provider": name})
            or True
        ),
    )
    # S1 — agent_settings is now LIVE; mock its radiolist to "Apply
    # recommended defaults" (idx 0).
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.agent_settings.radiolist",
        lambda *a, **kw: 0,
    )
    # S5 — launchd_service is now LIVE; force non-macOS path so the
    # test doesn't actually write a real plist or shell out to launchctl.
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.launchd_service._is_macos",
        lambda: False,
    )
    # S4 — tools section is now LIVE; mock to apply preset (idx 0).
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.tools.radiolist",
        lambda *a, **kw: 0,
    )

    rc = wizard.run_setup()
    assert rc == 0

    import yaml
    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert written["model"]["provider"] == "anthropic"
    assert "platforms" not in (written.get("gateway") or {})
    # S1 wrote recommended loop defaults.
    assert written["loop"]["max_iterations"] == 90
