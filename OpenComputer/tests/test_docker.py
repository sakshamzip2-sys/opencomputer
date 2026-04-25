"""Structure-validation tests for Dockerfile + docker-compose.yml.

We don't run ``docker build`` in CI (no daemon available), so these tests
parse the files and assert key invariants:

- Dockerfile is multi-stage (builder + runtime).
- Image runs as a non-root user.
- Webhook port (18790) is exposed.
- docker-compose declares the gateway + cron-only profiles.
- docker-compose mounts a named volume for ``~/.opencomputer/``.
- ``.dockerignore`` excludes the predictable "never put this in an image"
  things (``.venv``, ``__pycache__``, ``.git``, ``tests/``).

If the image actually builds is verified by Saksham at deploy time and
optionally by a separate workflow that has docker available.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_COMPOSE = _REPO_ROOT / "docker-compose.yml"
_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_data() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dockerignore_text() -> str:
    return _DOCKERIGNORE.read_text(encoding="utf-8")


class TestDockerfile:
    def test_dockerfile_exists(self) -> None:
        assert _DOCKERFILE.exists()

    def test_python_3_13(self, dockerfile_text: str) -> None:
        # Both stages should base off python:3.13-slim
        from_lines = [ln for ln in dockerfile_text.splitlines() if ln.startswith("FROM ")]
        assert len(from_lines) >= 2, "expected multi-stage build"
        for line in from_lines:
            assert "python:3.13" in line, f"unexpected base image: {line!r}"

    def test_multi_stage(self, dockerfile_text: str) -> None:
        # Look for "AS builder" and "AS runtime"
        assert re.search(r"FROM .* AS builder", dockerfile_text)
        assert re.search(r"FROM .* AS runtime", dockerfile_text)

    def test_runs_as_non_root(self, dockerfile_text: str) -> None:
        # Last USER directive should be a non-root user (we use 'oc')
        users = re.findall(r"^USER\s+(\S+)", dockerfile_text, re.MULTILINE)
        assert users, "no USER directive — would run as root"
        assert users[-1] != "root", "last USER must be non-root"
        assert users[-1] == "oc"

    def test_exposes_webhook_port(self, dockerfile_text: str) -> None:
        # Tier 1.3 webhook adapter binds 18790 by default
        assert re.search(r"^EXPOSE\s+.*\b18790\b", dockerfile_text, re.MULTILINE)

    def test_uses_tini_init(self, dockerfile_text: str) -> None:
        # PID 1 should be tini so signals propagate cleanly under `docker stop`
        assert "tini" in dockerfile_text.lower()
        assert 'ENTRYPOINT ["/usr/bin/tini"' in dockerfile_text

    def test_persistent_home_env(self, dockerfile_text: str) -> None:
        # OPENCOMPUTER_HOME points inside the container's home so volume mount
        # at /home/oc/.opencomputer captures all profile state.
        assert "ENV OPENCOMPUTER_HOME=" in dockerfile_text

    def test_pyproject_in_build_stage(self, dockerfile_text: str) -> None:
        # Builder stage installs from pyproject.toml so deps are layer-cached
        assert "COPY pyproject.toml" in dockerfile_text


class TestCompose:
    def test_compose_parses(self, compose_data: dict) -> None:
        assert isinstance(compose_data, dict)
        assert "services" in compose_data

    def test_two_profiles(self, compose_data: dict) -> None:
        services = compose_data["services"]
        assert "gateway" in services
        assert "cron-only" in services

    def test_gateway_has_webhook_port_mapped(self, compose_data: dict) -> None:
        ports = compose_data["services"]["gateway"].get("ports", [])
        assert any("18790" in str(p) for p in ports), f"webhook port not exposed: {ports!r}"

    def test_named_volume_for_persistence(self, compose_data: dict) -> None:
        volumes = compose_data.get("volumes") or {}
        assert "oc-data" in volumes
        # And both services mount it
        for svc in ("gateway", "cron-only"):
            mounts = compose_data["services"][svc].get("volumes") or []
            assert any("oc-data" in str(m) for m in mounts), f"{svc} doesn't mount oc-data"

    def test_restart_unless_stopped(self, compose_data: dict) -> None:
        # Both services should auto-restart on crash but respect `docker stop`
        for svc in ("gateway", "cron-only"):
            assert compose_data["services"][svc].get("restart") == "unless-stopped"

    def test_provider_keys_in_env(self, compose_data: dict) -> None:
        env = compose_data["services"]["gateway"].get("environment") or []
        assert any("ANTHROPIC_API_KEY" in str(e) for e in env)
        assert any("TELEGRAM_BOT_TOKEN" in str(e) for e in env)


class TestDockerIgnore:
    @pytest.mark.parametrize(
        "pattern",
        [
            ".venv/",
            "__pycache__/",
            ".git/",
            "tests/",
            ".pytest_cache/",
            ".DS_Store",
        ],
    )
    def test_excludes(self, dockerignore_text: str, pattern: str) -> None:
        assert pattern in dockerignore_text, f"{pattern!r} should be in .dockerignore"
