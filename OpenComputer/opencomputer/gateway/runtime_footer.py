"""Per-platform runtime-metadata footer (Wave 5 T4 + PR-2 T2.2/T2.4).

Hermes-port from ``e123f4ecf``. Optional one-line footer appended to the
last assistant message of each turn — surfacing model name, context-window
fill percentage, and the agent's working directory. Default disabled so
existing deployments see no change; opt-in via
``display.runtime_footer.enabled = true`` (with optional per-platform
overrides under ``display.platforms.<name>.runtime_footer.enabled``).

PR-2 (T2.2) — extended to honor a configurable ``fields`` list so users
can pick which metadata appears in the footer.

PR-2 (T2.4) — adds the first-time busy-input-tip onboarding latch at
``<profile>/onboarding.json``. The first time a user triggers a
busy-ack, the tip ``💡 First-time tip — set display.busy_input_mode
(queue|steer|interrupt)`` is appended; subsequent acks skip it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("opencomputer.gateway.runtime_footer")

# ── FooterConfig ──────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class FooterConfig:
    """Resolved per-platform footer enablement + selected fields."""

    enabled: bool = False
    fields: tuple[str, ...] = ("model", "context_pct", "cwd")


_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")


def resolve_footer_config(
    cfg: dict,
    *,
    platform: str | None = None,
) -> FooterConfig:
    """Resolve effective footer enablement + fields for ``platform``.

    Resolution order (later wins):
        1. Built-in defaults (enabled=False, fields=DEFAULT_FIELDS)
        2. ``display.runtime_footer`` (global)
        3. ``display.platforms.<platform>.runtime_footer`` (per-platform)
    """
    display = cfg.get("display") or {}
    enabled = False
    fields: tuple[str, ...] = _DEFAULT_FIELDS

    base = display.get("runtime_footer") or {}
    if isinstance(base, dict):
        if "enabled" in base:
            enabled = bool(base.get("enabled"))
        if isinstance(base.get("fields"), list) and base["fields"]:
            fields = tuple(str(f) for f in base["fields"])

    if platform:
        plat = (display.get("platforms") or {}).get(platform) or {}
        plat_footer = plat.get("runtime_footer") or {}
        if isinstance(plat_footer, dict):
            if "enabled" in plat_footer:
                enabled = bool(plat_footer["enabled"])
            if isinstance(plat_footer.get("fields"), list) and plat_footer["fields"]:
                fields = tuple(str(f) for f in plat_footer["fields"])

    return FooterConfig(enabled=enabled, fields=fields)


def format_runtime_footer(
    *,
    model: str,
    tokens_used: int,
    context_length: int | None,
    cwd: str,
    fields: tuple[str, ...] | list[str] = _DEFAULT_FIELDS,
) -> str:
    """Render a single ``model · pct% · ~/cwd`` line per ``fields`` order.

    Recognised field names: ``model``, ``context_pct``, ``cwd``. Unknown
    fields are silently dropped (logged at debug). Empty-output policy:
    when no field has data, return ``""`` so callers don't accidentally
    append a stray glyph.
    """
    parts: list[str] = []
    seen: set[str] = set()
    for fname in fields:
        if fname in seen:
            continue
        seen.add(fname)
        if fname == "model" and model:
            parts.append(model)
        elif fname == "context_pct" and context_length and tokens_used >= 0:
            pct = round(100.0 * tokens_used / context_length)
            parts.append(f"{pct}%")
        elif fname == "cwd" and cwd:
            parts.append(_shorten_cwd(cwd))
        else:
            logger.debug(
                "runtime_footer: unknown or empty field %r — dropping",
                fname,
            )
    return " · ".join(parts) if parts else ""


def append_or_send_trailing(
    reply_text: str,
    footer: str,
    *,
    streaming: bool,
) -> tuple[str, str | None]:
    """Decide how to deliver the footer alongside ``reply_text``.

    Non-streaming: append ``\\n<footer>`` to the reply, return ``(combined, None)``.
    Streaming: return ``(reply_text, footer)`` so the caller can ``adapter.send``
    the footer as a separate trailing message after the streamed body landed.
    """
    if not footer:
        return (reply_text, None)
    if streaming:
        return (reply_text, footer)
    return (reply_text + "\n" + footer, None)


def _shorten_cwd(cwd: str) -> str:
    """Replace the user's home prefix with ``~`` for a compact path."""
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


# ── Busy ack + first-time tip ──────────────────────────────────────────────


def should_send_busy_ack(cfg: dict) -> bool:
    """``display.busy_ack_enabled`` knob (default True).

    Default-on preserves the historical UX where the gateway tells the
    user "got it, working on it" after long replies. Set to False to
    suppress the explicit ack and rely on typing indicators alone.
    """
    return bool((cfg.get("display") or {}).get("busy_ack_enabled", True))


def busy_ack_text(
    cfg: dict,
    *,
    profile_home: Path | None = None,
    base: str = "⚡ working — your message is queued",
) -> str:
    """Compose the busy-ack reply text for the current chat.

    First-time-only behaviour: when ``profile_home`` is provided AND the
    onboarding latch ``<profile>/onboarding.json`` has not yet recorded
    ``busy_input_prompt`` as seen, append a one-line tip about the
    interrupt/queue/steer modes. Subsequent calls skip the tip.

    The latch is flock-protected on POSIX and uses a tmpfile-rename
    "first writer wins" fallback on Windows. A two-process race during
    install may emit the tip twice — acceptable; the latch latches on
    first successful write.
    """
    if profile_home is None:
        return base
    latch = _OnboardingLatch(Path(profile_home) / "onboarding.json")
    if latch.seen("busy_input_prompt"):
        return base
    latch.mark_seen("busy_input_prompt")
    return (
        base
        + "\n💡 First-time tip — set display.busy_input_mode "
        "(queue|steer|interrupt) to control how the gateway handles "
        "rapid-fire messages."
    )


class _OnboardingLatch:
    """File-backed once-only latch for first-time chat tips.

    Schema: ``{"seen": {"<key>": true, ...}}``. Each unique key gets one
    emit — `mark_seen("foo")` is idempotent thereafter.
    """

    def __init__(self, path: Path):
        self._path = Path(path)

    def seen(self, key: str) -> bool:
        try:
            data = self._load()
            return bool((data.get("seen") or {}).get(key))
        except OSError:
            return False

    def mark_seen(self, key: str) -> None:
        try:
            with self._lock():
                data = self._load()
                seen = dict(data.get("seen") or {})
                if seen.get(key):
                    return
                seen[key] = True
                data["seen"] = seen
                self._save(data)
        except OSError as exc:
            logger.warning(
                "_OnboardingLatch: mark_seen %r failed: %s — tip may re-emit",
                key,
                exc,
            )

    # ── Internals ──

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            # Treat corrupt latch as "fresh state" — first-time tip will
            # emit again, which is benign (a one-line message). Better
            # than crashing dispatch.
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @contextmanager
    def _lock(self):
        """Cross-process advisory lock around the save path.

        POSIX: ``fcntl.flock``. Windows: best-effort O_EXCL on a sibling
        ``.lock`` file (acceptable race envelope: one extra tip emit max).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            lock_path = self._path.with_suffix(self._path.suffix + ".lock")
            for _ in range(50):
                try:
                    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    break
                except FileExistsError:
                    try:
                        if time.time() - lock_path.stat().st_mtime > 60:
                            lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    time.sleep(0.05)
            else:
                # Force-acquire — drop stale lock.
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
            try:
                yield
            finally:
                try:
                    os.close(fd)
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            import fcntl

            fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


__all__ = [
    "FooterConfig",
    "_OnboardingLatch",
    "append_or_send_trailing",
    "busy_ack_text",
    "format_runtime_footer",
    "resolve_footer_config",
    "should_send_busy_ack",
]
