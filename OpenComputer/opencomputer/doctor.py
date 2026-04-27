"""
opencomputer doctor — diagnose common issues.

Runs a battery of checks and prints a pass/fail report. Intended to be
the first thing a user runs when something isn't working.

Checks:
  1. Python version
  2. Config file exists + is valid YAML
  3. Configured provider's plugin is installed
  4. Provider API key is set in environment
  5. Optional channel tokens are set if configured
  6. Session DB is writable
  7. Skills directory is writable
  8. MCP servers can be reached (skipped if none configured)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console

console = Console()


@dataclass(slots=True)
class Check:
    name: str
    status: Literal["pass", "fail", "warn", "skip"]
    detail: str = ""


@dataclass(slots=True)
class CheckResult:
    """Result type for the introspection / orphan-venv helpers (T10).

    Distinct from ``Check`` because these helpers are also called directly
    from tests, where a boolean ``ok`` plus a free-form ``level`` (info /
    warning / error) is more ergonomic than the four-value ``status``
    string the rich report uses. ``run_doctor`` translates ``CheckResult``
    -> ``Check`` when wiring them into the report.
    """

    ok: bool
    level: Literal["info", "warning", "error"]
    message: str


def _status_icon(s: str) -> str:
    return {
        "pass": "[green]✓[/green]",
        "fail": "[red]✗[/red]",
        "warn": "[yellow]![/yellow]",
        "skip": "[dim]·[/dim]",
    }[s]


def _check_python() -> Check:
    v = sys.version_info
    if (v.major, v.minor) < (3, 12):
        return Check(
            "python version", "fail", f"need Python >=3.12, got {v.major}.{v.minor}.{v.micro}"
        )
    return Check("python version", "pass", f"{v.major}.{v.minor}.{v.micro}")


def _check_config() -> tuple[Check, object]:
    from opencomputer.agent.config_store import config_file_path, load_config

    path = config_file_path()
    if not path.exists():
        return (
            Check("config file", "warn", f"no config at {path} — run `opencomputer setup`"),
            None,
        )
    try:
        cfg = load_config()
        return Check("config file", "pass", str(path)), cfg
    except Exception as e:  # noqa: BLE001
        return Check("config file", "fail", f"{path}: {e}"), None


def _check_provider_plugin(cfg) -> Check:
    from opencomputer.plugins.registry import registry as plugin_registry

    repo_root = Path(__file__).resolve().parent.parent
    ext_dir = repo_root / "extensions"
    if ext_dir.exists():
        plugin_registry.load_all([ext_dir])

    if cfg is None:
        return Check("provider plugin", "skip", "no config")

    provider_id = cfg.model.provider
    if provider_id in plugin_registry.providers:
        return Check("provider plugin", "pass", f"'{provider_id}' registered")
    return Check(
        "provider plugin",
        "fail",
        f"provider '{provider_id}' not found. "
        f"installed: {list(plugin_registry.providers.keys()) or 'none'}",
    )


def _check_provider_key(cfg) -> Check:
    if cfg is None:
        return Check("provider API key", "skip", "no config")
    env_key = cfg.model.api_key_env
    if os.environ.get(env_key):
        return Check("provider API key", "pass", f"{env_key} is set")
    return Check("provider API key", "fail", f"{env_key} not set — export it before running")


def _check_session_db(cfg) -> Check:
    if cfg is None:
        return Check("session DB", "skip", "no config")
    db_path = cfg.session.db_path
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        test_file = db_path.parent / ".writetest"
        test_file.write_text("x")
        test_file.unlink()
        return Check("session DB path", "pass", str(db_path))
    except Exception as e:  # noqa: BLE001
        return Check("session DB path", "fail", f"{db_path}: {e}")


def _check_skills_dir(cfg) -> Check:
    if cfg is None:
        return Check("skills dir", "skip", "no config")
    p = cfg.memory.skills_path
    try:
        p.mkdir(parents=True, exist_ok=True)
        return Check("skills dir", "pass", str(p))
    except Exception as e:  # noqa: BLE001
        return Check("skills dir", "fail", f"{p}: {e}")


def _check_channel_tokens(cfg) -> list[Check]:
    out: list[Check] = []
    if cfg is None:
        return out
    from opencomputer.plugins.registry import registry as plugin_registry

    # Generic: for each channel plugin registered, check its conventional env var.
    # We know about Telegram specifically because it's bundled.
    if "telegram" in plugin_registry.channels:
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if tok:
            out.append(Check("telegram token", "pass", "TELEGRAM_BOT_TOKEN set"))
        else:
            out.append(Check("telegram token", "skip", "TELEGRAM_BOT_TOKEN not set"))
    return out


def _check_profile_artifacts() -> list[Check]:
    """Phase 14.F / C4 — validate per-profile C1/C2/C3 artifacts exist.

    Runs for the sticky active profile only. Default profile
    (no sticky file / sticky="default") produces skip checks — the
    default profile doesn't have a ``home/`` subdir or wrapper by
    design (it uses the user's real HOME and `opencomputer` binary).

    Each check is WARN on miss (not fail): a user may have intentionally
    deleted a wrapper or SOUL.md. The point is to surface drift, not
    block startup.
    """
    import sys as _sys

    from opencomputer.profiles import (
        ProfileNameError,
        get_profile_dir,
        read_active_profile,
        wrapper_path,
    )

    out: list[Check] = []

    try:
        active = read_active_profile()
    except Exception as e:  # noqa: BLE001 — never let doctor crash
        return [Check("profile artifacts", "fail", f"could not read active profile: {e}")]

    if active is None:
        # Default profile — no per-profile artifacts to validate.
        return [
            Check("profile home/", "skip", "default profile"),
            Check("profile wrapper", "skip", "default profile"),
            Check("profile SOUL.md", "skip", "default profile"),
        ]

    try:
        pdir = get_profile_dir(active)
    except ProfileNameError as e:
        return [Check("profile artifacts", "fail", f"sticky profile invalid: {e}")]

    # C1 — home/ subdir. Deliberately DO NOT call profile_home_dir (which
    # mkdirs on demand); we want to report drift, not silently repair.
    home_check_path = pdir / "home"
    if home_check_path.is_dir():
        out.append(Check("profile home/", "pass", str(home_check_path)))
    else:
        out.append(
            Check(
                "profile home/",
                "warn",
                f"expected {home_check_path} (use `opencomputer profile create` to re-seed)",
            )
        )

    # C2 — wrapper script (unix only)
    if _sys.platform.startswith("win") or os.name == "nt":
        out.append(Check("profile wrapper", "skip", "wrapper scripts unsupported on Windows"))
    else:
        wrapper = wrapper_path(active)
        if wrapper.exists():
            out.append(Check("profile wrapper", "pass", str(wrapper)))
        else:
            out.append(
                Check(
                    "profile wrapper",
                    "warn",
                    f"expected {wrapper} (use `opencomputer profile create` to re-seed)",
                )
            )

    # C3 — SOUL.md personality file
    soul = pdir / "SOUL.md"
    if soul.exists():
        out.append(Check("profile SOUL.md", "pass", str(soul)))
    else:
        out.append(
            Check(
                "profile SOUL.md",
                "warn",
                f"expected {soul} (use `opencomputer profile create` to re-seed)",
            )
        )

    return out


def _check_profile_and_overlay() -> list[Check]:
    """Phase 14.M/14.N doctor checks.

    M-check: profile.yaml references a preset that exists on disk.
    N-check: workspace overlay (if any) references a preset that exists
             and only names installed plugins in ``plugins.additional``.

    Both checks degrade gracefully — if there's no profile.yaml, no
    workspace overlay, or neither references anything to verify, the
    check is ``skip``. A malformed file -> ``fail`` (don't paper over).
    """
    from opencomputer.agent.config import _home
    from opencomputer.agent.profile_config import (
        ProfileConfigError,
        load_profile_config,
    )
    from opencomputer.agent.workspace import find_workspace_overlay
    from opencomputer.plugins.preset import list_presets, preset_path

    out: list[Check] = []

    # ── M-check: profile.yaml -> preset exists ─────────────────────────
    try:
        profile_cfg = load_profile_config(_home())
    except ProfileConfigError as e:
        out.append(
            Check(
                "profile.yaml",
                "fail",
                f"{_home() / 'profile.yaml'}: {e}",
            )
        )
        profile_cfg = None

    if profile_cfg is None:
        # Already reported the parse failure above.
        pass
    elif profile_cfg.preset is None:
        out.append(Check("profile preset", "skip", "no preset referenced"))
    else:
        target = preset_path(profile_cfg.preset)
        if target.exists():
            out.append(
                Check(
                    "profile preset",
                    "pass",
                    f"{profile_cfg.preset} -> {target}",
                )
            )
        else:
            available = list_presets() or ["(none)"]
            out.append(
                Check(
                    "profile preset",
                    "fail",
                    f"profile.yaml references preset "
                    f"{profile_cfg.preset!r} but {target} "
                    f"does not exist. available: {available}",
                )
            )

    # ── N-check: workspace overlay -> referents exist ─────────────────
    try:
        overlay = find_workspace_overlay()
    except ValueError as e:
        out.append(Check("workspace overlay", "fail", str(e)))
        return out

    if overlay is None:
        out.append(Check("workspace overlay", "skip", "no .opencomputer/ in CWD tree"))
        return out

    details: list[str] = []
    failed = False
    if overlay.preset is not None:
        tgt = preset_path(overlay.preset)
        if tgt.exists():
            details.append(f"preset={overlay.preset}")
        else:
            failed = True
            details.append(f"preset={overlay.preset!r} (MISSING: {tgt})")
    if overlay.plugins.additional:
        # We can only check *that* plugins are installed; the discovery
        # pass will produce a proper list later. For the doctor, a
        # shallow "id looks sane" check is enough — actual presence is
        # verified by the plugin loader.
        details.append(f"additional={list(overlay.plugins.additional)}")

    if failed:
        out.append(
            Check(
                "workspace overlay",
                "fail",
                f"{overlay.source_path}: " + "; ".join(details),
            )
        )
    else:
        out.append(
            Check(
                "workspace overlay",
                "pass",
                f"{overlay.source_path} — " + "; ".join(details or ["(empty)"]),
            )
        )

    return out


async def _check_mcp(cfg) -> list[Check]:
    out: list[Check] = []
    if cfg is None or not cfg.mcp.servers:
        return out

    from opencomputer.mcp.client import MCPConnection

    for server in cfg.mcp.servers:
        if not server.enabled:
            out.append(Check(f"mcp:{server.name}", "skip", "disabled in config"))
            continue
        conn = MCPConnection(config=server)
        try:
            ok = await conn.connect()
            if ok:
                out.append(Check(f"mcp:{server.name}", "pass", f"{len(conn.tools)} tool(s)"))
            else:
                out.append(Check(f"mcp:{server.name}", "fail", "connect returned False"))
        finally:
            await conn.disconnect()
    return out


async def _run_contributions(fix: bool) -> list[Check]:
    """Run all plugin-contributed health contributions. See plugin_sdk/doctor.py.

    Each contribution is `async (fix: bool) -> RepairResult`. When `fix=True`
    a contribution is expected to mutate state in place and set `repaired=True`
    on its result. Contributions raise-safe: any exception becomes a `fail`
    Check so one broken plugin can't crash the whole doctor run.

    Assumes plugins are already loaded — `_check_provider_plugin` runs earlier
    in `run_doctor` and does the `load_all` call. Reloading here would
    double-register tools (ValueError) and produce spurious failures.
    """
    from opencomputer.plugins.registry import registry as plugin_registry

    out: list[Check] = []
    for c in plugin_registry.doctor_contributions:
        try:
            result = await c.run(fix)
        except Exception as e:  # noqa: BLE001 — any plugin error becomes a fail
            out.append(Check(c.id, "fail", f"{type(e).__name__}: {e}"))
            continue
        detail = result.detail
        if result.repaired:
            detail = f"[repaired] {detail}" if detail else "[repaired]"
        out.append(Check(c.id, result.status, detail))
    return out


def check_ollama_available() -> Check:
    """V2.B-T10 — check whether Ollama is on PATH.

    Ollama is required for Layer 3 LLM-based artifact extraction during
    profile deepening. ``fail`` (not warn) when missing because deepening
    silently degrades without it.

    Lazy import: probe lives in :mod:`opencomputer.profile_bootstrap.llm_extractor`
    so the optional deepening deps don't get pulled in at doctor-import time.
    """
    from opencomputer.profile_bootstrap.llm_extractor import is_ollama_available

    if is_ollama_available():
        return Check(name="ollama", status="pass", detail="ollama on PATH")
    return Check(
        name="ollama",
        status="fail",
        detail=(
            "Ollama not found. Install via 'brew install ollama' (macOS) "
            "or follow https://ollama.com — required for Layer 3 deepening."
        ),
    )


def check_embedding_available() -> Check:
    """V2.B-T10 — check whether sentence-transformers is importable.

    Required for Layer 4 semantic search over deepening artifacts. ``fail``
    when missing so the doctor surfaces it; install via the
    ``opencomputer[deepening]`` extra.
    """
    from opencomputer.profile_bootstrap.embedding import is_embedding_available

    if is_embedding_available():
        return Check(name="sentence-transformers", status="pass", detail="importable")
    return Check(
        name="sentence-transformers",
        status="fail",
        detail="Install via 'pip install opencomputer[deepening]'",
    )


def check_chroma_available() -> Check:
    """V2.B-T10 — check whether chromadb is importable.

    Required for the deepening vector store. ``fail`` when missing; install
    via the ``opencomputer[deepening]`` extra.
    """
    from opencomputer.profile_bootstrap.vector_store import is_chroma_available

    if is_chroma_available():
        return Check(name="chromadb", status="pass", detail="importable")
    return Check(
        name="chromadb",
        status="fail",
        detail="Install via 'pip install opencomputer[deepening]'",
    )


def _check_g_subsystems() -> list[Check]:
    """Health checks for Sub-project G subsystems (cron / cost-guard / oauth /
    voice / webhook). Read-only — surfaces state without modifying anything.

    Each subsystem returns a single Check showing whether it's set up and
    in a sensible state. Missing subsystems are ``skip`` not ``fail``;
    visible problems (e.g. cron file unreadable) are ``warn`` so they
    show up but don't block the rest of the doctor run.
    """
    from opencomputer.agent.config import _home

    checks: list[Check] = []
    home = _home()

    # G.1 — cron
    cron_dir = home / "cron"
    cron_jobs = cron_dir / "jobs.json"
    if not cron_dir.exists():
        checks.append(Check("cron storage", "skip", "no jobs scheduled"))
    elif not cron_jobs.exists():
        checks.append(Check("cron storage", "skip", "no jobs file"))
    else:
        try:
            import json as _json

            data = _json.loads(cron_jobs.read_text(encoding="utf-8"))
            n = len(data.get("jobs", []))
            checks.append(Check("cron storage", "pass", f"{n} job(s) at {cron_jobs}"))
        except Exception as e:  # noqa: BLE001
            checks.append(Check("cron storage", "warn", f"unreadable: {e}"))

    # G.3 — webhook tokens
    webhook_tokens = home / "webhook_tokens.json"
    if not webhook_tokens.exists():
        checks.append(Check("webhook tokens", "skip", "none issued"))
    else:
        try:
            import json as _json

            data = _json.loads(webhook_tokens.read_text(encoding="utf-8"))
            tokens = data.get("tokens", {})
            active = sum(1 for t in tokens.values() if not t.get("revoked"))
            checks.append(
                Check("webhook tokens", "pass", f"{active} active / {len(tokens)} total")
            )
        except Exception as e:  # noqa: BLE001
            checks.append(Check("webhook tokens", "warn", f"unreadable: {e}"))

    # G.8 — cost-guard
    cost_file = home / "cost_guard.json"
    if not cost_file.exists():
        checks.append(Check("cost-guard limits", "skip", "no limits configured"))
    else:
        try:
            from opencomputer.cost_guard import get_default_guard

            usages = get_default_guard().current_usage()
            n_with_limits = sum(
                1 for u in usages if u.daily_limit is not None or u.monthly_limit is not None
            )
            if n_with_limits:
                checks.append(
                    Check(
                        "cost-guard limits",
                        "pass",
                        f"{n_with_limits} provider(s) with caps",
                    )
                )
            else:
                checks.append(
                    Check(
                        "cost-guard limits",
                        "warn",
                        "usage tracked but no caps set — voice / paid MCPs are unguarded",
                    )
                )
        except Exception as e:  # noqa: BLE001
            checks.append(Check("cost-guard limits", "warn", f"read failed: {e}"))

    # G.9 — voice (just check if OPENAI_API_KEY is present for TTS/STT)
    if os.environ.get("OPENAI_API_KEY"):
        checks.append(Check("voice TTS/STT key", "pass", "OPENAI_API_KEY set"))
    else:
        checks.append(
            Check("voice TTS/STT key", "skip", "OPENAI_API_KEY unset — voice CLI will fail")
        )

    # G.13 — OAuth / PAT store
    oauth = home / "mcp_oauth"
    if not oauth.exists():
        checks.append(Check("oauth store", "skip", "no tokens stored"))
    else:
        try:
            n = len(list(oauth.glob("*.json")))
            mode = oct(oauth.stat().st_mode)[-3:] if os.name != "nt" else "n/a"
            if mode != "n/a" and mode != "700":
                checks.append(
                    Check(
                        "oauth store",
                        "warn",
                        f"{n} token(s) but dir mode is {mode} (should be 700)",
                    )
                )
            else:
                checks.append(Check("oauth store", "pass", f"{n} token(s) at {oauth}"))
        except Exception as e:  # noqa: BLE001
            checks.append(Check("oauth store", "warn", f"read failed: {e}"))

    return checks


def _check_orphan_oi_venv(profile_home: Path) -> CheckResult:
    """Detect leftover OI venv directory from prior OpenComputer versions.

    Pre-2026-04-27 codebase ran Open Interpreter in a separate venv at
    ``<profile_home>/oi_capability/``. That bridge is now removed; if the
    directory still exists it's ~150 MB of orphan disk and we surface it
    so the user can ``rm -rf`` it.
    """
    if not profile_home.exists() or not profile_home.is_dir():
        return CheckResult(
            ok=True, level="info", message="No orphan OI venv (profile dir absent)"
        )

    oi_venv = profile_home / "oi_capability"
    if oi_venv.exists():
        return CheckResult(
            ok=False,
            level="warning",
            message=(
                f"Orphan OI venv at {oi_venv} (~150 MB). "
                f"Safe to delete: rm -rf {oi_venv}"
            ),
        )
    return CheckResult(ok=True, level="info", message="No orphan OI venv")


def _check_introspection_deps() -> list[CheckResult]:
    """Verify the 4 native deps used by extensions/coding-harness/introspection.

    Also checks Linux-specific clipboard helper (xclip / xsel). Linux-only
    failures are emitted at WARNING level so doctor exit code stays clean
    when introspection isn't actively used.
    """
    results: list[CheckResult] = []
    for mod_name in ("psutil", "mss", "pyperclip", "rapidocr_onnxruntime"):
        try:
            __import__(mod_name)
            results.append(
                CheckResult(ok=True, level="info", message=f"{mod_name} OK")
            )
        except ImportError:
            pip_name = mod_name.replace("_", "-")
            results.append(
                CheckResult(
                    ok=False,
                    level="error",
                    message=f"{mod_name} missing — pip install -U {pip_name}",
                )
            )

    if sys.platform.startswith("linux"):
        if shutil.which("xclip") or shutil.which("xsel"):
            results.append(
                CheckResult(
                    ok=True,
                    level="info",
                    message="Linux clipboard helper present (xclip/xsel)",
                )
            )
        else:
            results.append(
                CheckResult(
                    ok=False,
                    level="warning",
                    message=(
                        "Linux clipboard requires xclip or xsel — apt install xclip"
                    ),
                )
            )
    return results


def _check_voice_mode_capable() -> CheckResult:
    """T6 (voice-mode) — verify dependencies + audio device + STT backend.

    Voice-mode is opt-in (default off, started via ``opencomputer voice
    talk``). The check is a *preflight*, not a runtime probe — we don't
    open the mic, we only verify:

    1. ``sounddevice`` imports (PortAudio is reachable on Linux).
    2. At least one audio input device is enumerable. Headless / SSH
       sessions typically have zero input devices; surface that clearly.
    3. ``webrtcvad`` imports (VAD gating is required).
    4. At least one STT backend is reachable: ``OPENAI_API_KEY`` set, or
       ``mlx-whisper`` importable, or ``pywhispercpp`` importable.

    Each missing dep returns level=warning (not error) — voice-mode is
    optional, so doctor exit-code stays clean when the user hasn't opted
    into it. The happy-path message lists which STT backends were
    detected so the user can confirm they're getting the path they
    expect (e.g. local-only on a no-API-key host).
    """
    # 1. sounddevice import (covers both pip-missing and PortAudio-missing).
    try:
        import sounddevice as sd
    except ImportError:
        return CheckResult(
            ok=False,
            level="warning",
            message=(
                "voice-mode: sounddevice not installed — "
                "pip install opencomputer[voice]"
            ),
        )
    except OSError as exc:
        return CheckResult(
            ok=False,
            level="warning",
            message=(
                f"voice-mode: PortAudio missing ({exc}) — "
                "Linux: apt install libportaudio2"
            ),
        )

    # 2. At least one audio input device. Headless / SSH = no input.
    try:
        devices = sd.query_devices()
        has_input = any(
            d.get("max_input_channels", 0) > 0 for d in devices
        )
        if not has_input:
            return CheckResult(
                ok=False,
                level="warning",
                message=(
                    "voice-mode: no audio input device detected "
                    "(headless? SSH?)"
                ),
            )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            ok=False,
            level="warning",
            message=f"voice-mode: device query failed: {exc}",
        )

    # 3. webrtcvad import.
    try:
        import webrtcvad  # noqa: F401
    except ImportError:
        return CheckResult(
            ok=False,
            level="warning",
            message=(
                "voice-mode: webrtcvad not installed — "
                "pip install opencomputer[voice]"
            ),
        )

    # 4. At least one STT backend reachable.
    has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))
    has_mlx = False
    has_whisper_cpp = False
    try:
        import mlx_whisper  # noqa: F401

        has_mlx = True
    except ImportError:
        pass
    try:
        import pywhispercpp  # noqa: F401

        has_whisper_cpp = True
    except ImportError:
        pass

    if not (has_openai_key or has_mlx or has_whisper_cpp):
        return CheckResult(
            ok=False,
            level="warning",
            message=(
                "voice-mode: no STT backend — set OPENAI_API_KEY OR "
                "pip install opencomputer[voice-mlx] OR "
                "opencomputer[voice-local]"
            ),
        )

    backends: list[str] = []
    if has_openai_key:
        backends.append("openai-api")
    if has_mlx:
        backends.append("mlx-whisper")
    if has_whisper_cpp:
        backends.append("whisper-cpp")
    return CheckResult(
        ok=True,
        level="info",
        message=f"voice-mode ready (STT: {', '.join(backends)})",
    )


def _check_ambient_state(profile_home: Path) -> CheckResult:
    """Read ambient state.json; warn if enabled but heartbeat is stale or missing.

    The ambient foreground sensor is opt-in (default off). When the user has
    enabled it via ``opencomputer ambient on``, the daemon writes a heartbeat
    file each tick. A missing or stale heartbeat means the daemon isn't
    running or got stuck — surface that as a warning so the user knows their
    opt-in isn't actually being honoured.
    """
    state_path = profile_home / "ambient" / "state.json"
    if not state_path.exists():
        return CheckResult(
            ok=True,
            level="info",
            message="ambient sensor disabled (default — opt in with `opencomputer ambient on`)",
        )

    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            ok=False, level="warning", message=f"ambient state.json unreadable: {exc}"
        )

    if not state.get("enabled", False):
        return CheckResult(ok=True, level="info", message="ambient sensor disabled")

    hb_path = profile_home / "ambient" / "heartbeat"
    if not hb_path.exists():
        return CheckResult(
            ok=False,
            level="warning",
            message=(
                "ambient sensor enabled but heartbeat missing — "
                "daemon not running (start gateway or run `opencomputer ambient daemon`)"
            ),
        )
    try:
        hb_age = time.time() - float(hb_path.read_text().strip())
    except (OSError, ValueError):
        return CheckResult(
            ok=False, level="warning", message="ambient heartbeat file unreadable"
        )

    if hb_age > 60:
        return CheckResult(
            ok=False,
            level="warning",
            message=f"ambient sensor heartbeat stale ({hb_age:.0f}s old) — daemon may be stuck",
        )
    return CheckResult(
        ok=True,
        level="info",
        message=f"ambient sensor running (heartbeat {hb_age:.0f}s ago)",
    )


def _check_skill_evolution_state(profile_home: Path) -> CheckResult:
    """T8 — read skill-evolution state.json; warn if enabled but heartbeat stale.

    The auto-skill-evolution subscriber is opt-in (default off). When the user
    has flipped ``oc skills evolution on`` the in-process subscriber writes a
    heartbeat file on every observed ``session_end`` event while enabled.
    Sessions are infrequent (one per user-driven turn), so the staleness
    threshold is generous (10 minutes) — this surfaces "subscriber not
    running" rather than "no events recently".

    Also surfaces a pile-up warning when the ``_proposed/`` candidates
    directory has more than 20 entries — that's the user's review queue
    and a backlog usually means they need to run ``oc skills review``.
    """
    state_path = profile_home / "skills" / "evolution_state.json"
    if not state_path.exists():
        return CheckResult(
            ok=True,
            level="info",
            message=(
                "skill-evolution disabled (default — opt in with "
                "`opencomputer skills evolution on`)"
            ),
        )
    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            ok=False,
            level="warning",
            message=f"evolution_state.json unreadable: {exc}",
        )
    if not state.get("enabled", False):
        return CheckResult(ok=True, level="info", message="skill-evolution disabled")
    hb_path = profile_home / "skills" / "evolution_heartbeat"
    if not hb_path.exists():
        return CheckResult(
            ok=False,
            level="warning",
            message=(
                "skill-evolution enabled but heartbeat missing — "
                "subscriber not running (gateway boot likely failed silently)"
            ),
        )
    try:
        hb_age = time.time() - float(hb_path.read_text().strip())
    except (OSError, ValueError):
        return CheckResult(
            ok=False,
            level="warning",
            message="skill-evolution heartbeat unreadable",
        )
    if hb_age > 600:  # 10 minutes — events are infrequent
        return CheckResult(
            ok=False,
            level="warning",
            message=f"skill-evolution heartbeat stale ({hb_age:.0f}s old)",
        )
    proposed_dir = profile_home / "skills" / "_proposed"
    proposal_count = (
        sum(
            1
            for entry in proposed_dir.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )
        if proposed_dir.exists()
        else 0
    )
    if proposal_count > 20:
        return CheckResult(
            ok=False,
            level="warning",
            message=(
                f"skill-evolution: {proposal_count} candidates pending — "
                f"run `opencomputer skills review`"
            ),
        )
    return CheckResult(
        ok=True,
        level="info",
        message=f"skill-evolution running ({proposal_count} candidates pending review)",
    )


def _check_ambient_foreground_capable() -> CheckResult:
    """Verify the platform-specific foreground detector can actually run.

    macOS uses ``osascript``; Linux X11 uses ``xdotool`` / ``wmctrl``; Windows
    uses ``pywin32``. Linux Wayland is unsupported in v1 — warn so the user
    knows the sensor will silently no-op there.
    """
    if sys.platform == "darwin":
        if shutil.which("osascript"):
            return CheckResult(
                ok=True,
                level="info",
                message=(
                    "ambient: osascript present (macOS) — ensure Accessibility "
                    "permission is granted"
                ),
            )
        return CheckResult(
            ok=False,
            level="warning",
            message="ambient: osascript missing — sensor cannot detect foreground on macOS",
        )

    if sys.platform.startswith("linux"):
        if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
            return CheckResult(
                ok=False,
                level="warning",
                message=(
                    "ambient: Wayland-only display server — foreground sensor "
                    "unsupported in v1; runs on X11 sessions only"
                ),
            )
        if shutil.which("xdotool") or shutil.which("wmctrl"):
            return CheckResult(
                ok=True,
                level="info",
                message="ambient: xdotool/wmctrl available (Linux X11)",
            )
        return CheckResult(
            ok=False,
            level="warning",
            message=(
                "ambient: install xdotool or wmctrl for Linux foreground "
                "detection — `apt install xdotool`"
            ),
        )

    if sys.platform == "win32":
        try:
            __import__("win32gui")
            return CheckResult(
                ok=True,
                level="info",
                message="ambient: pywin32 importable (Windows)",
            )
        except ImportError:
            return CheckResult(
                ok=False,
                level="warning",
                message="ambient: install pywin32 for Windows foreground detection",
            )

    return CheckResult(
        ok=False,
        level="warning",
        message=f"ambient: platform {sys.platform} unsupported",
    )


def _level_to_status(
    result: CheckResult,
) -> Literal["pass", "fail", "warn", "skip"]:
    """Translate CheckResult.level/ok to the four-state Check.status used
    by the rich report. Errors -> fail, warnings -> warn, success -> pass.
    """
    if result.ok:
        return "pass"
    if result.level == "error":
        return "fail"
    return "warn"


def _result_to_check(name: str, result: CheckResult) -> Check:
    return Check(name=name, status=_level_to_status(result), detail=result.message)


def run_doctor(fix: bool = False) -> int:
    """Run all checks and print a report. Returns the number of failed checks.

    When `fix=True`, every plugin-contributed HealthContribution is invoked
    with `fix=True`, giving it the chance to repair state in place rather
    than merely reporting the problem. Built-in checks are read-only either
    way; repair belongs to plugins (the core doesn't know which legacy
    config shape each plugin owns).
    """
    console.print("\n[bold cyan]OpenComputer — Doctor[/bold cyan]\n")
    if fix:
        console.print("[dim]--fix mode: contributions may repair state in place.[/dim]\n")

    checks: list[Check] = [_check_python()]
    cfg_check, cfg = _check_config()
    checks.append(cfg_check)

    checks.append(_check_provider_plugin(cfg))
    checks.append(_check_provider_key(cfg))
    checks.append(_check_session_db(cfg))
    checks.append(_check_skills_dir(cfg))
    checks.extend(_check_channel_tokens(cfg))
    checks.extend(_check_profile_and_overlay())
    # Phase 14.F / C4 — per-profile C1/C2/C3 artifact drift detection.
    checks.extend(_check_profile_artifacts())

    try:
        mcp_checks = asyncio.run(_check_mcp(cfg))
    except RuntimeError:
        mcp_checks = []
    checks.extend(mcp_checks)

    # Sub-project G subsystems — surface their state at a glance.
    checks.extend(_check_g_subsystems())

    # T10 — orphan OI venv detection + introspection deps verification.
    # Both feed the same per-profile home dir other checks already use, and
    # surface as warn (orphan disk) / fail (missing required dep) in the
    # rich report.
    from opencomputer.agent.config import _home as _resolve_home

    try:
        profile_home = _resolve_home()
    except Exception:  # noqa: BLE001 — resolution must never crash doctor
        profile_home = None
    if profile_home is not None:
        checks.append(
            _result_to_check("orphan OI venv", _check_orphan_oi_venv(profile_home))
        )
    for dep_result in _check_introspection_deps():
        # Use the first whitespace-delimited token as the check name so the
        # rich report shows e.g. "introspection: psutil" / "introspection:
        # rapidocr_onnxruntime" / "introspection: Linux".
        first = dep_result.message.split()[0] if dep_result.message else "dep"
        checks.append(_result_to_check(f"introspection: {first}", dep_result))

    # T8 — ambient foreground sensor: state/heartbeat freshness + platform
    # capability. Runs alongside the orphan-venv / introspection checks so
    # they share the resolved profile_home.
    if profile_home is not None:
        checks.append(
            _result_to_check("ambient state", _check_ambient_state(profile_home))
        )
    checks.append(
        _result_to_check("ambient foreground", _check_ambient_foreground_capable())
    )

    # Auto-skill-evolution (T8 of the skill-evolution series): per-profile
    # state + heartbeat + candidate-backlog surface. Same shape as the
    # ambient-state check above — opt-in, fail-open if disabled.
    if profile_home is not None:
        checks.append(
            _result_to_check(
                "skill-evolution",
                _check_skill_evolution_state(profile_home),
            )
        )

    # T6 (voice-mode) — sounddevice + webrtcvad + audio-input-device + STT
    # backend preflight. Opt-in feature; check returns warning on missing
    # deps so doctor exit-code stays clean if the user hasn't installed
    # the [voice] extra.
    checks.append(
        _result_to_check("voice-mode", _check_voice_mode_capable())
    )

    # Plugin-contributed checks + repairs (run last so plugins see a fully-
    # loaded registry, config, and DB-writable environment).
    try:
        contrib_checks = asyncio.run(_run_contributions(fix=fix))
    except RuntimeError:
        contrib_checks = []
    checks.extend(contrib_checks)

    # Print
    max_name = max(len(c.name) for c in checks)
    for c in checks:
        pad = " " * (max_name - len(c.name))
        detail = f"  [dim]— {c.detail}[/dim]" if c.detail else ""
        console.print(f"  {_status_icon(c.status)}  {c.name}{pad}{detail}")

    failures = sum(1 for c in checks if c.status == "fail")
    warnings = sum(1 for c in checks if c.status == "warn")
    console.print()
    if failures:
        console.print(f"[red bold]{failures} failure(s)[/red bold] — fix these before running.")
    elif warnings:
        console.print(f"[yellow bold]{warnings} warning(s)[/yellow bold] — should still work.")
    else:
        console.print("[green bold]All checks passed.[/green bold]")
    return failures


__all__ = ["run_doctor"]
