"""profile-scraper — orchestrator + 12 source functions.

Sources organized by category. Each ``scrape_<name>`` returns a
``list[ProfileFact]``; failures are caught at the orchestrator level
so one source failing never sinks the rest. The orchestrator writes
the resulting :class:`Snapshot` to disk under ``<profile_home>/profile_scraper/``
and trims the directory to the most recent 10 snapshots.

Defense-in-depth: each source function that touches the filesystem
double-checks ``_is_denied(path)`` before opening — we do not rely on
the orchestrator to filter its inputs. The denylist patterns are
expanded relative to ``Path.home()`` at call time so tests can shim
``Path.home`` without leaking real-user paths.

Privacy invariants:

- ``scrape_secrets_audit`` MUST return a *count*, never the matched
  token value. The :class:`ProfileFact` ``value`` field for an audit
  hit is the filename + occurrence count only.
- Browser history sources reuse :func:`opencomputer.profile_bootstrap.browser_history.read_browser_history`
  (V2.A) — page content is never collected, only URL / title /
  visit count.

This skill stays on stdlib + the existing browser-history helper.
Heavy dependencies (sentence-transformers, chromadb, …) live in the
V2.B "deepening" extras and are intentionally out of scope here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from opencomputer.agent.config import _home
from opencomputer.profile_bootstrap.browser_history import (
    read_browser_history,
)
from opencomputer.skills.profile_scraper.schema import ProfileFact, Snapshot

_log = logging.getLogger("opencomputer.skills.profile_scraper")

#: Files / dirs the scraper MUST NOT read. Patterns are evaluated
#: relative to ``Path.home()`` at call time (so tests can shim home).
#: Trailing ``/*`` indicates "any direct child"; bare paths match
#: that exact path.
_DENYLIST_GLOBS: tuple[str, ...] = (
    "~/.ssh/*",
    "~/Library/Messages/chat.db",
    "~/Library/Keychains/*",
    "~/.aws/credentials",
    "~/.config/gh/hosts.yml",
    "~/Documents/Financial/*",
)

#: Pattern used by ``scrape_secrets_audit`` — counts hits, never
#: captures the value.
_SECRET_TOKEN_RE = re.compile(r"(TOKEN|API_KEY|SECRET)", re.IGNORECASE)


def _is_denied(path: Path) -> bool:
    """Return True if ``path`` matches any denylist pattern.

    The check resolves both ``path`` and the denylist match candidates
    so symlinked accesses are caught. ``OSError`` during ``resolve`` is
    treated as denied — better to skip a path we can't classify than
    to risk reading something sensitive.
    """
    try:
        target = path.expanduser().resolve()
    except OSError:
        return True

    home = Path.home()
    for pattern in _DENYLIST_GLOBS:
        rel = pattern.removeprefix("~/")
        # Glob expansion handles the ``/*`` suffix.
        try:
            matches = list(home.glob(rel))
        except OSError:
            continue
        for match in matches:
            try:
                if target == match.resolve():
                    return True
            except OSError:
                continue
        # Also catch the directory itself for ``foo/*`` patterns when
        # the target IS the directory (not a child).
        if rel.endswith("/*"):
            parent = home / rel.removesuffix("/*")
            try:
                if target == parent.resolve():
                    return True
                # Direct-child check — a path under the parent dir is denied.
                if parent.exists() and parent.resolve() in target.parents:
                    return True
            except OSError:
                continue
    return False


# ---------------------------------------------------------------------------
# Source functions
# ---------------------------------------------------------------------------


def scrape_identity() -> list[ProfileFact]:
    """``$USER``, hostname, primary email (git config), full name (Contacts), locale."""
    facts: list[ProfileFact] = []
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if user:
        facts.append(ProfileFact("system_user", user, "env_USER"))
    try:
        facts.append(
            ProfileFact("hostname", socket.gethostname(), "socket_gethostname")
        )
    except OSError as exc:
        _log.warning("scrape_identity: hostname lookup failed: %s", exc)

    # Primary email via git config --global user.email.
    email = _run_capture(["git", "config", "--global", "user.email"])
    if email:
        facts.append(ProfileFact("primary_email", email, "git_config_global"))
    name = _run_capture(["git", "config", "--global", "user.name"])
    if name:
        facts.append(ProfileFact("git_user_name", name, "git_config_global"))

    # Full name via Contacts.app — best-effort AppleScript. This is
    # FDA-gated on macOS 14+, so failure is expected outside a real
    # interactive session.
    if _has_binary("osascript"):
        full_name = _run_capture(
            [
                "osascript",
                "-e",
                'tell application "Contacts" to get name of my card',
            ],
            timeout=3,
        )
        if full_name and full_name.lower() != "missing value":
            facts.append(
                ProfileFact("full_name", full_name, "contacts_app_my_card", 0.9)
            )

    locale = os.environ.get("LANG") or ""
    if locale:
        facts.append(ProfileFact("locale", locale, "env_LANG"))

    return facts


def scrape_projects() -> list[ProfileFact]:
    """Enumerate git repos under ``~/Vscode``, ``~/Documents/GitHub``, ``~/clean``."""
    facts: list[ProfileFact] = []
    home = Path.home()
    candidates = [home / "Vscode", home / "Documents" / "GitHub", home / "clean"]
    for root in candidates:
        if not root.exists() or _is_denied(root):
            continue
        try:
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                if _is_denied(entry):
                    continue
                if (entry / ".git").exists():
                    facts.append(
                        ProfileFact(
                            field="git_repo",
                            value=str(entry),
                            source=f"filesystem:{root.name}",
                        )
                    )
        except (OSError, PermissionError) as exc:
            _log.warning("scrape_projects: cannot iterate %s: %s", root, exc)
    return facts


def scrape_brave_history() -> list[ProfileFact]:
    """Last 30 days of Brave history — URL + title + visit_count, no content."""
    return _scrape_chromium_family("brave", "BraveSoftware/Brave-Browser")


def scrape_chrome_history() -> list[ProfileFact]:
    """Last 30 days of Chrome history — URL + title + visit_count, no content."""
    return _scrape_chromium_family("chrome", "Google/Chrome")


def _scrape_chromium_family(browser: str, vendor_path: str) -> list[ProfileFact]:
    """Shared helper for Brave / Chrome — both share the Chromium history schema.

    Walks each profile directory under the vendor root and reuses the
    V2.A :func:`read_browser_history` helper. We deliberately do NOT
    fall through to ``read_all_browser_history`` so the per-browser
    source attribution stays clean.
    """
    home = Path.home()
    root = home / "Library" / "Application Support" / Path(vendor_path)
    if not root.exists() or _is_denied(root):
        return []
    facts: list[ProfileFact] = []
    try:
        profiles = [p for p in root.iterdir() if p.is_dir()]
    except (OSError, PermissionError):
        return []
    for profile in profiles:
        history_db = profile / "History"
        if not history_db.exists() or _is_denied(history_db):
            continue
        try:
            visits = read_browser_history(
                history_db=history_db,
                browser=browser,
                days=30,
                max_visits=2000,
            )
        except Exception as exc:  # noqa: BLE001 — defensive; any IO failure
            _log.warning(
                "scrape_%s_history: read failed for %s: %s", browser, history_db, exc
            )
            continue
        for v in visits:
            facts.append(
                ProfileFact(
                    field=f"{browser}_visit",
                    value={"url": v.url, "title": v.title, "visit_time": v.visit_time},
                    source=f"{browser}_history:{profile.name}",
                    confidence=0.9,
                )
            )
    return facts


def scrape_shell_history() -> list[ProfileFact]:
    """Last 200 lines of ``~/.zsh_history``, filtered to drop secret-bearing exports."""
    home = Path.home()
    path = home / ".zsh_history"
    if not path.exists() or _is_denied(path):
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log.warning("scrape_shell_history: cannot read %s: %s", path, exc)
        return []
    lines = text.splitlines()[-200:]
    facts: list[ProfileFact] = []
    for line in lines:
        # Strip zsh extended-history prefix ``: 1700000000:0;cmd``.
        cmd = line.split(";", 1)[1] if line.startswith(": ") and ";" in line else line
        cmd = cmd.strip()
        if not cmd:
            continue
        # Defense-in-depth: drop ``export FOO=...`` lines that mention
        # SECRET / TOKEN / KEY in the variable name OR value.
        if cmd.startswith("export ") and _SECRET_TOKEN_RE.search(cmd):
            continue
        facts.append(
            ProfileFact(
                field="shell_command",
                value=cmd[:512],
                source="zsh_history",
                confidence=0.8,
            )
        )
    return facts


def scrape_git_activity() -> list[ProfileFact]:
    """``gh repo list`` + ``gh starred`` — best-effort; empty if gh missing."""
    if not _has_binary("gh"):
        return []
    facts: list[ProfileFact] = []
    repos = _run_capture(["gh", "repo", "list", "--limit", "30"], timeout=10)
    if repos:
        for line in repos.splitlines():
            slug = line.split("\t", 1)[0].strip()
            if slug:
                facts.append(
                    ProfileFact(
                        field="github_repo",
                        value=slug,
                        source="gh_repo_list",
                        confidence=0.95,
                    )
                )
    starred = _run_capture(
        ["gh", "api", "user/starred?per_page=30"], timeout=10
    )
    if starred:
        try:
            data = json.loads(starred)
        except json.JSONDecodeError:
            data = []
        for item in data if isinstance(data, list) else []:
            slug = item.get("full_name") if isinstance(item, dict) else None
            if slug:
                facts.append(
                    ProfileFact(
                        field="github_starred",
                        value=slug,
                        source="gh_user_starred",
                        confidence=0.85,
                    )
                )
    return facts


def scrape_recent_files() -> list[ProfileFact]:
    """Spotlight: files modified in the last 7 days, capped at 50."""
    if not _has_binary("mdfind"):
        return []
    raw = _run_capture(
        [
            "mdfind",
            "kMDItemContentModificationDate > $time.now(-7d)",
        ],
        timeout=10,
    )
    if not raw:
        return []
    facts: list[ProfileFact] = []
    for path_str in raw.splitlines()[:50]:
        path = Path(path_str)
        if _is_denied(path):
            continue
        facts.append(
            ProfileFact(
                field="recent_file",
                value=str(path),
                source="spotlight_mdfind",
                confidence=0.85,
            )
        )
    return facts


def scrape_app_inventory() -> list[ProfileFact]:
    """Names of apps in ``/Applications`` — no metadata, just bundle names."""
    apps_dir = Path("/Applications")
    if not apps_dir.exists():
        return []
    facts: list[ProfileFact] = []
    try:
        for entry in apps_dir.iterdir():
            name = entry.name
            if name.endswith(".app"):
                facts.append(
                    ProfileFact(
                        field="installed_app",
                        value=name.removesuffix(".app"),
                        source="ls_Applications",
                    )
                )
    except (OSError, PermissionError) as exc:
        _log.warning("scrape_app_inventory: cannot list /Applications: %s", exc)
    return facts


def scrape_system_info() -> list[ProfileFact]:
    """Locale (``locale`` cmd), timezone, hardware (``system_profiler``)."""
    facts: list[ProfileFact] = []
    if _has_binary("locale"):
        loc = _run_capture(["locale"], timeout=3)
        if loc:
            for line in loc.splitlines():
                if line.startswith("LANG="):
                    facts.append(
                        ProfileFact(
                            field="lang",
                            value=line.split("=", 1)[1].strip('"'),
                            source="locale_cmd",
                        )
                    )
                    break
    # Timezone — best-effort via /etc/localtime symlink readlink.
    tz_link = Path("/etc/localtime")
    try:
        if tz_link.is_symlink():
            target = os.readlink(tz_link)
            facts.append(
                ProfileFact(
                    field="timezone", value=str(target), source="etc_localtime_symlink"
                )
            )
    except OSError:
        pass
    # Hardware — system_profiler is macOS-only; degrade gracefully.
    if _has_binary("system_profiler"):
        hw = _run_capture(
            ["system_profiler", "SPHardwareDataType"], timeout=15
        )
        if hw:
            for line in hw.splitlines():
                stripped = line.strip()
                if stripped.startswith("Model Name:"):
                    facts.append(
                        ProfileFact(
                            field="hardware_model",
                            value=stripped.split(":", 1)[1].strip(),
                            source="system_profiler",
                        )
                    )
                elif stripped.startswith("Chip:"):
                    facts.append(
                        ProfileFact(
                            field="hardware_chip",
                            value=stripped.split(":", 1)[1].strip(),
                            source="system_profiler",
                        )
                    )
                elif stripped.startswith("Memory:"):
                    facts.append(
                        ProfileFact(
                            field="hardware_memory",
                            value=stripped.split(":", 1)[1].strip(),
                            source="system_profiler",
                        )
                    )
    return facts


def scrape_secrets_audit() -> list[ProfileFact]:
    """Count of ``TOKEN|API_KEY|SECRET`` matches in ``~/.zshrc`` + ``~/.zsh_history``.

    Returns the *count*, never the matched value. ``ProfileFact.value``
    is a structured ``{"file": ..., "count": N}`` so consumers see how
    many hits were found and where, but never the secret itself.
    """
    home = Path.home()
    targets = [home / ".zshrc", home / ".zsh_history"]
    facts: list[ProfileFact] = []
    for target in targets:
        if not target.exists() or _is_denied(target):
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _log.warning("scrape_secrets_audit: cannot read %s: %s", target, exc)
            continue
        count = sum(1 for _ in _SECRET_TOKEN_RE.finditer(text))
        # NOTE: We deliberately store filename + count only — never the
        # matched substring or surrounding line content. The whole point
        # of this audit is to surface *that* secrets exist, not *what*
        # they are.
        facts.append(
            ProfileFact(
                field="secret_token_hits",
                value={"file": str(target), "count": count},
                source="secrets_audit_grep",
                confidence=0.9,
            )
        )
    return facts


def scrape_git_email_history() -> list[ProfileFact]:
    """Top 10 distinct commit-author emails across detected project repos."""
    if not _has_binary("git"):
        return []
    home = Path.home()
    candidates = [home / "Vscode", home / "Documents" / "GitHub", home / "clean"]
    seen: set[str] = set()
    facts: list[ProfileFact] = []
    for root in candidates:
        if not root.exists() or _is_denied(root):
            continue
        try:
            entries = [
                p
                for p in root.iterdir()
                if p.is_dir() and (p / ".git").exists() and not _is_denied(p)
            ]
        except (OSError, PermissionError):
            continue
        for repo in entries:
            out = _run_capture(
                [
                    "git",
                    "-C",
                    str(repo),
                    "log",
                    "--all",
                    "--format=%ae",
                ],
                timeout=10,
            )
            if not out:
                continue
            for email in out.splitlines():
                email = email.strip()
                if not email or email in seen:
                    continue
                seen.add(email)
                facts.append(
                    ProfileFact(
                        field="git_author_email",
                        value=email,
                        source=f"git_log:{repo.name}",
                        confidence=0.85,
                    )
                )
                if len(seen) >= 10:
                    return facts
    return facts


def scrape_pkg_managers() -> list[ProfileFact]:
    """``brew list --formulae`` (top 50) + ``pip list`` top-level packages."""
    facts: list[ProfileFact] = []
    if _has_binary("brew"):
        out = _run_capture(["brew", "list", "--formulae"], timeout=10)
        if out:
            for name in out.splitlines()[:50]:
                name = name.strip()
                if name:
                    facts.append(
                        ProfileFact(
                            field="brew_formula", value=name, source="brew_list"
                        )
                    )
    if _has_binary("pip"):
        out = _run_capture(["pip", "list", "--format=freeze"], timeout=15)
        if out:
            for line in out.splitlines()[:50]:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                pkg = line.split("==", 1)[0]
                if pkg:
                    facts.append(
                        ProfileFact(
                            field="pip_package", value=pkg, source="pip_list"
                        )
                    )
    return facts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_binary(name: str) -> bool:
    """``shutil.which``-with-PATH; testable swap point."""
    return shutil.which(name) is not None


def _run_capture(
    cmd: list[str], *, timeout: float = 5.0
) -> str:
    """Run ``cmd``, return stripped stdout or ``""`` on any failure.

    Errors are logged at ``DEBUG`` (most are expected: missing binary,
    no git config, FDA-gated osascript). The caller should treat empty
    output as "source not available."
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, never shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("_run_capture %s failed: %s", cmd[0], exc)
        return ""
    if proc.returncode != 0:
        _log.debug(
            "_run_capture %s exited %d: %s",
            cmd[0],
            proc.returncode,
            (proc.stderr or "").strip()[:200],
        )
        return ""
    return (proc.stdout or "").strip()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


_SCRAPER_REGISTRY: tuple[tuple[str, Callable[[], list[ProfileFact]]], ...] = (
    ("identity", scrape_identity),
    ("projects", scrape_projects),
    ("brave_history", scrape_brave_history),
    ("chrome_history", scrape_chrome_history),
    ("shell_history", scrape_shell_history),
    ("git_activity", scrape_git_activity),
    ("recent_files", scrape_recent_files),
    ("app_inventory", scrape_app_inventory),
    ("system_info", scrape_system_info),
    ("secrets_audit", scrape_secrets_audit),
    ("git_email_history", scrape_git_email_history),
    ("pkg_managers", scrape_pkg_managers),
)


def run_scrape(*, full: bool = False) -> Snapshot:
    """Run every registered scraper, persist a snapshot, return it.

    The ``full`` parameter is reserved for V3.B's incremental-diff
    pathway. In the MVP we always run a full scrape — ``full`` is
    accepted but ignored so callers can wire the CLI flag now and get
    the right behaviour later without an API change.
    """
    del full  # reserved for V3.B; MVP always does a full scrape.
    started = time.time()
    facts: list[ProfileFact] = []
    attempted: list[str] = []
    succeeded: list[str] = []

    for name, fn in _SCRAPER_REGISTRY:
        attempted.append(name)
        try:
            facts.extend(fn())
            succeeded.append(name)
        except Exception as exc:  # noqa: BLE001 — defensive top-level barrier
            _log.warning("scrape_%s failed: %s", name, exc)

    ended = time.time()
    snapshot = Snapshot(
        facts=tuple(facts),
        started_at=started,
        ended_at=ended,
        sources_attempted=tuple(attempted),
        sources_succeeded=tuple(succeeded),
    )

    _write_snapshot(snapshot)
    return snapshot


def _write_snapshot(snap: Snapshot) -> Path:
    """Persist ``snap`` as JSON, update ``latest.json``, GC to most recent 10."""
    out_dir = _home() / "profile_scraper"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(snap.ended_at)
    path = out_dir / f"snapshot_{ts}.json"
    payload = {
        "started_at": snap.started_at,
        "ended_at": snap.ended_at,
        "sources_attempted": list(snap.sources_attempted),
        "sources_succeeded": list(snap.sources_succeeded),
        "facts": [
            {
                "field": f.field,
                "value": f.value,
                "source": f.source,
                "confidence": f.confidence,
                "timestamp": f.timestamp,
            }
            for f in snap.facts
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    # Latest pointer — literal copy so consumers can read it without
    # following a symlink (works on every filesystem we care about).
    (out_dir / "latest.json").write_text(path.read_text())
    # Trim — keep only the 10 most recent snapshots.
    snapshots = sorted(out_dir.glob("snapshot_*.json"))
    for old in snapshots[:-10]:
        try:
            old.unlink()
        except OSError as exc:
            _log.warning("_write_snapshot: cannot unlink %s: %s", old, exc)
    return path
