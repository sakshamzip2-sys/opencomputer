"""macOS menu-bar indicator — best-effort, no hard dep.

Soft-deps on the optional ``rumps`` extra (``pip install
opencomputer[menubar]``). On Linux/Windows, or on macOS without rumps
installed, ``is_menu_bar_supported()`` returns ``False`` and the
:class:`MenuBarIndicator` raises a clean ``RuntimeError`` if anyone
tries to start it. All ``rumps`` imports are LAZY inside methods so
this module imports cleanly on every platform.

Started by ``opencomputer system-control enable --menu-bar`` in a
daemon thread; stopped on ``disable`` or process exit.
"""

from __future__ import annotations

import importlib
import logging
import platform
import threading
import time

_log = logging.getLogger("opencomputer.system_control.menu_bar")


def is_menu_bar_supported() -> bool:
    """Return True iff this host can run the menu-bar indicator.

    Requires:
      - macOS (``platform.system() == 'Darwin'``)
      - ``rumps`` importable from the active Python env

    No hard dep on ``rumps`` — installable via the optional ``[menubar]``
    extra. Returning False here is the silent skip path; callers print
    a friendlier message if appropriate.
    """
    if platform.system() != "Darwin":
        return False
    try:
        importlib.import_module("rumps")
    except ImportError:
        return False
    except Exception as e:  # noqa: BLE001 — defensive (rumps can't even import)
        _log.debug("rumps import failed unexpectedly: %s", e, exc_info=True)
        return False
    return True


class MenuBarIndicator:
    """Minimal menu-bar indicator for autonomous-mode visibility.

    Title shows a power emoji + status text. Updates every 5 seconds
    based on whether :func:`default_logger` returned a live logger
    (proxy for "system-control still on"). Runs in a daemon thread —
    process exit kills it cleanly.

    Use ``start()`` / ``stop()`` to manage lifecycle. ``start()`` raises
    if the host doesn't support the menu bar.
    """

    def __init__(self, title: str = "⚡ OC") -> None:
        self._title = title
        self._app: object | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Run the rumps app in a daemon thread.

        Raises ``RuntimeError`` if ``is_menu_bar_supported()`` is False.
        """
        if not is_menu_bar_supported():
            raise RuntimeError(
                "menu-bar indicator requires macOS + the optional 'rumps' "
                "extra (`pip install opencomputer[menubar]`)"
            )
        if self._thread is not None and self._thread.is_alive():
            _log.debug("menu bar already running; start() is a no-op")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="opencomputer-menubar", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the indicator to exit. Best-effort — daemon thread."""
        self._stop.set()
        # rumps doesn't have a clean cross-thread quit hook; the daemon
        # nature of the thread means process exit will reap it. We
        # leave the thread reference around for inspection.

    def _run(self) -> None:
        """Thread entry point. Lazy-imports rumps so this file imports
        on every platform."""
        try:
            import rumps  # type: ignore[import-not-found]
        except Exception as e:  # noqa: BLE001 — already gated by is_menu_bar_supported
            _log.warning("menu bar: rumps import failed inside thread: %s", e)
            return

        # Keep a ref so timers can read it.
        try:
            app = rumps.App(name="OpenComputer", title=self._title)
        except Exception as e:  # noqa: BLE001 — rumps init can fail in headless test
            _log.warning("menu bar: rumps.App init failed: %s", e)
            return
        self._app = app

        # Periodic title-refresh thread (separate from rumps' own
        # event loop). We use rumps.timer in a friendlier app, but
        # for our minimal "is system-control still on?" probe, a
        # background ticker that calls app.title = ... is simpler
        # and decoupled from rumps' Timer subclass.
        def _tick() -> None:
            from opencomputer.system_control.logger import default_logger

            while not self._stop.is_set():
                try:
                    on = default_logger() is not None
                    new_title = (
                        f"⚡ OC{'' if on else ' (off)'}"
                        if on
                        else "OC (off)"
                    )
                    if hasattr(app, "title"):
                        app.title = new_title
                except Exception as e:  # noqa: BLE001 — defensive
                    _log.debug("menu bar tick error: %s", e, exc_info=True)
                time.sleep(5.0)

        ticker = threading.Thread(target=_tick, name="opencomputer-menubar-tick", daemon=True)
        ticker.start()

        # rumps.run() blocks until the app quits. Wrapped in try/except
        # so an exception in the menu bar can never propagate up to the
        # CLI; this is purely cosmetic, after all.
        try:
            app.run()
        except Exception as e:  # noqa: BLE001 — best-effort
            _log.warning("menu bar: app.run() raised: %s", e, exc_info=True)


__all__ = ["MenuBarIndicator", "is_menu_bar_supported"]
