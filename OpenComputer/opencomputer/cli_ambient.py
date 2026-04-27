"""CLI: oc ambient {on,off,pause,resume,status,daemon}.

State file at <profile_home>/ambient/state.json. The active profile_home
is resolved via OPENCOMPUTER_PROFILE_HOME env var (testing convenience)
or via opencomputer.agent.config._home() (production).

The status command shows AGGREGATE state only — never specific app names.
This is a hard privacy contract enforced by tests/test_ambient_cli.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import time
import types
from pathlib import Path

import typer

app = typer.Typer(help="Ambient sensor controls — opt-in foreground-app observation.")


# ---------------------------------------------------------------------------
# extensions.ambient_sensors alias — production parity with tests/conftest.py.
#
# The plugin lives at ``extensions/ambient-sensors/`` (hyphenated). Python
# module names use underscores, so we register a synthetic namespace package
# pointing at the hyphenated dir on first import. tests/conftest.py also
# does this for the test runner; this helper makes the same alias available
# when the CLI is invoked outside pytest.
# ---------------------------------------------------------------------------


def _ensure_ambient_sensors_alias() -> None:
    if "extensions.ambient_sensors" in sys.modules:
        return
    project_root = Path(__file__).resolve().parent.parent
    ext_dir = project_root / "extensions"
    ambient_dir = ext_dir / "ambient-sensors"
    if not ambient_dir.exists():
        return
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(ext_dir)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg
    mod = types.ModuleType("extensions.ambient_sensors")
    mod.__path__ = [str(ambient_dir)]
    mod.__package__ = "extensions.ambient_sensors"
    sys.modules["extensions.ambient_sensors"] = mod
    for sub in ("foreground", "sensitive_apps", "pause_state", "daemon", "plugin"):
        full_name = f"extensions.ambient_sensors.{sub}"
        if full_name in sys.modules:
            continue
        init = ambient_dir / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.ambient_sensors"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)


def _profile_home() -> Path:
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME")
    if env:
        return Path(env)
    from opencomputer.agent.config import _home  # lazy: avoid import cycles

    return _home()


def _state_path() -> Path:
    return _profile_home() / "ambient" / "state.json"


def _heartbeat_path() -> Path:
    return _profile_home() / "ambient" / "heartbeat"


_DURATION_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.IGNORECASE)


def _parse_duration(text: str) -> float:
    m = _DURATION_RE.fullmatch(text)
    if not m:
        raise typer.BadParameter("duration must be like '90s', '5m', '1h', '2d'")
    value = float(m.group(1))
    unit = m.group(2).lower()
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


@app.command()
def on() -> None:
    """Enable the ambient foreground sensor."""
    _ensure_ambient_sensors_alias()
    from extensions.ambient_sensors.pause_state import AmbientState, save_state

    save_state(
        _state_path(),
        AmbientState(enabled=True, paused_until=None, sensors=("foreground",)),
    )
    typer.echo(
        "ambient: enabled. The sensor publishes hashed foreground events to the F2 bus."
    )
    typer.echo(
        "Run `opencomputer ambient status` to verify, or `opencomputer ambient off` to disable."
    )


@app.command()
def off() -> None:
    """Disable the ambient foreground sensor."""
    _ensure_ambient_sensors_alias()
    from extensions.ambient_sensors.pause_state import AmbientState, load_state, save_state

    state = load_state(_state_path())
    save_state(
        _state_path(),
        AmbientState(enabled=False, paused_until=None, sensors=state.sensors),
    )
    typer.echo("ambient: disabled.")


@app.command()
def pause(
    duration: str = typer.Option(
        "", "--duration", "-d", help="e.g. 30s, 5m, 1h, 2d. Empty = indefinite."
    ),
) -> None:
    """Pause the sensor without disabling it."""
    _ensure_ambient_sensors_alias()
    from extensions.ambient_sensors.pause_state import AmbientState, load_state, save_state

    state = load_state(_state_path())
    if not state.enabled:
        typer.echo(
            "ambient: sensor is not enabled. Run `opencomputer ambient on` first."
        )
        raise typer.Exit(code=1)
    if duration:
        secs = _parse_duration(duration)
        until = time.time() + secs
        new_state = AmbientState(
            enabled=True, paused_until=until, sensors=state.sensors
        )
        readable = time.strftime("%H:%M:%S", time.localtime(until))
        typer.echo(f"ambient: paused for {duration} (until ~{readable}).")
    else:
        # Indefinite: 100 years
        until = time.time() + 100 * 365 * 86400
        new_state = AmbientState(
            enabled=True, paused_until=until, sensors=state.sensors
        )
        typer.echo(
            "ambient: paused indefinitely. `opencomputer ambient resume` to lift."
        )
    save_state(_state_path(), new_state)


@app.command()
def resume() -> None:
    """Resume after a pause."""
    _ensure_ambient_sensors_alias()
    from extensions.ambient_sensors.pause_state import AmbientState, load_state, save_state

    state = load_state(_state_path())
    save_state(
        _state_path(),
        AmbientState(enabled=state.enabled, paused_until=None, sensors=state.sensors),
    )
    typer.echo("ambient: resumed.")


@app.command()
def status() -> None:
    """Show current state. Aggregate-only — never specific app names."""
    _ensure_ambient_sensors_alias()
    from extensions.ambient_sensors.pause_state import is_currently_paused, load_state

    state = load_state(_state_path())
    if not state.enabled:
        typer.echo("ambient: disabled (sensor is opt-in; not currently enabled).")
        typer.echo("(run `opencomputer ambient on` to enable.)")
        return
    typer.echo(f"enabled: {state.enabled}")
    if is_currently_paused(state):
        until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.paused_until or 0))
        typer.echo(f"paused until: {until}")
    else:
        typer.echo("paused: no")
    hb = _heartbeat_path()
    if hb.exists():
        try:
            ts = float(hb.read_text().strip())
            age = time.time() - ts
            typer.echo(f"last heartbeat: {age:.0f}s ago")
        except (OSError, ValueError):
            typer.echo("last heartbeat: unreadable")
    else:
        typer.echo("last heartbeat: never (daemon not running)")
    typer.echo(f"sensors: {', '.join(state.sensors) if state.sensors else '(none)'}")


@app.command()
def daemon() -> None:
    """Run the ambient sensor daemon standalone (outside gateway)."""
    _ensure_ambient_sensors_alias()
    from extensions.ambient_sensors.daemon import ForegroundSensorDaemon

    from opencomputer.ingestion.bus import default_bus

    typer.echo("ambient daemon: starting (Ctrl+C to stop)")

    async def _run() -> None:
        d = ForegroundSensorDaemon(bus=default_bus, profile_home_factory=_profile_home)
        await d.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("\nambient daemon: stopped")
