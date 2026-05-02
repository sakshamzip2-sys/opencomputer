"""StepFun provider plugin — registers StepFunProvider as 'stepfun'."""
from __future__ import annotations

from provider import StepFunProvider  # type: ignore[import-not-found]


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("stepfun", StepFunProvider)
