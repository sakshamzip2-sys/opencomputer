"""SecretRef provider chain — env / exec resolvers + eager registry.

Sits on top of :class:`plugin_sdk.wire_primitives.SecretRef` (the
wire-safe reference shape, with no resolution logic of its own) and
adds the missing piece: how a configured reference becomes a real
credential at startup, with the safety guarantees the OpenClaw
reference design calls for.

Three pieces:

* :class:`SecretProvider` — ABC. Subclasses implement
  :meth:`resolve` (string in, string out, raises
  :class:`SecretProviderError` on failure).
* :class:`EnvSecretProvider` — looks up an env var.
* :class:`ExecSecretProvider` — runs a *validated* binary (no shell,
  configurable timeout, output-byte cap). Suitable for 1Password
  ``op``, HashiCorp ``vault``, ``sops``.
* :class:`SecretRegistry` — eager-resolves a list of
  :class:`SecretSpec` declarations at startup. Atomic swap on
  reload: a partial failure keeps the last-known-good map intact.

Plus :func:`audit_paths` — a static analyzer that walks config files
looking for plaintext credentials and surfacing
``$secret_ref``-shaped values, used by ``oc secrets audit``.

Why live here, not in ``plugin_sdk/``: ``plugin_sdk`` is the wire
contract — adding subprocess, environ, and filesystem work to it
would break the "no imports from opencomputer / minimal surface"
invariant. The wire primitive stays in ``plugin_sdk``; this module
is its in-process companion.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from plugin_sdk.wire_primitives import SecretRef

__all__ = [
    "AuditFinding",
    "EnvSecretProvider",
    "ExecSecretProvider",
    "FileSecretProvider",
    "SecretProvider",
    "SecretProviderError",
    "SecretRegistry",
    "SecretSpec",
    "apply_secrets_to_environ",
    "audit_paths",
    "load_secrets_at_startup",
]


_log = logging.getLogger("opencomputer.security.secrets")

DEFAULT_TIMEOUT_S: float = 5.0
DEFAULT_MAX_OUTPUT_BYTES: int = 64 * 1024  # 64 KiB — secret values are short


class SecretProviderError(RuntimeError):
    """Resolution failed — missing var, exec error, timeout, etc.

    Subclassing :class:`RuntimeError` mirrors how
    :class:`opencomputer.agent.loop_safety.LoopAbortError` is shaped:
    callers that want a generic "any provider failure" handler can
    catch ``RuntimeError``; specific callers can catch this class.
    """


# ─── providers ────────────────────────────────────────────────────────


class SecretProvider(ABC):
    """Abstract resolver. Implementations are responsible for I/O safety."""

    @abstractmethod
    def resolve(self, key: str) -> str:
        """Return the secret value for *key* or raise :class:`SecretProviderError`."""


class EnvSecretProvider(SecretProvider):
    """Resolves a secret reference against ``os.environ``.

    Empty-string values count as unset — a sentinel set to ``""`` is no
    better than a missing var for credential purposes and would silently
    pass to downstream callers as a working secret otherwise.
    """

    def resolve(self, key: str) -> str:
        value = os.environ.get(key)
        if not value:
            raise SecretProviderError(
                f"env var {key!r} is unset or blank"
            )
        return value


class ExecSecretProvider(SecretProvider):
    """Resolves a secret by invoking an external CLI.

    Args:
        command: absolute path to the resolver binary.
        args_template: argv tail passed after *command*. Each element
            may contain ``{id}`` which is substituted with the secret
            id at resolve time. Substitution is positional only — no
            shell evaluation, no glob expansion.
        timeout_s: hard wall-clock cap; exceeded → kill + raise.
        max_output_bytes: stdout cap; exceeded → kill + raise. Real
            credentials never approach this; a bad resolver dumping
            its own help text would.
        pass_env: env vars to forward into the subprocess. Defaults to
            an empty list — the resolver only sees what we hand it.
            Operators add ``["VAULT_ADDR", "VAULT_TOKEN"]`` etc.

    Safety rails:

    * ``shell=False`` (subprocess.run with a list argv).
    * ``command`` must be absolute and present at construction time —
      construction fails fast if the binary is missing.
    * Argv elements containing ``{id}`` are formatted with **only** the
      ``id`` field; nothing else interpolates so a malicious ``id``
      can't smuggle in alternative format placeholders.
    """

    def __init__(
        self,
        *,
        command: str,
        args_template: Sequence[str],
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        pass_env: Sequence[str] = (),
    ) -> None:
        if not os.path.isabs(command):
            raise SecretProviderError(
                f"exec provider command must be an absolute path, got {command!r}"
            )
        if not Path(command).is_file():
            raise SecretProviderError(
                f"exec provider command not found at {command!r}"
            )
        # Resolve symlinks so the audit log records the real binary.
        self._command = os.path.realpath(command)
        self._args_template = tuple(args_template)
        self._timeout_s = float(timeout_s)
        self._max_output_bytes = int(max_output_bytes)
        self._pass_env = tuple(pass_env)

    def resolve(self, key: str) -> str:
        argv = [self._command] + [
            a.format_map({"id": key}) for a in self._args_template
        ]
        env = {var: os.environ[var] for var in self._pass_env if var in os.environ}
        # PATH must always be set so the subprocess can locate libraries
        # via ld even though we never let it shell out.
        env.setdefault("PATH", os.environ.get("PATH", ""))
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=self._timeout_s,
                env=env,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise SecretProviderError(
                f"exec provider timeout after {self._timeout_s}s for {key!r}"
            ) from e
        except OSError as e:  # binary became unavailable since construction
            raise SecretProviderError(
                f"exec provider OS error for {key!r}: {e}"
            ) from e
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise SecretProviderError(
                f"exec provider exit {proc.returncode} for {key!r}: {stderr[:200]}"
            )
        if len(proc.stdout) > self._max_output_bytes:
            raise SecretProviderError(
                f"exec provider output exceeded {self._max_output_bytes} bytes for {key!r}"
            )
        # Strip trailing newline that CLIs typically add. Don't strip
        # leading whitespace — secrets can begin with whitespace
        # (unlikely but possible for binary-encoded tokens).
        return proc.stdout.decode("utf-8", errors="replace").rstrip("\n")


class FileSecretProvider(SecretProvider):
    """Resolves a secret by reading a value out of a local JSON file.

    Two-part lookup string:

    * Plain key — ``api_keys/anthropic`` — interpreted as a JSON pointer
      (RFC 6901-style ``/``-separated path). The pointer traverses
      nested objects/arrays; a missing segment is a hard error.
    * Single key — ``ANTHROPIC_API_KEY`` (no ``/``) — short-circuit to
      a top-level dict lookup.

    Args:
        path: absolute path to the secrets file.
        encoding: text encoding (default utf-8).
        require_strict_perms: when True (default), refuse to read the
            file unless its permissions are ``0o600`` or stricter
            (owner-only). Stops the operator from accidentally
            world-readable credentials. Pass False for tests or for
            files managed by an external secrets store.
    """

    def __init__(
        self,
        *,
        path: str | Path,
        encoding: str = "utf-8",
        require_strict_perms: bool = True,
    ) -> None:
        p = Path(path).expanduser()
        if not p.is_absolute():
            raise SecretProviderError(
                f"file provider path must be absolute, got {path!r}"
            )
        if not p.is_file():
            raise SecretProviderError(
                f"file provider path not found at {p}"
            )
        if require_strict_perms:
            mode = p.stat().st_mode & 0o777
            if mode & 0o077:
                raise SecretProviderError(
                    f"file provider path {p} is world/group readable "
                    f"(mode 0o{mode:03o}); chmod 600 it before use"
                )
        self._path = p
        self._encoding = encoding

    def resolve(self, key: str) -> str:
        try:
            text = self._path.read_text(encoding=self._encoding)
        except OSError as e:
            raise SecretProviderError(
                f"file provider could not read {self._path}: {e}"
            ) from e
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError as e:
            raise SecretProviderError(
                f"file provider {self._path} is not valid JSON: {e}"
            ) from e
        # JSON-pointer-ish traversal. We accept both ``"foo/bar"`` and
        # ``"foo.bar"`` so operators don't trip on convention drift.
        segments = [s for s in re.split(r"[/.]", key) if s]
        if not segments:
            raise SecretProviderError(
                f"file provider lookup {key!r} is empty after splitting on /."
            )
        node: Any = data
        for seg in segments:
            if isinstance(node, dict):
                if seg not in node:
                    raise SecretProviderError(
                        f"file provider: key {seg!r} not found in {self._path}"
                    )
                node = node[seg]
            elif isinstance(node, list):
                try:
                    idx = int(seg)
                except ValueError as e:
                    raise SecretProviderError(
                        f"file provider: array index must be int, got {seg!r}"
                    ) from e
                try:
                    node = node[idx]
                except IndexError as e:
                    raise SecretProviderError(
                        f"file provider: index {idx} out of range at {key!r}"
                    ) from e
            else:
                raise SecretProviderError(
                    f"file provider: cannot descend into non-container at {seg!r} in {key!r}"
                )
        if not isinstance(node, str):
            raise SecretProviderError(
                f"file provider: value at {key!r} is not a string "
                f"(got {type(node).__name__})"
            )
        return node


# ─── registry ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SecretSpec:
    """A single declared reference to be eager-resolved at startup.

    Attributes:
        id: the application-side identifier — what the rest of the
            code asks for via ``registry.get(id)``.
        source: which provider class handles this spec.
        lookup: provider-specific lookup string. For ``env`` it's the
            env-var name; for ``exec`` it's the secret id passed to
            the configured CLI; for ``file`` it's the JSON pointer
            into the file (``/``-separated, e.g. ``"api_keys/anthropic"``).
        provider_name: optional logical provider name, used to pick
            among multiple configured exec/file providers (e.g.
            ``"onepassword"`` vs ``"vault"``, or
            ``"main_secrets"`` vs ``"backup_secrets"``).
        export_as: when set, ``apply_secrets_to_environ`` will write the
            resolved value to ``os.environ[export_as]``. Use this to
            map a registry id (``"anthropic"``) to the env var name
            existing OC code reads (``"ANTHROPIC_API_KEY"``).
    """

    id: str
    source: Literal["env", "exec", "file"]
    lookup: str
    provider_name: str = "default"
    export_as: str = ""


@dataclass(slots=True)
class SecretRegistry:
    """Eager resolver — load a list of specs and serve resolved values."""

    _values: dict[str, str] = field(default_factory=dict)
    _specs: tuple[SecretSpec, ...] = ()
    _exec_providers: dict[str, ExecSecretProvider] = field(default_factory=dict)
    _file_providers: dict[str, FileSecretProvider] = field(default_factory=dict)
    _env_provider: EnvSecretProvider = field(default_factory=EnvSecretProvider)

    def register_exec_provider(
        self, name: str, provider: ExecSecretProvider,
    ) -> None:
        """Register an :class:`ExecSecretProvider` under *name*.

        Specs with ``source="exec"`` and ``provider_name=name`` will
        delegate to *provider* during :meth:`load`.
        """
        self._exec_providers[name] = provider

    def register_file_provider(
        self, name: str, provider: FileSecretProvider,
    ) -> None:
        """Register a :class:`FileSecretProvider` under *name*.

        Specs with ``source="file"`` and ``provider_name=name`` will
        delegate to *provider* during :meth:`load`.
        """
        self._file_providers[name] = provider

    def load(self, specs: Sequence[SecretSpec]) -> None:
        """Eager-resolve every spec. Failure preserves last-known-good.

        On any spec failing, we raise :class:`SecretProviderError` and
        leave ``self._values`` untouched — callers see their previous
        values, not a partially-rewritten map.
        """
        new_values: dict[str, str] = {}
        for spec in specs:
            if spec.source == "env":
                new_values[spec.id] = self._env_provider.resolve(spec.lookup)
            elif spec.source == "exec":
                provider = self._exec_providers.get(spec.provider_name)
                if provider is None:
                    raise SecretProviderError(
                        f"spec {spec.id!r} requires exec provider "
                        f"{spec.provider_name!r} which is not registered"
                    )
                new_values[spec.id] = provider.resolve(spec.lookup)
            elif spec.source == "file":
                file_provider = self._file_providers.get(spec.provider_name)
                if file_provider is None:
                    raise SecretProviderError(
                        f"spec {spec.id!r} requires file provider "
                        f"{spec.provider_name!r} which is not registered"
                    )
                new_values[spec.id] = file_provider.resolve(spec.lookup)
            else:  # pragma: no cover — defensive; literal narrows it already
                raise SecretProviderError(
                    f"spec {spec.id!r} has unknown source {spec.source!r}"
                )
        # Atomic swap.
        self._values = new_values
        self._specs = tuple(specs)

    def specs(self) -> tuple[SecretSpec, ...]:
        """Return the currently-loaded specs (post-:meth:`load`).

        Useful for :func:`apply_secrets_to_environ` and audit logs.
        """
        return self._specs

    def get(self, id: str) -> str | None:
        return self._values.get(id)

    def resolve_wire(self, payload: Any) -> Any:
        """Resolve a wire-shape :class:`SecretRef` payload to its value.

        * ``{"$secret_ref": id, ...}`` → resolved value (or raises if
          the id isn't registered).
        * Anything else → returned as-is.
        """
        if isinstance(payload, dict) and "$secret_ref" in payload:
            ref_id = str(payload.get("$secret_ref"))
            value = self._values.get(ref_id)
            if value is None:
                raise SecretProviderError(
                    f"unknown secret ref {ref_id!r} (registry empty? "
                    f"call SecretRegistry.load() first)"
                )
            return value
        if isinstance(payload, SecretRef):
            value = self._values.get(payload.ref_id)
            if value is None:
                raise SecretProviderError(
                    f"unknown secret ref {payload.ref_id!r}"
                )
            return value
        return payload


# ─── audit ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """One entry in the ``oc secrets audit`` report."""

    kind: Literal["plaintext_secret", "secret_ref_present", "unresolved_ref"]
    path: Path
    detail: str
    line: int | None = None


# Heuristic patterns for plaintext-shaped credentials. Conservative —
# false positives are noisy but a missed plaintext token is worse than
# a noisy audit. Each pattern targets a credential *shape*, not a
# specific brand, so the audit stays useful as the ecosystem grows.
_PLAINTEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_api_key", re.compile(r"\bsk-ant-[a-zA-Z0-9_\-]{16,}\b")),
    ("openai_api_key", re.compile(r"\bsk-[a-zA-Z0-9_\-]{20,}\b")),
    ("oauth_token", re.compile(r"\b[a-zA-Z0-9_\-]{40,}\b\s*$")),
    ("telegram_bot_token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}\b")),
    ("github_pat", re.compile(r"\bgh[opsu]_[A-Za-z0-9]{30,}\b")),
)
# Field-name hints — a YAML key like ``api_key`` or ``token`` followed
# by a non-quoted-empty / non-secret-ref value.
_FIELD_HINT_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*?(?:_token|_key|_secret|password))\s*[:=]\s*"
    r"(?P<val>[^\s#].*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SECRET_REF_RE = re.compile(r"\$secret_ref")


def audit_paths(paths: Sequence[Path]) -> list[AuditFinding]:
    """Walk *paths* and return findings.

    Skips paths that don't exist (caller may pass an aspirational list
    of "places we'd look").
    """
    findings: list[AuditFinding] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            _log.debug("secrets audit skipping unreadable %s: %s", path, e)
            continue
        # 1. SecretRef presence — informational, not a problem.
        if _SECRET_REF_RE.search(text):
            findings.append(
                AuditFinding(
                    kind="secret_ref_present",
                    path=path,
                    detail="contains $secret_ref reference (good)",
                )
            )
        # 2. Plaintext-pattern matches.
        for kind, pattern in _PLAINTEXT_PATTERNS:
            for line_no, line in enumerate(text.splitlines(), start=1):
                if "$secret_ref" in line:
                    continue  # already a ref, skip the line
                if pattern.search(line):
                    findings.append(
                        AuditFinding(
                            kind="plaintext_secret",
                            path=path,
                            detail=f"line {line_no}: {kind} pattern matched",
                            line=line_no,
                        )
                    )
                    break  # one match per file per pattern is enough
        # 3. Field-name hints — flag any "<thing>_key: rawvalue" that's
        #    not a SecretRef. Skips empty values + already-flagged
        #    plaintext lines.
        already_flagged_lines = {f.line for f in findings if f.path == path}
        for m in _FIELD_HINT_RE.finditer(text):
            val = m.group("val").strip()
            if not val or val.startswith("{") or "$secret_ref" in val:
                continue
            # Strip surrounding quotes for the emptiness test.
            stripped = val.strip("'\"")
            if not stripped or stripped.lower() in {"null", "none", "~"}:
                continue
            line_no = text[: m.start()].count("\n") + 1
            if line_no in already_flagged_lines:
                continue
            findings.append(
                AuditFinding(
                    kind="plaintext_secret",
                    path=path,
                    detail=f"line {line_no}: field {m.group('key')!r} appears to hold a plaintext value",
                    line=line_no,
                )
            )
    return findings


# ─── startup wire-in ──────────────────────────────────────────────────


def apply_secrets_to_environ(
    registry: SecretRegistry,
    *,
    overwrite_existing: bool = True,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Export resolved secrets into ``environ`` based on each spec's
    ``export_as``.

    Behaviour:

    * Specs whose ``export_as`` is empty are skipped — the operator did
      not declare an env-var target for them, so they remain
      registry-only (callers consult ``registry.get(id)``).
    * For specs with ``export_as``, the resolved value is written to
      ``environ[export_as]``. The previous value (if any) is logged at
      WARNING level so the operator sees plaintext-vs-ref conflicts.
    * ``overwrite_existing`` controls whether existing values are
      replaced. Default ``True`` — refs take precedence over plaintext
      env vars (OpenClaw spec: "If ref and plaintext both exist, ref
      wins at runtime"). Set False to honour pre-existing env values
      when the operator has explicitly set them.

    Returns the mutated ``environ`` (same dict identity as passed in).
    Defaults to mutating ``os.environ`` directly when no dict is given,
    so the most common call site (OC startup) is one line.
    """
    target: dict[str, str] = environ if environ is not None else os.environ  # type: ignore[assignment]
    conflicts: list[str] = []
    for spec in registry.specs():
        if not spec.export_as:
            continue
        resolved = registry.get(spec.id)
        if resolved is None:
            # Shouldn't happen post-load — but defend against caller
            # using a registry whose load() raised mid-way.
            _log.warning(
                "secrets.apply: spec %r resolved to None — skipping export",
                spec.id,
            )
            continue
        existing = target.get(spec.export_as)
        if existing == resolved:
            continue  # idempotent
        if existing is not None and existing != "":
            conflicts.append(spec.export_as)
            if not overwrite_existing:
                _log.warning(
                    "secrets.apply: %s already set in env (length=%d); "
                    "keeping existing value (overwrite_existing=False)",
                    spec.export_as, len(existing),
                )
                continue
            _log.warning(
                "secrets.apply: %s already set in env (length=%d); "
                "ref-resolved value wins per OpenClaw spec — old value "
                "DISCARDED in-process",
                spec.export_as, len(existing),
            )
        target[spec.export_as] = resolved
    if conflicts:
        _log.info(
            "secrets.apply: reconciled %d plaintext-vs-ref conflicts: %s",
            len(conflicts), ", ".join(sorted(conflicts)),
        )
    return target


def load_secrets_at_startup(
    *,
    profile_home: Path | None = None,
    overwrite_existing: bool = True,
) -> SecretRegistry | None:
    """Read ``<profile_home>/secrets.json``, resolve every spec, apply
    to ``os.environ``. Called once during OC bootstrap.

    Returns the populated registry on success, or ``None`` if there is
    nothing to load (no ``secrets.json`` file, no specs). The caller
    keeps the returned registry for later ``registry.resolve_wire(...)``
    calls.

    Failure modes:

    * ``secrets.json`` exists but is malformed → log error + return None.
      Startup proceeds with whatever env vars the operator had pre-set.
    * A spec fails to resolve → log error + return None. Startup
      proceeds; existing env vars stay intact (no partial application).

    Both failure modes are loud-but-non-fatal so a broken secrets file
    doesn't kill the daemon entirely; the operator sees the error in
    logs and `oc secrets audit` will surface it.
    """
    if profile_home is None:
        profile_home = Path(os.environ.get(
            "OC_PROFILE_DIR",
            str(Path.home() / ".opencomputer" / "default"),
        )).expanduser()
    secrets_path = profile_home / "secrets.json"
    if not secrets_path.is_file():
        return None
    try:
        raw = json.loads(secrets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.error(
            "secrets: cannot parse %s: %s — startup continues with plain env",
            secrets_path, e,
        )
        return None
    specs = _parse_specs_from_dict(raw)
    if not specs:
        return None
    registry = SecretRegistry()
    # Register declared exec/file providers from the same JSON. Operators
    # declare them once at the top level so multiple specs can share a
    # provider config.
    try:
        _register_providers_from_dict(registry, raw)
    except SecretProviderError as e:
        _log.error(
            "secrets: provider registration failed (%s) — startup continues with plain env",
            e,
        )
        return None
    try:
        registry.load(specs)
    except SecretProviderError as e:
        _log.error(
            "secrets: spec resolution failed (%s) — startup continues with plain env",
            e,
        )
        return None
    apply_secrets_to_environ(registry, overwrite_existing=overwrite_existing)
    _log.info(
        "secrets: loaded %d spec(s) from %s; %d exported to env",
        len(specs), secrets_path,
        sum(1 for s in specs if s.export_as),
    )
    return registry


def _parse_specs_from_dict(raw: Any) -> list[SecretSpec]:
    """Parse ``secrets.json`` ``secrets`` list into typed specs."""
    if not isinstance(raw, dict):
        return []
    out: list[SecretSpec] = []
    for entry in raw.get("secrets") or []:
        if not isinstance(entry, dict):
            continue
        try:
            spec_id = str(entry["id"])
            source = entry["source"]
            if source not in ("env", "exec", "file"):
                _log.warning(
                    "secrets: spec %r has unknown source %r — skipped",
                    spec_id, source,
                )
                continue
            out.append(SecretSpec(
                id=spec_id,
                source=source,
                lookup=str(entry["lookup"]),
                provider_name=str(entry.get("provider_name", "default")),
                export_as=str(entry.get("export_as", "")),
            ))
        except (KeyError, TypeError) as e:
            _log.warning("secrets: malformed spec entry %r — %s", entry, e)
            continue
    return out


def _register_providers_from_dict(
    registry: SecretRegistry, raw: Any,
) -> None:
    """Read top-level ``providers`` dict in ``secrets.json`` and register
    each declared exec/file provider on the registry.

    Shape::

        {
          "providers": {
            "vault":       {"type": "exec", "command": "/opt/homebrew/bin/vault",
                            "args": ["kv", "get", "-field={id}", "secret/{id}"],
                            "timeout_s": 5, "max_output_bytes": 65536,
                            "pass_env": ["VAULT_ADDR", "VAULT_TOKEN"]},
            "onepassword": {"type": "exec", "command": "/opt/homebrew/bin/op",
                            "args": ["read", "{id}"]},
            "local_file":  {"type": "file", "path": "/Users/saksham/.opencomputer/secrets.local.json"}
          },
          "secrets": [...]
        }

    Unknown provider types are skipped with a warning rather than raising
    so a fresh OC install with a partially-typed secrets.json still
    boots.
    """
    if not isinstance(raw, dict):
        return
    providers = raw.get("providers") or {}
    if not isinstance(providers, dict):
        _log.warning(
            "secrets: providers must be a dict, got %s — skipping all",
            type(providers).__name__,
        )
        return
    for name, cfg in providers.items():
        if not isinstance(cfg, dict):
            _log.warning("secrets: provider %r config must be dict — skipped", name)
            continue
        ptype = cfg.get("type")
        if ptype == "exec":
            registry.register_exec_provider(
                str(name),
                ExecSecretProvider(
                    command=str(cfg["command"]),
                    args_template=tuple(cfg.get("args") or []),
                    timeout_s=float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S)),
                    max_output_bytes=int(cfg.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)),
                    pass_env=tuple(cfg.get("pass_env") or []),
                ),
            )
        elif ptype == "file":
            registry.register_file_provider(
                str(name),
                FileSecretProvider(
                    path=str(cfg["path"]),
                    encoding=str(cfg.get("encoding", "utf-8")),
                    require_strict_perms=bool(cfg.get("require_strict_perms", True)),
                ),
            )
        else:
            _log.warning(
                "secrets: provider %r has unknown type %r — skipped",
                name, ptype,
            )
