"""Phase 5 tests: setup wizard helpers + doctor checks."""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

# ─── Doctor ─────────────────────────────────────────────────────


def test_doctor_python_version_check_passes() -> None:
    from opencomputer.doctor import _check_python

    c = _check_python()
    assert c.status == "pass"


def test_doctor_config_absent_warns(tmp_path: Path) -> None:
    """When config file doesn't exist, doctor warns but doesn't fail."""
    from opencomputer.doctor import _check_config

    with patch(
        "opencomputer.agent.config_store.config_file_path",
        return_value=tmp_path / "nope.yaml",
    ):
        check, cfg = _check_config()
    assert check.status == "warn"
    assert cfg is None


def test_doctor_config_present_passes(tmp_path: Path) -> None:
    from opencomputer.agent.config import default_config
    from opencomputer.agent.config_store import save_config
    from opencomputer.doctor import _check_config

    path = tmp_path / "config.yaml"
    save_config(default_config(), path)
    with patch("opencomputer.agent.config_store.config_file_path", return_value=path):
        check, cfg = _check_config()
    assert check.status == "pass"
    assert cfg is not None


def test_doctor_provider_key_missing_fails() -> None:
    from opencomputer.agent.config import default_config
    from opencomputer.doctor import _check_provider_key

    cfg = default_config()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(cfg.model.api_key_env, None)
        c = _check_provider_key(cfg)
    assert c.status == "fail"
    assert cfg.model.api_key_env in c.detail


def test_doctor_provider_key_set_passes() -> None:
    from opencomputer.agent.config import default_config
    from opencomputer.doctor import _check_provider_key

    cfg = default_config()
    with patch.dict(os.environ, {cfg.model.api_key_env: "sk-x"}, clear=False):
        c = _check_provider_key(cfg)
    assert c.status == "pass"


def test_doctor_session_db_writable(tmp_path: Path) -> None:
    from dataclasses import replace

    from opencomputer.agent.config import default_config
    from opencomputer.doctor import _check_session_db

    cfg = default_config()
    cfg = replace(cfg, session=replace(cfg.session, db_path=tmp_path / "sessions.db"))
    c = _check_session_db(cfg)
    assert c.status == "pass"


def test_doctor_skills_dir_writable(tmp_path: Path) -> None:
    from dataclasses import replace

    from opencomputer.agent.config import default_config
    from opencomputer.doctor import _check_skills_dir

    cfg = default_config()
    cfg = replace(cfg, memory=replace(cfg.memory, skills_path=tmp_path / "skills"))
    c = _check_skills_dir(cfg)
    assert c.status == "pass"


def test_doctor_run_returns_failure_count_zero_on_clean_env(tmp_path: Path) -> None:
    """End-to-end: on a clean setup with a valid config and API key, 0 failures."""
    from opencomputer.agent.config import default_config
    from opencomputer.agent.config_store import save_config
    from opencomputer.doctor import run_doctor

    config_file = tmp_path / "config.yaml"
    save_config(default_config(), config_file)
    with patch(
        "opencomputer.agent.config_store.config_file_path", return_value=config_file
    ), patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}, clear=False):
        failures = run_doctor()
    assert failures == 0


def test_doctor_captures_plugin_startup_noise(capsys) -> None:
    """Plugin loader warnings must not print before the health report."""
    import logging

    from opencomputer import doctor

    def noisy_provider_check(_cfg):
        logging.getLogger("opencomputer.plugins.loader").warning(
            "skipping plugin 'slack': single-instance lock unavailable"
        )
        logging.getLogger("opencomputer.plugins.loader").exception(
            "plugin 'webhook-inbound' register() raised: broken"
        )
        return doctor.Check("provider plugin", "pass", "'openrouter' registered")

    patches = [
        patch("opencomputer.doctor._check_python", return_value=doctor.Check("python version", "pass", "3.14")),
        patch("opencomputer.doctor._check_source_freshness", return_value=doctor.Check("source freshness", "pass", "fresh")),
        patch("opencomputer.doctor._check_fd_limit_and_competitors", return_value=doctor.Check("fd limit + competitors", "skip", "n/a")),
        patch("opencomputer.doctor._check_config", return_value=(doctor.Check("config file", "pass", "ok"), object())),
        patch("opencomputer.doctor._check_provider_plugin", side_effect=noisy_provider_check),
        patch("opencomputer.doctor._check_provider_key", return_value=doctor.Check("provider API key", "pass", "set")),
        patch("opencomputer.doctor._check_session_db", return_value=doctor.Check("session DB path", "pass", "ok")),
        patch("opencomputer.doctor._check_skills_dir", return_value=doctor.Check("skills dir", "pass", "ok")),
        patch("opencomputer.doctor._check_channel_tokens", return_value=[]),
        patch("opencomputer.doctor._check_profile_and_overlay", return_value=[]),
        patch("opencomputer.doctor._check_profile_artifacts", return_value=[]),
        patch("opencomputer.doctor._check_service", return_value=doctor.Check("service", "skip", "n/a")),
        patch("opencomputer.doctor._check_mcp", return_value=[]),
        patch("opencomputer.doctor._check_g_subsystems", return_value=[]),
        patch("opencomputer.agent.config._home", return_value=Path(".")),
        patch("opencomputer.doctor._check_orphan_oi_venv", return_value=doctor.CheckResult(True, "info", "ok")),
        patch("opencomputer.doctor._check_introspection_deps", return_value=[]),
        patch("opencomputer.doctor._check_ambient_state", return_value=doctor.CheckResult(True, "info", "ok")),
        patch("opencomputer.doctor._check_ambient_foreground_capable", return_value=doctor.CheckResult(True, "info", "ok")),
        patch("opencomputer.doctor._check_skill_evolution_state", return_value=doctor.CheckResult(True, "info", "ok")),
        patch("opencomputer.doctor._check_voice_mode_capable", return_value=doctor.CheckResult(True, "info", "ok")),
        patch("opencomputer.doctor._check_browser_control_capable", return_value=doctor.CheckResult(True, "info", "ok")),
        patch("opencomputer.doctor._check_wake_word_capable", return_value=doctor.CheckResult(True, "info", "ok")),
        patch("opencomputer.doctor._check_wake_train_capable", return_value=doctor.CheckResult(True, "info", "ok")),
        patch("opencomputer.doctor._run_contributions", return_value=[]),
    ]
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        failures = doctor.run_doctor()

    captured = capsys.readouterr()
    assert failures == 0
    assert "Traceback" not in captured.err
    assert "single-instance lock unavailable" not in captured.err
    assert "plugin 'webhook-inbound' register() raised" not in captured.err
    assert "Plugin Startup" in captured.out
    assert "2 startup notice(s) suppressed" in captured.out


# ─── Setup wizard helpers ───────────────────────────────────────


def test_setup_wizard_provider_catalog_includes_anthropic_and_openai() -> None:
    # G.24: catalog now built from plugin manifests via
    # _discover_supported_providers; the legacy _BUILTIN_PROVIDER_FALLBACK
    # acts as a backstop. Either way, anthropic + openai must be present
    # because both are bundled provider plugins shipping setup metadata.
    from opencomputer.setup_wizard import _get_supported_providers

    catalog = _get_supported_providers()
    assert "anthropic" in catalog
    assert "openai" in catalog
    for _pid, meta in catalog.items():
        assert "env_key" in meta
        assert "default_model" in meta
        assert "signup_url" in meta
        assert meta["signup_url"].startswith("https://")
