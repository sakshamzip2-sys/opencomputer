"""``/rollback [N]`` — list / restore filesystem checkpoints.

Hermes-CLI parity (doc line 95). Wraps the existing RewindStore (the
backing store of ``oc checkpoints`` CLI) so users can list and restore
without leaving the chat REPL.

The store is loaded lazily because the ``rewind`` package is an
optional dep — we don't want to break ``/help`` rendering on installs
that lack it.
"""

from __future__ import annotations

import datetime as _dt

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class RollbackCommand(SlashCommand):
    name = "rollback"
    description = "List recent filesystem checkpoints / restore the Nth-most-recent."

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        store = self._resolve_store(runtime)
        if store is None:
            return SlashCommandResult(
                output=(
                    "no checkpoint store available — install `rewind` and set "
                    "`checkpoint.auto_checkpoint = true` in config.yaml, or "
                    "run `oc checkpoints status` to bootstrap."
                ),
                handled=True,
            )

        try:
            checkpoints = list(self._list(store) or [])
        except Exception as exc:  # noqa: BLE001
            return SlashCommandResult(
                output=f"checkpoint listing failed: {type(exc).__name__}: {exc}",
                handled=True,
            )

        if not checkpoints:
            return SlashCommandResult(
                output="no checkpoints recorded yet.", handled=True
            )

        arg = (args or "").strip()
        if not arg:
            return SlashCommandResult(
                output=self._format_listing(checkpoints[:10]),
                handled=True,
            )

        try:
            n = int(arg)
        except ValueError:
            return SlashCommandResult(
                output=(
                    f"invalid arg '{arg}' — use `/rollback` (list) or "
                    "`/rollback N` (restore Nth)"
                ),
                handled=True,
            )
        if n < 1 or n > len(checkpoints):
            return SlashCommandResult(
                output=(
                    f"out of range: {n} (have {len(checkpoints)} checkpoints)"
                ),
                handled=True,
            )
        target = checkpoints[n - 1]
        try:
            self._restore(store, target)
        except Exception as exc:  # noqa: BLE001
            return SlashCommandResult(
                output=f"restore failed: {type(exc).__name__}: {exc}",
                handled=True,
            )
        label = self._field(target, "label", "")
        return SlashCommandResult(
            output=f"restored checkpoint #{n}{f' ({label})' if label else ''}",
            handled=True,
        )

    @staticmethod
    def _resolve_store(runtime: RuntimeContext) -> object | None:
        # 1. Test seam — runtime.custom["_rewind_store"] for unit tests.
        store = runtime.custom.get("_rewind_store")
        if store is not None:
            return store
        # 2. Real RewindStore — optional dep.
        try:
            from rewind.store import RewindStore  # type: ignore[import-not-found]

            from opencomputer.agent.config import default_config
        except ImportError:
            return None
        try:
            cfg = default_config()
            workspace_root = getattr(cfg.checkpoints, "workspace_root", None)
            store_path = getattr(cfg.checkpoints, "store_path", None)
            if not store_path:
                return None
            return RewindStore(store_path, workspace_root=workspace_root)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _list(store: object) -> list[object]:
        for name in ("list_checkpoints", "list", "all"):
            method = getattr(store, name, None)
            if callable(method):
                return list(method())
        return []

    @staticmethod
    def _restore(store: object, target: object) -> None:
        ident = RollbackCommand._field(target, "id", None) or RollbackCommand._field(
            target, "name", None
        )
        for name in ("restore", "rollback_to", "checkout"):
            method = getattr(store, name, None)
            if callable(method):
                method(ident) if ident is not None else method()
                return
        raise RuntimeError("checkpoint store has no restore method")

    @staticmethod
    def _field(obj: object, key: str, default: object) -> object:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _format_listing(checkpoints: list[object]) -> str:
        lines = ["#  ts                   label                  files"]
        for i, ck in enumerate(checkpoints, 1):
            ts_raw = RollbackCommand._field(ck, "ts", 0) or 0
            try:
                ts = _dt.datetime.fromtimestamp(float(ts_raw)).isoformat(
                    sep=" ", timespec="seconds"
                )
            except Exception:  # noqa: BLE001
                ts = "?"
            label = str(RollbackCommand._field(ck, "label", "") or "")[:22]
            files = RollbackCommand._field(ck, "files", 0)
            lines.append(f"{i:>2}  {ts:<19}  {label:<22}  {files}")
        return "\n".join(lines)
