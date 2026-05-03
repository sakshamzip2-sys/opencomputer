"""service_install wizard section — platform-agnostic, calls factory.get_backend()."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_service_install_section_calls_factory_install_when_user_picks_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.cli_setup.section_handlers import service_install
    from opencomputer.cli_setup.sections import SectionResult, WizardCtx

    fake_backend = MagicMock()
    fake_backend.NAME = "launchd"
    fake_backend.supported.return_value = True
    fake_backend.install.return_value = MagicMock(
        backend="launchd", config_path="/tmp/x.plist",
        enabled=True, started=True, notes=[],
    )
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.service_install.radiolist",
        lambda *a, **k: 0,
    )
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        ctx = WizardCtx(
            config={}, config_path=tmp_path / "config.yaml",
            is_first_run=True,
        )
        result = service_install.run_service_install_section(ctx)
    assert result == SectionResult.CONFIGURED
    fake_backend.install.assert_called_once()


def test_service_install_section_skip_returns_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.cli_setup.section_handlers import service_install
    from opencomputer.cli_setup.sections import SectionResult, WizardCtx

    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.service_install.radiolist",
        lambda *a, **k: 1,
    )
    fake_backend = MagicMock()
    fake_backend.NAME = "launchd"
    fake_backend.supported.return_value = True
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        ctx = WizardCtx(
            config={}, config_path=tmp_path / "config.yaml",
            is_first_run=True,
        )
        result = service_install.run_service_install_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH
    fake_backend.install.assert_not_called()


def test_service_install_section_unsupported_platform_skips(
    tmp_path: Path,
) -> None:
    from opencomputer.cli_setup.section_handlers import service_install
    from opencomputer.cli_setup.sections import SectionResult, WizardCtx
    from opencomputer.service.base import ServiceUnsupportedError

    with patch(
        "opencomputer.service.factory.get_backend",
        side_effect=ServiceUnsupportedError("no backend"),
    ):
        ctx = WizardCtx(
            config={}, config_path=tmp_path / "config.yaml",
            is_first_run=True,
        )
        result = service_install.run_service_install_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH
