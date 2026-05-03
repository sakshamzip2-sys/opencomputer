"""ACP auth helpers — detect the active OC provider for the initialize response."""

from __future__ import annotations


def detect_provider() -> str | None:
    """Return the active provider name from OC's config, or None."""
    try:
        from opencomputer.agent.config_store import load_config

        cfg = load_config()
        provider = cfg.model.provider
        if isinstance(provider, str) and provider.strip():
            return provider.strip().lower()
    except Exception:
        pass
    return None


def has_provider() -> bool:
    """Return True if OC can resolve a runtime provider."""
    return detect_provider() is not None


__all__ = ["detect_provider", "has_provider"]
