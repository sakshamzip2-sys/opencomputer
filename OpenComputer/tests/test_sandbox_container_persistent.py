"""Hermes parity: container_persistent: false adds explicit /workspace + /root tmpfs."""
from __future__ import annotations

from plugin_sdk.sandbox import SandboxConfig
from opencomputer.sandbox.docker import DockerStrategy


def test_persistent_default_is_true():
    cfg = SandboxConfig(strategy="docker")
    assert cfg.container_persistent is True


def test_persistent_true_does_not_add_workspace_or_root_tmpfs():
    cfg = SandboxConfig(strategy="docker", container_persistent=True)
    strategy = DockerStrategy()
    argv = strategy.explain(["echo", "hi"], config=cfg)
    tmpfs_targets = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    # Existing tmpfs trio (/tmp, /var/tmp, /run) is unchanged.
    assert any(t.startswith("/tmp:") for t in tmpfs_targets)
    # No /workspace or /root tmpfs in persistent mode.
    assert not any(t.startswith("/workspace:") for t in tmpfs_targets)
    assert not any(t.startswith("/root:") for t in tmpfs_targets)


def test_persistent_false_adds_workspace_and_root_tmpfs():
    cfg = SandboxConfig(strategy="docker", container_persistent=False)
    strategy = DockerStrategy()
    argv = strategy.explain(["echo", "hi"], config=cfg)
    tmpfs_targets = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert any(t.startswith("/workspace:") for t in tmpfs_targets)
    assert any(t.startswith("/root:") for t in tmpfs_targets)
    # Existing tmpfs trio still there.
    assert any(t.startswith("/tmp:") for t in tmpfs_targets)


def test_persistent_false_preserves_explicit_paths():
    """User-declared read_paths still bind in either mode."""
    cfg = SandboxConfig(
        strategy="docker",
        container_persistent=False,
        read_paths=("/etc/resolv.conf",),
    )
    strategy = DockerStrategy()
    argv = strategy.explain(["echo", "hi"], config=cfg)
    argv_str = " ".join(argv)
    assert "/etc/resolv.conf" in argv_str
