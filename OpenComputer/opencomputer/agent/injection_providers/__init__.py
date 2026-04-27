"""Bundled :class:`DynamicInjectionProvider` implementations.

Plugins register their own providers via ``api.register_injection_provider``;
the ones here ship with the core and run by default. Today: just
:class:`LinkUnderstandingInjectionProvider` (Tier B item 19).
"""

from __future__ import annotations

from .link_summary import LinkUnderstandingInjectionProvider

__all__ = ["LinkUnderstandingInjectionProvider"]
