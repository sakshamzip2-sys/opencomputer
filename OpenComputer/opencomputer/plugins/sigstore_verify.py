"""Sigstore signature verification wrapper (v1.1 plan-3 M11.3 follow-up).

Wraps the optional ``cosign`` binary so installers can verify sigstore
signatures alongside the manifest sha256 pin.  When ``cosign`` is
unavailable (the default for most users), this module returns
:class:`SigstoreUnavailableError` so callers fall back to the existing
sha256 pin without crashing.

Production model:

* **Opt-in.**  The installer only requires sigstore verification when
  the operator passes ``--require-sigstore`` (or sets
  ``OC_PLUGIN_REQUIRE_SIGSTORE=1``).  Without that flag the wrapper is
  best-effort: if cosign succeeds the result is recorded; if it fails
  or is absent the install proceeds with the sha256 pin alone.
* **No silent vendoring.**  Cosign is shelled out, never linked.
  The wrapper inspects the binary's existence + version once and
  caches the result for the process lifetime.
* **Signature provenance.**  When verification succeeds, the returned
  :class:`SigstoreVerification` carries the cosign output verbatim so
  the installed-index can preserve it for ``oc plugin verify``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("opencomputer.plugins.sigstore_verify")


class SigstoreUnavailableError(RuntimeError):
    """Raised by :func:`require_cosign` when cosign isn't on PATH."""


class SigstoreVerificationFailedError(RuntimeError):
    """Raised when cosign returns non-zero for a signature claim."""


@dataclass(frozen=True, slots=True)
class SigstoreVerification:
    """Outcome of a successful cosign verification."""

    cosign_version: str
    artifact: str
    signature: str
    certificate: str = ""
    output: str = ""
    """Verbatim cosign stdout — recorded for ``oc plugin verify``."""
    metadata: dict[str, str] = field(default_factory=dict)


def is_required_by_env() -> bool:
    """Return True if the operator demanded sigstore via env var."""
    return os.environ.get("OC_PLUGIN_REQUIRE_SIGSTORE", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


@lru_cache(maxsize=1)
def cosign_path() -> str | None:
    """Locate cosign on PATH; cached for the process lifetime."""
    return shutil.which("cosign")


def require_cosign() -> str:
    """Resolve cosign binary; raise :class:`SigstoreUnavailableError` if absent."""
    p = cosign_path()
    if p is None:
        raise SigstoreUnavailableError(
            "cosign binary not found on PATH. Install from "
            "https://github.com/sigstore/cosign or unset "
            "OC_PLUGIN_REQUIRE_SIGSTORE=1."
        )
    return p


def verify_blob(
    artifact_path: Path,
    *,
    signature_path: Path,
    certificate_path: Path | None = None,
    cert_identity: str | None = None,
    cert_oidc_issuer: str | None = None,
    extra_args: list[str] | None = None,
    cosign_runner: object | None = None,
) -> SigstoreVerification:
    """Verify ``artifact_path`` against ``signature_path`` via cosign.

    Args:
        artifact_path: file whose bytes are signed (typically the
            extracted plugin tarball; the same bytes whose sha256 is
            recorded in the installed-index).
        signature_path: cosign signature blob (``.sig``).
        certificate_path: keyless-mode certificate (``.cert`` /
            ``.pem``).  Required when verifying a keyless signature.
        cert_identity: the expected signer identity (e.g.
            ``https://github.com/owner/repo/.github/workflows/release.yml@refs/tags/v1.2.3``).
        cert_oidc_issuer: the expected OIDC issuer (e.g.
            ``https://token.actions.githubusercontent.com``).
        extra_args: passthrough flags for cosign (e.g. transparency-log
            controls).
        cosign_runner: test injection point.  Defaults to
            :func:`subprocess.run`.

    Returns:
        :class:`SigstoreVerification` on successful verification.

    Raises:
        :class:`SigstoreUnavailableError`: cosign not found.
        :class:`SigstoreVerificationFailedError`: cosign exited non-zero or
            paths are missing.
    """
    if not artifact_path.exists():
        raise SigstoreVerificationFailedError(
            f"artifact does not exist: {artifact_path}"
        )
    if not signature_path.exists():
        raise SigstoreVerificationFailedError(
            f"signature does not exist: {signature_path}"
        )
    if certificate_path is not None and not certificate_path.exists():
        raise SigstoreVerificationFailedError(
            f"certificate does not exist: {certificate_path}"
        )

    binary = require_cosign()

    cmd = [binary, "verify-blob", "--signature", str(signature_path)]
    if certificate_path is not None:
        cmd.extend(["--certificate", str(certificate_path)])
    if cert_identity is not None:
        cmd.extend(["--certificate-identity", cert_identity])
    if cert_oidc_issuer is not None:
        cmd.extend(["--certificate-oidc-issuer", cert_oidc_issuer])
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(artifact_path))

    runner = cosign_runner or subprocess.run
    try:
        result = runner(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise SigstoreVerificationFailedError(
            f"cosign verify-blob failed: "
            f"{(exc.stderr or '').strip() or exc}"
        ) from exc

    output = (
        getattr(result, "stdout", "") or getattr(result, "stderr", "") or ""
    )
    version = _detect_cosign_version(binary, runner=runner)
    return SigstoreVerification(
        cosign_version=version,
        artifact=str(artifact_path),
        signature=str(signature_path),
        certificate=str(certificate_path) if certificate_path else "",
        output=output.strip(),
    )


@lru_cache(maxsize=1)
def _cached_version(binary: str) -> str:
    try:
        out = subprocess.run(
            [binary, "version"], check=True, capture_output=True, text=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""
    return (out.stdout or out.stderr or "").strip().splitlines()[0] if (
        out.stdout or out.stderr
    ) else ""


def _detect_cosign_version(binary: str, *, runner: object) -> str:
    """Detect cosign version using the same runner the caller used.

    Falls back to ``subprocess.run`` cache for the production path.
    """
    if runner is subprocess.run:
        return _cached_version(binary)
    try:
        result = runner(
            [binary, "version"], check=True, capture_output=True, text=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""
    out = getattr(result, "stdout", "") or getattr(result, "stderr", "") or ""
    return out.strip().splitlines()[0] if out else ""


def verify_or_warn(
    artifact_path: Path,
    *,
    signature_path: Path | None,
    require: bool,
    **kwargs: object,
) -> SigstoreVerification | None:
    """Best-effort verification helper used by installers.

    * ``signature_path is None`` and ``require=False`` → returns None
      (no signature provided; caller falls back to sha256 pin).
    * ``signature_path is None`` and ``require=True`` → raises
      :class:`SigstoreVerificationFailedError`.
    * Cosign missing and ``require=True`` → raises
      :class:`SigstoreUnavailableError`.
    * Cosign missing and ``require=False`` → returns None with a
      WARN-level log line (operator opted in via env but cosign isn't
      installed; we degrade gracefully).
    * Verification raises → re-raised when ``require=True``; logged
      and dropped to ``None`` when ``require=False``.
    """
    if signature_path is None:
        if require:
            raise SigstoreVerificationFailedError(
                "sigstore verification required but no signature was "
                "provided to the installer."
            )
        return None

    try:
        return verify_blob(
            artifact_path, signature_path=signature_path, **kwargs
        )
    except SigstoreUnavailableError:
        if require:
            raise
        logger.warning(
            "sigstore verification skipped: cosign not on PATH "
            "(set OC_PLUGIN_REQUIRE_SIGSTORE=1 to fail-closed)."
        )
        return None
    except SigstoreVerificationFailedError:
        if require:
            raise
        logger.warning(
            "sigstore verification failed for %s; install proceeding "
            "based on sha256 pin alone.",
            artifact_path,
        )
        return None


__all__ = [
    "SigstoreUnavailableError",
    "SigstoreVerification",
    "SigstoreVerificationFailedError",
    "cosign_path",
    "is_required_by_env",
    "require_cosign",
    "verify_blob",
    "verify_or_warn",
]
