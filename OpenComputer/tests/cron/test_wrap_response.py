"""Hermes parity (2026-05-08): cron.wrap_response config knob."""

from opencomputer.agent.config import CronConfig, default_config


class TestCronConfig:
    def test_wrap_response_defaults_false(self):
        # OC default differs from Hermes spec — preserves existing behavior.
        cfg = CronConfig()
        assert cfg.wrap_response is False

    def test_wrap_response_can_enable(self):
        cfg = CronConfig(wrap_response=True)
        assert cfg.wrap_response is True

    def test_script_timeout_defaults_120(self):
        cfg = CronConfig()
        assert cfg.script_timeout_seconds == 120

    def test_script_timeout_overridable(self):
        cfg = CronConfig(script_timeout_seconds=600)
        assert cfg.script_timeout_seconds == 600

    def test_root_config_includes_cron(self):
        c = default_config()
        assert isinstance(c.cron, CronConfig)
        assert c.cron.wrap_response is False
