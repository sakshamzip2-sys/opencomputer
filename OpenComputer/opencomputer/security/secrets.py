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
    "SecretProvider",
    "SecretProviderError",
    "SecretRegistry",
    "SecretSpec",
    "audit_paths",
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
            the configured CLI.
        provider_name: optional logical provider name, used to pick
            among multiple configured exec providers (e.g.
            ``"onepassword"`` vs ``"vault"``).
    """

    id: str
    source: Literal["env", "exec"]
    lookup: str
    provider_name: str = "default"


@dataclass(slots=True)
class SecretRegistry:
    """Eager resolver — load a list of specs and serve resolved values."""

    _values: dict[str, str] = field(default_factory=dict)
    _exec_providers: dict[str, ExecSecretProvider] = field(default_factory=dict)
    _env_provider: EnvSecretProvider = field(default_factory=EnvSecretProvider)

    def register_exec_provider(
        self, name: str, provider: ExecSecretProvider,
    ) -> None:
        """Register an :class:`ExecSecretProvider` under *name*.

        Specs with ``source="exec"`` and ``provider_name=name`` will
        delegate to *provider* during :meth:`load`.
        """
        self._exec_providers[name] = provider

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
            else:  # pragma: no cover — defensive; literal narrows it already
                raise SecretProviderError(
                    f"spec {spec.id!r} has unknown source {spec.source!r}"
                )
        # Atomic swap.
        self._values = new_values

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
