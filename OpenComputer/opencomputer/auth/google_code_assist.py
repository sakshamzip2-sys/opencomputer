"""Cloud Code Assist preflight + project resolution.

Wraps the ``cloudcode-pa.googleapis.com/v1internal:loadCodeAssist`` and
``:onboardUser`` endpoints used by Google's gemini-cli to pick a project_id
and tier for the user before any inference call.

Resolution order (mirrors Hermes ``resolve_project_context``):

  1. Caller-supplied ``configured_project_id`` (e.g. from ``ModelConfig``).
  2. ``OPENCOMPUTER_GEMINI_PROJECT_ID`` env var.
  3. ``GOOGLE_CLOUD_PROJECT`` / ``GOOGLE_CLOUD_PROJECT_ID`` env vars.
  4. ``loadCodeAssist`` preflight — Google reports the user's tier + assigned
     managed project (free-tier users get one auto-assigned by Google).
  5. ``onboardUser`` — if loadCodeAssist returns no tier, provision the
     user on free-tier so their first call succeeds.

Paid tiers REQUIRE an explicit project_id. Free tier gets a managed project
from Google. The caller can always set ``OPENCOMPUTER_GEMINI_PROJECT_ID``
to short-circuit all discovery.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"

FREE_TIER_ID = "free-tier"
LEGACY_TIER_ID = "legacy-tier"
STANDARD_TIER_ID = "standard-tier"

REQUEST_TIMEOUT_SECONDS = 30.0
ONBOARDING_POLL_ATTEMPTS = 12
ONBOARDING_POLL_INTERVAL_SECONDS = 5.0


# =============================================================================
# Errors
# =============================================================================

class CodeAssistError(RuntimeError):
    """Raised for any Cloud Code Assist HTTP / preflight failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "code_assist_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ProjectIdRequiredError(CodeAssistError):
    """Paid tiers need an explicit project_id; user must set the env var."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="project_id_required")


# =============================================================================
# Dataclasses
# =============================================================================

@dataclass
class CodeAssistProjectInfo:
    current_tier_id: str = ""
    cloudaicompanion_project: str = ""
    allowed_tiers: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectContext:
    project_id: str
    managed_project_id: str = ""
    tier_id: str = ""
    source: str = ""  # "config" / "env" / "discovered" / "onboarded"


# =============================================================================
# HTTP helper
# =============================================================================

def _client_metadata() -> dict[str, Any]:
    """Metadata Google's preflights expect from a client."""
    return {
        "ideType": "IDE_UNSPECIFIED",
        "platform": "PLATFORM_UNSPECIFIED",
        "pluginType": "GEMINI",
    }


def _build_headers(access_token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "opencomputer (gemini-cli-compat)",
        "X-Goog-Api-Client": "gl-python/opencomputer",
    }


def _post_json(
    url: str,
    body: dict[str, Any],
    access_token: str,
) -> dict[str, Any]:
    """POST JSON, raise CodeAssistError on non-200, return parsed dict."""
    response = httpx.post(
        url,
        headers=_build_headers(access_token),
        json=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code == 401:
        raise CodeAssistError(
            f"Cloud Code Assist unauthorized: {response.text[:200]}",
            code="code_assist_unauthorized",
            status_code=401,
        )
    if response.status_code == 429:
        raise CodeAssistError(
            f"Cloud Code Assist rate_limited: {response.text[:200]}",
            code="code_assist_rate_limited",
            status_code=429,
        )
    if response.status_code != 200:
        raise CodeAssistError(
            f"Cloud Code Assist error {response.status_code}: {response.text[:200]}",
            code="code_assist_error",
            status_code=response.status_code,
        )
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise CodeAssistError(
            f"Cloud Code Assist returned non-JSON: {response.text[:200]}"
        ) from exc


# =============================================================================
# loadCodeAssist
# =============================================================================

def load_code_assist(
    access_token: str,
    *,
    project_id: str = "",
) -> CodeAssistProjectInfo:
    """``POST :loadCodeAssist`` — discover tier + assigned project."""
    body: dict[str, Any] = {
        "metadata": {
            "duetProject": project_id,
            **_client_metadata(),
        },
    }
    if project_id:
        body["cloudaicompanionProject"] = project_id

    url = f"{CODE_ASSIST_ENDPOINT}/v1internal:loadCodeAssist"
    resp = _post_json(url, body, access_token)
    return _parse_load_response(resp)


def _parse_load_response(resp: dict[str, Any]) -> CodeAssistProjectInfo:
    current_tier = resp.get("currentTier") or {}
    tier_id = ""
    if isinstance(current_tier, dict):
        tier_id = str(current_tier.get("id") or "")
    project = str(resp.get("cloudaicompanionProject") or "")
    allowed_raw = resp.get("allowedTiers") or []
    allowed_ids: list[str] = []
    if isinstance(allowed_raw, list):
        for tier in allowed_raw:
            if isinstance(tier, dict):
                tid = str(tier.get("id") or "")
                if tid:
                    allowed_ids.append(tid)
    return CodeAssistProjectInfo(
        current_tier_id=tier_id,
        cloudaicompanion_project=project,
        allowed_tiers=allowed_ids,
        raw=resp,
    )


# =============================================================================
# onboardUser
# =============================================================================

def onboard_user(
    access_token: str,
    *,
    tier_id: str,
    project_id: str = "",
) -> dict[str, Any]:
    """``POST :onboardUser`` — provision the user on the given tier.

    Paid tiers (everything except free/legacy) REQUIRE ``project_id``. Free
    tier auto-assigns one. Returns the final operation response after polling
    if the response is a long-running operation.
    """
    if tier_id not in {FREE_TIER_ID, LEGACY_TIER_ID} and not project_id:
        raise ProjectIdRequiredError(
            f"Tier {tier_id!r} requires a GCP project id. Set "
            "OPENCOMPUTER_GEMINI_PROJECT_ID or GOOGLE_CLOUD_PROJECT."
        )

    body: dict[str, Any] = {
        "tierId": tier_id,
        "metadata": _client_metadata(),
    }
    if project_id:
        body["cloudaicompanionProject"] = project_id

    url = f"{CODE_ASSIST_ENDPOINT}/v1internal:onboardUser"
    resp = _post_json(url, body, access_token)

    # Poll if this is a long-running operation
    if not resp.get("done"):
        op_name = resp.get("name", "")
        if op_name:
            for _ in range(ONBOARDING_POLL_ATTEMPTS):
                time.sleep(ONBOARDING_POLL_INTERVAL_SECONDS)
                poll_url = f"{CODE_ASSIST_ENDPOINT}/v1internal/{op_name}"
                try:
                    poll_resp = _post_json(poll_url, {}, access_token)
                except CodeAssistError as exc:
                    logger.warning("Onboarding poll failed: %s", exc)
                    continue
                if poll_resp.get("done"):
                    return poll_resp
            logger.warning(
                "Onboarding did not complete in %d polls",
                ONBOARDING_POLL_ATTEMPTS,
            )
    return resp


# =============================================================================
# resolve_project_context — the public entry point
# =============================================================================

def _env_project_id() -> str:
    """Resolve project_id from any of the supported env vars."""
    for name in (
        "OPENCOMPUTER_GEMINI_PROJECT_ID",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_PROJECT_ID",
    ):
        value = os.environ.get(name)
        if value:
            return value
    return ""


def resolve_project_context(
    access_token: str,
    *,
    configured_project_id: str = "",
) -> ProjectContext:
    """Pick the right project_id + tier for inference calls.

    Resolution: configured > env > loadCodeAssist discovery > onboard.
    """
    if configured_project_id:
        return ProjectContext(
            project_id=configured_project_id,
            tier_id=STANDARD_TIER_ID,
            source="config",
        )

    env_proj = _env_project_id()
    if env_proj:
        return ProjectContext(
            project_id=env_proj,
            tier_id=STANDARD_TIER_ID,
            source="env",
        )

    info = load_code_assist(access_token)
    project = info.cloudaicompanion_project
    tier = info.current_tier_id

    if not tier:
        onboard_resp = onboard_user(access_token, tier_id=FREE_TIER_ID, project_id="")
        response_body = onboard_resp.get("response") or {}
        if isinstance(response_body, dict):
            project = project or str(response_body.get("cloudaicompanionProject") or "")
        return ProjectContext(
            project_id=project,
            managed_project_id=project,
            tier_id=FREE_TIER_ID,
            source="onboarded",
        )

    return ProjectContext(
        project_id=project,
        managed_project_id=project if tier == FREE_TIER_ID else "",
        tier_id=tier,
        source="discovered",
    )


__all__ = [
    "CODE_ASSIST_ENDPOINT",
    "CodeAssistError",
    "CodeAssistProjectInfo",
    "FREE_TIER_ID",
    "LEGACY_TIER_ID",
    "STANDARD_TIER_ID",
    "ProjectContext",
    "ProjectIdRequiredError",
    "load_code_assist",
    "onboard_user",
    "resolve_project_context",
]
