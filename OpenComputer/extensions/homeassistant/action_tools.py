"""Home Assistant action tools — Hermes parity port (2026-05-01).

Four BaseTool subclasses giving the agent the same control over a HA
instance that ``ha_list_entities`` / ``ha_get_state`` / ``ha_list_services``
/ ``ha_call_service`` give it in Hermes:

* :class:`HAListEntitiesTool` — list/filter entities by domain or area
* :class:`HAGetStateTool` — fetch detailed state of a single entity
* :class:`HAListServicesTool` — discover available services per domain
* :class:`HACallServiceTool` — call ``turn_on`` / ``set_temperature`` / etc.

Two capabilities:

* ``homeassistant.read`` (IMPLICIT) — the three discovery tools.
* ``homeassistant.call_service`` (EXPLICIT) — :class:`HACallServiceTool`.

Auth via the existing OC env vars ``HOMEASSISTANT_URL`` +
``HOMEASSISTANT_TOKEN`` — same convention the channel adapter already uses.

Security guardrails (carried over from Hermes — without them this is an
arbitrary-code-execution gateway):

* Entity-ID regex (path-traversal / SSRF prevention).
* Service / domain regex (same).
* Blocked-domain frozenset for ``shell_command`` / ``python_script`` /
  ``hassio`` / ``rest_command`` etc. — these allow arbitrary code execution
  on the HA host or SSRF on the local network.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, ClassVar

import httpx

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger("opencomputer.homeassistant.action_tools")


# Entity / service regexes — same as Hermes. Keep these tight.
_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Hard blocklist — these domains have full code-execution / SSRF surface.
_BLOCKED_DOMAINS = frozenset({
    "shell_command",
    "command_line",
    "python_script",
    "pyscript",
    "hassio",
    "rest_command",
})


def _get_config() -> tuple[str, str]:
    """Return (url, token) read from env. URL is right-stripped of '/'."""
    url = os.environ.get("HOMEASSISTANT_URL", "http://homeassistant.local:8123").rstrip("/")
    token = os.environ.get("HOMEASSISTANT_TOKEN", "")
    return url, token


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# ─── async REST helpers ──────────────────────────────────────────


def _filter_states(
    states: list[dict[str, Any]],
    *,
    domain: str | None,
    area: str | None,
) -> dict[str, Any]:
    """Filter raw HA states by domain/area and return a compact summary."""
    if domain:
        states = [s for s in states if s.get("entity_id", "").startswith(f"{domain}.")]
    if area:
        area_l = area.lower()
        states = [
            s for s in states
            if area_l in (s.get("attributes", {}).get("friendly_name", "") or "").lower()
            or area_l in (s.get("attributes", {}).get("area", "") or "").lower()
        ]
    return {
        "count": len(states),
        "entities": [
            {
                "entity_id": s["entity_id"],
                "state": s["state"],
                "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
            }
            for s in states
        ],
    }


async def list_entities(domain: str | None = None, area: str | None = None) -> dict[str, Any]:
    url, token = _get_config()
    if not token:
        return {"error": "HOMEASSISTANT_TOKEN not set"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{url}/api/states", headers=_headers(token))
        resp.raise_for_status()
        states = resp.json()
    return _filter_states(states, domain=domain, area=area)


async def get_entity_state(entity_id: str) -> dict[str, Any]:
    url, token = _get_config()
    if not token:
        return {"error": "HOMEASSISTANT_TOKEN not set"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{url}/api/states/{entity_id}", headers=_headers(token))
        resp.raise_for_status()
        data = resp.json()
    return {
        "entity_id": data["entity_id"],
        "state": data["state"],
        "attributes": data.get("attributes", {}),
        "last_changed": data.get("last_changed"),
        "last_updated": data.get("last_updated"),
    }


async def list_services(domain: str | None = None) -> dict[str, Any]:
    url, token = _get_config()
    if not token:
        return {"error": "HOMEASSISTANT_TOKEN not set"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{url}/api/services", headers=_headers(token))
        resp.raise_for_status()
        services = resp.json()

    if domain:
        services = [s for s in services if s.get("domain") == domain]

    out = []
    for svc_domain in services:
        d = svc_domain.get("domain", "")
        domain_services: dict[str, Any] = {}
        for svc_name, svc_info in svc_domain.get("services", {}).items():
            entry: dict[str, Any] = {"description": svc_info.get("description", "")}
            fields = svc_info.get("fields", {})
            if fields:
                entry["fields"] = {
                    k: v.get("description", "") for k, v in fields.items()
                    if isinstance(v, dict)
                }
            domain_services[svc_name] = entry
        out.append({"domain": d, "services": domain_services})
    return {"count": len(out), "domains": out}


async def call_service(
    domain: str, service: str,
    *,
    entity_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url, token = _get_config()
    if not token:
        return {"error": "HOMEASSISTANT_TOKEN not set"}
    payload: dict[str, Any] = {}
    if data:
        payload.update(data)
    if entity_id:
        payload["entity_id"] = entity_id

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/api/services/{domain}/{service}",
            headers=_headers(token), json=payload,
        )
        resp.raise_for_status()
        result = resp.json()

    affected: list[dict[str, Any]] = []
    if isinstance(result, list):
        for s in result:
            affected.append({
                "entity_id": s.get("entity_id", ""),
                "state": s.get("state", ""),
            })
    return {
        "success": True,
        "service": f"{domain}.{service}",
        "affected_entities": affected,
    }


# ─── Common base ────────────────────────────────────────────────


def _err(call: ToolCall, msg: str) -> ToolResult:
    return ToolResult(
        tool_call_id=call.id,
        content=json.dumps({"error": msg}),
        is_error=True,
    )


def _ok(call: ToolCall, payload: Any) -> ToolResult:
    return ToolResult(tool_call_id=call.id, content=json.dumps({"result": payload}))


class _HABaseTool(BaseTool):
    """Common __init__ (consent_gate / sandbox / audit) for HA tools."""

    parallel_safe: bool = True

    def __init__(
        self, *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit


# ─── Read tools ─────────────────────────────────────────────────


class HAListEntitiesTool(_HABaseTool):
    """List Home Assistant entities, optionally filtered by domain or area."""

    consent_tier: int = 1
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="homeassistant.read",
            tier_required=ConsentTier.IMPLICIT,
            human_description="List Home Assistant entities and their states.",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ha_list_entities",
            description=(
                "List Home Assistant entities. Optionally filter by domain "
                "(light, switch, climate, sensor, binary_sensor, cover, fan, "
                "etc.) or area name (living room, kitchen, bedroom, etc.). "
                "Read-only — no devices are touched."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Entity domain filter (e.g. 'light', 'switch'). Optional.",
                    },
                    "area": {
                        "type": "string",
                        "description": "Area/room name filter. Matches against friendly names. Optional.",
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        try:
            result = await list_entities(domain=args.get("domain"), area=args.get("area"))
        except Exception as exc:  # noqa: BLE001
            _log.error("ha_list_entities error: %s", exc)
            return _err(call, f"Failed to list entities: {exc}")
        return _ok(call, result)


class HAGetStateTool(_HABaseTool):
    """Get the detailed state of a single Home Assistant entity."""

    consent_tier: int = 1
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="homeassistant.read",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Read state + attributes of a single entity.",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ha_get_state",
            description=(
                "Get the detailed state of a single Home Assistant entity, "
                "including all attributes (brightness, color, temperature "
                "setpoint, sensor readings, etc.). Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "Entity ID (e.g. 'light.living_room', 'climate.thermostat').",
                    },
                },
                "required": ["entity_id"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        entity_id = (call.arguments or {}).get("entity_id", "")
        if not entity_id:
            return _err(call, "Missing required parameter: entity_id")
        if not _ENTITY_ID_RE.match(entity_id):
            return _err(call, f"Invalid entity_id format: {entity_id!r}")
        try:
            result = await get_entity_state(entity_id)
        except Exception as exc:  # noqa: BLE001
            _log.error("ha_get_state error: %s", exc)
            return _err(call, f"Failed to get state for {entity_id}: {exc}")
        return _ok(call, result)


class HAListServicesTool(_HABaseTool):
    """List available Home Assistant services for device control."""

    consent_tier: int = 1
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="homeassistant.read",
            tier_required=ConsentTier.IMPLICIT,
            human_description="List available HA services per domain.",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ha_list_services",
            description=(
                "List available Home Assistant services (actions) for device "
                "control. Shows what actions can be performed on each device "
                "type and what parameters they accept. Use this to discover "
                "how to control devices found via ha_list_entities. Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Filter by domain. Omit to list services for all domains.",
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        try:
            result = await list_services(domain=args.get("domain"))
        except Exception as exc:  # noqa: BLE001
            _log.error("ha_list_services error: %s", exc)
            return _err(call, f"Failed to list services: {exc}")
        return _ok(call, result)


# ─── Mutating tool ──────────────────────────────────────────────


class HACallServiceTool(_HABaseTool):
    """Call a Home Assistant service to control a device."""

    consent_tier: int = 2
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="homeassistant.call_service",
            tier_required=ConsentTier.EXPLICIT,
            human_description=(
                "Mutate Home Assistant device state (turn on/off, set "
                "temperature, run scripts/scenes, etc.)."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ha_call_service",
            description=(
                "Call a Home Assistant service to control a device. Use "
                "ha_list_services to discover available services and their "
                "parameters for each domain. Blocks dangerous domains "
                "(shell_command, python_script, hassio, rest_command, "
                "command_line, pyscript) for security."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Service domain (e.g. 'light', 'switch', 'climate').",
                    },
                    "service": {
                        "type": "string",
                        "description": "Service name (e.g. 'turn_on', 'set_temperature').",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "Target entity ID (e.g. 'light.living_room'). "
                            "Some services (like scene.turn_on) may not need this."
                        ),
                    },
                    "data": {
                        "type": "string",
                        "description": (
                            'Additional service data as a JSON string. '
                            'Examples: {"brightness": 255, "color_name": "blue"}, '
                            '{"temperature": 22, "hvac_mode": "heat"}.'
                        ),
                    },
                },
                "required": ["domain", "service"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        domain = args.get("domain", "")
        service = args.get("service", "")
        if not domain or not service:
            return _err(call, "Missing required parameters: domain and service")

        # Validate format BEFORE blocklist — prevents path traversal in URL
        # and blocklist bypass via payloads like 'shell_command/../light'.
        if not _SERVICE_NAME_RE.match(domain):
            return _err(call, f"Invalid domain format: {domain!r}")
        if not _SERVICE_NAME_RE.match(service):
            return _err(call, f"Invalid service format: {service!r}")

        if domain in _BLOCKED_DOMAINS:
            return _err(
                call,
                (
                    f"Service domain '{domain}' is blocked for security. "
                    f"Blocked domains: {', '.join(sorted(_BLOCKED_DOMAINS))}"
                ),
            )

        entity_id = args.get("entity_id")
        if entity_id and not _ENTITY_ID_RE.match(entity_id):
            return _err(call, f"Invalid entity_id format: {entity_id!r}")

        data = args.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data) if data.strip() else None
            except json.JSONDecodeError as e:
                return _err(call, f"Invalid JSON string in 'data' parameter: {e}")

        try:
            result = await call_service(domain, service, entity_id=entity_id, data=data)
        except Exception as exc:  # noqa: BLE001
            _log.error("ha_call_service error: %s", exc)
            return _err(call, f"Failed to call {domain}.{service}: {exc}")
        return _ok(call, result)


ALL_TOOLS = [
    HAListEntitiesTool,
    HAGetStateTool,
    HAListServicesTool,
    HACallServiceTool,
]


__all__ = [
    "ALL_TOOLS",
    "HACallServiceTool",
    "HAGetStateTool",
    "HAListEntitiesTool",
    "HAListServicesTool",
    "_BLOCKED_DOMAINS",
    "_ENTITY_ID_RE",
    "_SERVICE_NAME_RE",
    "call_service",
    "get_entity_state",
    "list_entities",
    "list_services",
]
