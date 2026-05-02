"""Alibaba Coding Plan plugin — registers AlibabaCodingPlanProvider as 'alibaba-coding-plan'."""
from __future__ import annotations

from provider import AlibabaCodingPlanProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("alibaba-coding-plan", AlibabaCodingPlanProvider)
