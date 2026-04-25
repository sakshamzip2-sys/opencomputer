"""PR-1: verify AgentLoop init calls bootstrap_if_enabled and never raises."""
from unittest.mock import MagicMock, patch


def test_agentloop_init_calls_bootstrap_if_enabled(tmp_path, monkeypatch):
    """Init calls bootstrap_if_enabled exactly once."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    with patch("opencomputer.evolution.trajectory.bootstrap_if_enabled") as mock_boot:
        mock_boot.return_value = None
        from opencomputer.agent.config import Config
        from opencomputer.agent.loop import AgentLoop
        provider = MagicMock()
        loop = AgentLoop(provider=provider, config=Config())
        mock_boot.assert_called_once()
        assert loop._evolution_subscription is None


def test_agentloop_init_swallows_bootstrap_exception(tmp_path, monkeypatch):
    """If bootstrap_if_enabled raises, AgentLoop init still succeeds."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    with patch("opencomputer.evolution.trajectory.bootstrap_if_enabled") as mock_boot:
        mock_boot.side_effect = RuntimeError("boom")
        from opencomputer.agent.config import Config
        from opencomputer.agent.loop import AgentLoop
        provider = MagicMock()
        loop = AgentLoop(provider=provider, config=Config())  # MUST NOT raise
        assert loop._evolution_subscription is None
