"""@-reference parser + expanders.

Grammar:
    @file:<path>                 → inject file body
    @file:<path>:<a>-<b>         → inject lines a..b (1-indexed inclusive)
    @folder:<path>               → tree listing (≤ 200 entries)
    @diff                        → git diff (unstaged)
    @staged                      → git diff --staged
    @git:<N>                     → last N commits with patches (clamp ≤ 10)
    @url:<https://...>           → fetch + inject web page text

Multiple refs in one message: all that fit are expanded; refs over the
combined hard cap are refused with an inline marker. The CLI input
loop calls ``expand(text, ctx=...)`` AFTER slash dispatch and BEFORE
message construction. Channel adapters do NOT call ``expand``
(CLI-only) — the ``@`` syntax is plausibly meaningful in chat content
on Telegram / Discord, so silent expansion there would surprise users.

Blocked-paths policy refuses files in ``~/.ssh/``, ``~/.aws/``,
``~/.gnupg/``, ``~/.kube/``, plus shell profiles (``~/.bashrc``,
``~/.zshrc``, etc.) and credential glob patterns (``*.pem``, ``*.key``,
``id_rsa*``). Single helper :func:`is_path_blocked` so the policy
lives in one place.

For ``@url:``, reuses :func:`opencomputer.agent.link_understanding.is_safe_url`
as the SSRF guard (refuses private / link-local / loopback / cloud
metadata addresses; see Tier-S PR #171).
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

logger = logging.getLogger("opencomputer.agent.at_references")

AtRefKind = Literal["file", "folder", "diff", "staged", "git", "url"]


@dataclass(frozen=True, slots=True)
class AtRef:
    kind: AtRefKind
    arg: str
    line_start: int | None
    line_end: int | None


# ─── parser ────────────────────────────────────────────────────────

_KIND_PATTERN = (
    r"(?:^|(?<=\s))"
    r"@(file|folder|diff|staged|git|url)"
    r"(?::([^\s]+))?"
)
_RE = re.compile(_KIND_PATTERN)
_TRAILING_PUNCT = ",.;:!?)"


def parse(text: str) -> list[AtRef]:
    """Parse ``text`` for @-references. Returns them in left-to-right order."""
    out: list[AtRef] = []
    for m in _RE.finditer(text):
        kind = m.group(1)
        arg = (m.group(2) or "").rstrip(_TRAILING_PUNCT)

        if kind in ("diff", "staged"):
            out.append(AtRef(kind=kind, arg="", line_start=None, line_end=None))
            continue

        if kind == "git":
            if not arg:
                continue
            out.append(AtRef(kind="git", arg=arg, line_start=None, line_end=None))
            continue

        if kind == "url":
            if not arg:
                continue
            out.append(AtRef(kind="url", arg=arg, line_start=None, line_end=None))
            continue

        if not arg:
            continue

        if kind == "file":
            range_match = re.search(r":(\d+)-(\d+)$", arg)
            if range_match:
                path = arg[: range_match.start()]
                a = int(range_match.group(1))
                b = int(range_match.group(2))
                out.append(
                    AtRef(kind="file", arg=path, line_start=a, line_end=b)
                )
            else:
                out.append(
                    AtRef(kind="file", arg=arg, line_start=None, line_end=None)
                )
        else:  # folder
            out.append(
                AtRef(kind="folder", arg=arg, line_start=None, line_end=None)
            )

    return out


# ─── context + caps ───────────────────────────────────────────────

_FOLDER_MAX_ENTRIES = 200
_GIT_MAX_COMMITS = 10
_SOFT_CAP_FRAC = 0.25
_HARD_CAP_FRAC = 0.50
_URL_TIMEOUT_S = 5.0
_URL_BODY_CAP = 50_000  # bytes after HTML strip


@dataclass(frozen=True, slots=True)
class AtRefContext:
    cwd: str
    home: str
    context_window_chars: int = 200_000

    @property
    def soft_cap(self) -> int:
        return int(self.context_window_chars * _SOFT_CAP_FRAC)

    @property
    def hard_cap(self) -> int:
        return int(self.context_window_chars * _HARD_CAP_FRAC)


# ─── blocked paths ────────────────────────────────────────────────

_BLOCKED_DIRS = (".ssh", ".aws", ".gnupg", ".kube")
# Shell profile basenames where users commonly source secrets
# (``export ANTHROPIC_API_KEY=…``). Hermes v2 parity follow-up: added
# ``.zprofile`` (was missed in PR #510) plus the broader zsh/bash login
# profile set so all common shell-credential sources are covered.
_BLOCKED_FILE_BASENAMES = frozenset({
    ".netrc", ".pgpass",
    ".bashrc", ".bash_profile", ".bash_login", ".profile",
    ".zshrc", ".zprofile", ".zlogin", ".zshenv",
})
_BLOCKED_FILE_GLOBS = ("*.pem", "*.key", "id_rsa*", "id_ed25519*", "id_dsa*")
# Common binary file extensions — short-circuit before the more
# expensive null-byte sniff in :func:`_looks_binary`. These are the
# extensions a casual ``@file:`` user would most often hit by mistake
# (image / archive / executable) without intending a binary dump.
_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".class",
    ".pyc", ".pyo", ".whl",
    ".mp3", ".mp4", ".wav", ".ogg", ".flac", ".avi", ".mov", ".mkv",
    ".sqlite", ".db", ".sqlite3",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
})
# Hermes v2 spec D2: "Known text extensions (.py, .md, .json, .yaml,
# etc.) bypass MIME-based detection." A .md file with a literal NUL
# (rare but legitimate — diagram files, escaped text) shouldn't be
# wrongly flagged as binary. The extension allowlist short-circuits the
# null-byte sniff for common source/config formats.
_TEXT_EXTENSIONS = frozenset({
    # Source code
    ".py", ".pyi", ".pyx", ".pxd",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".rs", ".go", ".rb", ".java", ".kt", ".kts", ".scala", ".clj", ".cljs",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hxx",
    ".swift", ".m", ".mm", ".pl", ".pm", ".lua", ".php", ".r", ".jl",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".sql", ".graphql", ".gql", ".proto",
    # Config / data
    ".json", ".json5", ".jsonc", ".jsonl", ".ndjson",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".xml", ".html", ".htm", ".xhtml", ".svg", ".css", ".scss", ".sass",
    ".csv", ".tsv",
    # Docs
    ".md", ".markdown", ".rst", ".adoc", ".txt", ".text",
    # Build / lockfiles
    ".lock", ".dockerfile", ".gitignore", ".gitattributes",
    ".editorconfig", ".prettierrc", ".eslintrc",
    # Misc text-y formats
    ".log", ".diff", ".patch",
})


def is_path_blocked(path: Path, *, home: Path) -> bool:
    """True if ``path`` is on the deny list.

    Refuses anything in a top-level blocked dir under home (``~/.ssh``,
    etc.), plus shell profile basenames anywhere, plus key/cert glob
    patterns.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return True

    try:
        rel = resolved.relative_to(home.resolve())
        head = rel.parts[0] if rel.parts else ""
        if head in _BLOCKED_DIRS:
            return True
    except ValueError:
        pass

    name = resolved.name
    if name in _BLOCKED_FILE_BASENAMES:
        return True
    return any(fnmatch(name, g) for g in _BLOCKED_FILE_GLOBS)


def _is_outside_workspace(path: Path, *, workspace_root: Path) -> bool:
    """True if ``path`` resolves outside ``workspace_root``.

    Hermes v2 spec: "References outside allowed workspace root rejected."
    Symlinks are resolved before the comparison so a symlink pointing
    out cannot be used to bypass.

    Failures (resolve error etc.) return ``True`` — better to refuse
    a path we cannot reason about than to let it through.
    """
    try:
        resolved = path.resolve()
        root = workspace_root.resolve()
    except OSError:
        return True
    try:
        resolved.relative_to(root)
        return False
    except ValueError:
        return True


def _looks_binary(path: Path) -> bool:
    """Best-effort binary-file detection (Hermes v2 parity).

    Three signals applied in order:

    1. **Text-extension allowlist** — known source / config / docs
       extensions (``.py``, ``.md``, ``.json``, ``.yaml`` etc.) bypass
       further checks. Matches Hermes v2 spec verbatim: text extensions
       skip the null-byte sniff.
    2. **Binary-extension blocklist** — known binary formats
       (``.png``, ``.zip``, etc.) short-circuit to True without I/O.
    3. **Null-byte sniff** — first 8KB of the file. Fallback for
       extensions we don't recognize.

    Returns ``False`` on any read error — the caller's ``read_text``
    path will surface the underlying issue with a more specific message.
    """
    suffix = path.suffix.lower()
    if suffix in _TEXT_EXTENSIONS:
        return False  # explicit text bypass, no I/O
    if suffix in _BINARY_EXTENSIONS:
        return True
    try:
        with path.open("rb") as fh:
            chunk = fh.read(8192)
    except OSError:
        return False
    return b"\x00" in chunk


# ─── expanders ────────────────────────────────────────────────────

def _expand_file(ref: AtRef, ctx: AtRefContext) -> str:
    p = Path(ref.arg).expanduser()
    if not p.is_absolute():
        p = Path(ctx.cwd) / ref.arg
    p = p.expanduser()

    if not p.exists():
        return f"[file not found: {ref.arg}]"
    if not p.is_file():
        return f"[not a file: {ref.arg}]"
    if is_path_blocked(p, home=Path(ctx.home)):
        return f"[blocked path: {ref.arg}]"
    # Hermes v2 spec: references outside the workspace root are
    # refused. Resolves symlinks first so a symlink-bypass cannot leak
    # secrets via a path that "looks" inside the workspace.
    if _is_outside_workspace(p, workspace_root=Path(ctx.cwd)):
        return f"[blocked: {ref.arg} resolves outside workspace]"
    # Hermes v2 spec: binary files are not supported.
    if _looks_binary(p):
        return f"[binary file not supported: {ref.arg}]"

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[read failed: {ref.arg} — {exc}]"

    if ref.line_start is not None and ref.line_end is not None:
        lines = text.splitlines(keepends=True)
        a = max(1, ref.line_start)
        b = min(len(lines), ref.line_end)
        text = "".join(lines[a - 1 : b])

    if len(text) > ctx.hard_cap:
        return (
            f"[ref refused: {ref.arg} is {len(text)} chars "
            f"(hard cap {ctx.hard_cap})]"
        )

    notice = ""
    if len(text) > ctx.soft_cap:
        notice = (
            f"\n[note: {ref.arg} is {len(text)} chars — "
            f"exceeds soft cap {ctx.soft_cap}]"
        )

    label = f"@file:{ref.arg}"
    if ref.line_start is not None:
        label += f":{ref.line_start}-{ref.line_end}"

    return f"### {label}\n```\n{text}\n```{notice}"


def _expand_folder(ref: AtRef, ctx: AtRefContext) -> str:
    p = Path(ref.arg).expanduser()
    if not p.is_absolute():
        p = Path(ctx.cwd) / ref.arg
    p = p.expanduser()

    if not p.exists():
        return f"[folder not found: {ref.arg}]"
    if not p.is_dir():
        return f"[not a folder: {ref.arg}]"

    try:
        children = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))
    except OSError as exc:
        return f"[folder read failed: {ref.arg} — {exc}]"

    entries: list[str] = []
    truncated = False
    for child in children:
        if len(entries) >= _FOLDER_MAX_ENTRIES:
            truncated = True
            break
        try:
            size = child.stat().st_size if child.is_file() else 0
        except OSError:
            size = 0
        marker = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{marker}\t{size} bytes")

    body = "\n".join(entries)
    trailer = ""
    if truncated:
        trailer = (
            f"\n[truncated: showing first {_FOLDER_MAX_ENTRIES} of "
            f"{len(children)} entries]"
        )

    return f"### @folder:{ref.arg}\n```\n{body}{trailer}\n```"


def _git(ctx: AtRefContext, *args: str) -> tuple[bool, str]:
    """Run a git subprocess in ctx.cwd. Returns (ok, output_or_error).

    Pre-checks that ctx.cwd is inside a git work-tree via
    ``git rev-parse --git-dir``; this gives all git-based refs a
    uniform "not a git repository" error message regardless of which
    sub-command was attempted (git's behavior outside a repo varies —
    some commands fall into ``--no-index`` mode and produce confusing
    errors).
    """
    if not shutil.which("git"):
        return False, "[git not on PATH]"

    try:
        precheck = subprocess.run(  # noqa: S603 — literal args
            ["git", "rev-parse", "--git-dir"],
            cwd=ctx.cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return False, "[git timed out]"

    if precheck.returncode != 0:
        return False, "[not a git repository]"

    try:
        proc = subprocess.run(  # noqa: S603 — args are literals, not user input
            ["git", *args],
            cwd=ctx.cwd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, "[git timed out]"

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().lower()
        if "not a git repository" in msg:
            return False, "[not a git repository]"
        return False, f"[git failed: {msg[:200]}]"

    return True, proc.stdout


def _expand_diff(_ref: AtRef, ctx: AtRefContext) -> str:
    ok, out = _git(ctx, "diff")
    if not ok:
        return out
    return f"### @diff\n```diff\n{out}\n```"


def _expand_staged(_ref: AtRef, ctx: AtRefContext) -> str:
    ok, out = _git(ctx, "diff", "--staged")
    if not ok:
        return out
    return f"### @staged\n```diff\n{out}\n```"


def _expand_git(ref: AtRef, ctx: AtRefContext) -> str:
    try:
        n = int(ref.arg)
    except ValueError:
        return f"[bad @git argument: {ref.arg!r}]"

    clamped = min(max(n, 1), _GIT_MAX_COMMITS)
    ok, out = _git(ctx, "log", "-p", "-n", str(clamped))
    if not ok:
        return out

    notice = ""
    if n != clamped:
        notice = f"\n[clamped from {n} to {clamped} commits]"

    return f"### @git:{clamped}\n```\n{out}\n```{notice}"


def _expand_url(ref: AtRef, _ctx: AtRefContext) -> str:
    from opencomputer.agent.link_understanding import is_safe_url

    if not is_safe_url(ref.arg):
        return f"[blocked: {ref.arg} failed SSRF guard]"

    try:
        import httpx
    except ImportError:
        return "[fetch unavailable: httpx not installed]"

    try:
        with httpx.Client(timeout=_URL_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(ref.arg)
            if resp.status_code >= 400:
                return f"[fetch failed: {resp.status_code} for {ref.arg}]"
            text = resp.text
    except httpx.TimeoutException:
        return f"[fetch timed out: {ref.arg}]"
    except httpx.HTTPError as exc:
        return f"[fetch failed: {ref.arg} — {exc}]"

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return f"### @url:{ref.arg}\n```\n{text[:_URL_BODY_CAP]}\n```"


_DISPATCH = {
    "file": _expand_file,
    "folder": _expand_folder,
    "diff": _expand_diff,
    "staged": _expand_staged,
    "git": _expand_git,
    "url": _expand_url,
}


def expand(text: str, *, ctx: AtRefContext) -> str:
    """Parse ``text`` for refs and append expansions under a header.

    Original text is preserved verbatim. Expansions are appended after a
    ``--- Attached Context ---`` separator. Returns ``text`` unchanged
    when no refs are found.
    """
    refs = parse(text)
    if not refs:
        return text

    blocks: list[str] = []
    total = 0
    for ref in refs:
        try:
            block = _DISPATCH[ref.kind](ref, ctx)
        except Exception as exc:  # noqa: BLE001 — never crash send-path
            block = f"[expander error: @{ref.kind}:{ref.arg} — {exc}]"
            logger.exception(
                "at_references: expander crashed for %r", ref
            )

        if total + len(block) > ctx.hard_cap and len(blocks) > 0:
            blocks.append(
                f"[ref refused: combined expansion exceeded hard cap "
                f"after {len(blocks)} ref(s)]"
            )
            break

        blocks.append(block)
        total += len(block)

    body = "\n\n".join(blocks)
    return f"{text}\n\n--- Attached Context ---\n\n{body}"


__all__ = [
    "AtRef",
    "AtRefContext",
    "AtRefKind",
    "expand",
    "is_path_blocked",
    "parse",
]
