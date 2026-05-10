"""Smart-mode auxiliary-LLM risk assessor for the consent gate.

Hermes parity for ``approvals.mode: smart``. Replaces the
manual-prompt-fallback that ``parse_mode`` historically used for
``smart``. When the consent gate has to decide whether to allow a
destructive-shaped command without the user clicking, the smart-mode
path consults a separate, low-stakes LLM call to score the risk and
returns a deterministic verdict the gate can act on.

Design constraints (Hermes spec):

* Low-risk → auto-approve with session-level persistence.
* Genuinely dangerous → auto-deny.
* Uncertain → fall through to the manual prompt the user installed.

LLM contract:

The assessor sends the candidate command + a tight system prompt that
demands a strict JSON response::

    {"risk": "low" | "medium" | "high" | "uncertain", "reason": "..."}

Anything else (free-form text, missing fields, transport failure) is
treated as ``uncertain`` and the gate falls back to a manual prompt.
This is the conservative direction — never silently auto-allow on a
malformed response.

Resilience:

* Aux LLM unavailable / quota exhausted → ``uncertain``.
* Aux LLM JSON parse failure → ``uncertain``.
* Aux LLM hangs → 5-second timeout → ``uncertain``.
* Aux LLM returns an unknown risk label → ``uncertain``.

The module never raises to the caller. The verdict is the source of
truth.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("opencomputer.security.smart_mode")


RiskLevel = Literal["low", "medium", "high", "uncertain"]


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """Verdict the consent gate applies.

    Attributes:
        level: ``low`` / ``medium`` / ``high`` / ``uncertain``. Maps to
            gate behaviour in :meth:`apply_to_decision`.
        reason: short human-readable summary surfaced in the audit log
            so the operator can review smart-mode decisions later.
        used_fallback: ``True`` when the LLM call failed and the
            verdict is the conservative fallback (``uncertain``). Used
            by the gate to log a one-shot warning so smart-mode
            outages are visible.
    """

    level: RiskLevel
    reason: str
    used_fallback: bool = False

    @property
    def auto_allow(self) -> bool:
        """``True`` when the gate should auto-approve this call."""
        return self.level == "low"

    @property
    def auto_deny(self) -> bool:
        """``True`` when the gate should auto-deny this call."""
        return self.level == "high"

    @property
    def needs_manual(self) -> bool:
        """``True`` when the gate should fall through to a manual prompt."""
        return self.level in ("medium", "uncertain")


_SYSTEM_PROMPT = (
    "You are a security risk classifier for a coding agent. The user's "
    "command will be executed inside the agent's sandbox if you classify "
    "it as low risk. You must respond with a single JSON object and "
    "nothing else, of the shape:\n\n"
    '  {"risk": "low" | "medium" | "high" | "uncertain", '
    '"reason": "<one short sentence>"}\n\n'
    "Use these labels:\n"
    "  - low: routine read-only / introspection / lint / format / test commands\n"
    "  - medium: writes inside the project tree, package installs, git commits\n"
    "  - high: writes to /etc, /var, /usr, root paths, system services, "
    "kernel modules, package-system-wide installs, force-pushes to default "
    "branches, or anything matching a hardline pattern (rm -rf /, dd, mkfs, "
    "fork bomb, curl|sh)\n"
    "  - uncertain: command shape unfamiliar OR ambiguous OR multi-stage / "
    "obfuscated\n\n"
    "Default to higher caution on ambiguity. Reply MUST be valid JSON, "
    "no markdown fences, no prose."
)


_TIMEOUT_S: float = 5.0
_VALID_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "uncertain"})


async def assess_risk(
    command: str, *, capability_id: str = "", scope: str | None = None,
    model: str | None = None,
) -> RiskAssessment:
    """Ask the auxiliary LLM to classify the risk of ``command``.

    Args:
        command: the literal command string the agent wants to run.
            Raw — the assessor's prompt does its own quoting.
        capability_id: optional capability label (e.g.
            ``"Bash.execute"``); included in the user message for
            extra context.
        scope: optional resource scope (e.g. a path); included in the
            prompt so file-write classifications can use it.
        model: optional explicit aux model name. ``None`` uses the
            configured default via :mod:`opencomputer.agent.aux_llm`.

    Returns:
        Always returns a :class:`RiskAssessment`. Never raises.
    """
    if not command or not command.strip():
        return RiskAssessment(
            level="uncertain",
            reason="empty command — cannot assess",
            used_fallback=True,
        )

    user_msg_parts = [f"Command: {command.strip()}"]
    if capability_id:
        user_msg_parts.append(f"Capability: {capability_id}")
    if scope:
        user_msg_parts.append(f"Scope: {scope}")
    user_msg = "\n".join(user_msg_parts)

    try:
        from opencomputer.agent.aux_llm import complete_text

        # M1.3 (2026-05-09) — opt in to aux-LLM response cache.
        # Smart-mode runs at temperature=0.0 with a fixed system prompt;
        # the same (command, capability_id, scope) deterministically yields
        # the same RiskAssessment, so an agent that retries the same Bash
        # invocation 10 times pays for the LLM verdict once.
        raw = await asyncio.wait_for(
            complete_text(
                messages=[{"role": "user", "content": user_msg}],
                system=_SYSTEM_PROMPT,
                max_tokens=128,
                temperature=0.0,
                model=model,
                use_cache=True,
            ),
            timeout=_TIMEOUT_S,
        )
    except TimeoutError as e:
        logger.warning("smart-mode aux LLM timed out: %s", e)
        return RiskAssessment(
            level="uncertain",
            reason="aux LLM timed out",
            used_fallback=True,
        )
    except Exception as e:  # noqa: BLE001 — any aux-LLM failure → fallback
        logger.warning("smart-mode aux LLM unavailable: %s", e)
        return RiskAssessment(
            level="uncertain",
            reason=f"aux LLM unavailable: {type(e).__name__}",
            used_fallback=True,
        )

    return _parse_assessment(raw)


def _parse_assessment(raw: str) -> RiskAssessment:
    """Parse the LLM's response into a :class:`RiskAssessment`.

    Tolerates surrounding whitespace and accidental code-fence wrapping
    (``json``-tagged) but treats everything else as malformed →
    ``uncertain`` fallback. Pure function, exposed for testing.
    """
    if not raw:
        return RiskAssessment(
            level="uncertain", reason="empty LLM response",
            used_fallback=True,
        )
    text = raw.strip()
    # Strip ``` / ```json fences if the LLM ignored the no-markdown rule.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: try to find the first {...} block.
        m = re.search(r"\{[^{}]*\}", text)
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                return RiskAssessment(
                    level="uncertain", reason="malformed JSON",
                    used_fallback=True,
                )
        else:
            return RiskAssessment(
                level="uncertain", reason="non-JSON response",
                used_fallback=True,
            )
    if not isinstance(obj, dict):
        return RiskAssessment(
            level="uncertain", reason="response not an object",
            used_fallback=True,
        )
    level = str(obj.get("risk", "")).strip().lower()
    if level not in _VALID_LEVELS:
        return RiskAssessment(
            level="uncertain",
            reason=f"unknown risk label: {level!r}",
            used_fallback=True,
        )
    reason = str(obj.get("reason", "") or "")
    return RiskAssessment(level=level, reason=reason, used_fallback=False)


__all__ = [
    "RiskAssessment",
    "RiskLevel",
    "assess_risk",
]
