"""Prompt-evolution proposals — diff-only, never auto-apply.

When a reflection produces an Insight with action_type=="edit_prompt", we
DO NOT mutate the live system prompt. Instead, we persist the proposal to
the prompt_proposals table + write a sidecar diff file under
<evolution_home>/prompt_proposals/<id>.diff. The user reviews via
`opencomputer evolution prompts list` and decides via
`prompts apply <id>` (writes a backup, then applies) or `prompts reject <id>`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencomputer.evolution.reflect import Insight
from opencomputer.evolution.storage import (
    evolution_home,
    init_db,
    list_prompt_proposals,
    record_prompt_proposal,
    update_prompt_proposal_status,
)


@dataclass(frozen=True, slots=True)
class PromptProposal:
    id: int
    proposed_at: float
    target: str             # "system" | "tool_spec"
    diff_hint: str
    insight: Insight
    status: str             # "pending" | "applied" | "rejected"
    decided_at: float | None = None
    decided_reason: str | None = None


class PromptEvolver:
    """Persists Insight->proposal; reads/updates proposal status. Pure persistence,
    no LLM calls, no prompt mutations.
    """

    def __init__(self, *, dest_dir: Path | None = None) -> None:
        self._dest_dir = dest_dir

    def _resolve_dest_dir(self) -> Path:
        if self._dest_dir is not None:
            return self._dest_dir
        return evolution_home() / "prompt_proposals"

    def propose(self, insight: Insight) -> PromptProposal:
        """Persist an edit_prompt Insight as a pending proposal.

        Returns the persisted PromptProposal (with id assigned). Writes:
          - row in `prompt_proposals` table
          - sidecar `<dest_dir>/<id>.diff` containing the diff_hint text
            (atomic: tmp + os.replace)
        """
        if insight.action_type != "edit_prompt":
            raise ValueError(
                f"PromptEvolver.propose requires action_type='edit_prompt', got {insight.action_type!r}"
            )
        payload = dict(insight.payload)
        target = str(payload.get("target", ""))
        if target not in {"system", "tool_spec"}:
            raise ValueError(f"payload.target must be 'system' or 'tool_spec', got {target!r}")
        diff_hint = str(payload.get("diff_hint", "")).strip()
        if not diff_hint:
            raise ValueError("payload.diff_hint must be a non-empty string")

        # Ensure DB is initialised before writing proposals
        init_db()

        # Persist row + capture id
        proposal_id = record_prompt_proposal(
            target=target,
            diff_hint=diff_hint,
            insight_json=_insight_to_json(insight),
            proposed_at=time.time(),
        )

        # Sidecar atomic write
        dest = self._resolve_dest_dir()
        dest.mkdir(parents=True, exist_ok=True)
        sidecar = dest / f"{proposal_id}.diff"
        tmp = sidecar.with_suffix(".diff.tmp")
        tmp.write_text(diff_hint, encoding="utf-8")
        os.replace(tmp, sidecar)

        return self.get(proposal_id)

    def list_pending(self) -> list[PromptProposal]:
        return [self._row_to_proposal(r) for r in list_prompt_proposals(status="pending")]

    def list_all(self) -> list[PromptProposal]:
        return [self._row_to_proposal(r) for r in list_prompt_proposals()]

    def get(self, proposal_id: int) -> PromptProposal:
        rows = list_prompt_proposals()
        for r in rows:
            if r["id"] == proposal_id:
                return self._row_to_proposal(r)
        raise KeyError(f"No prompt proposal with id={proposal_id}")

    def apply(self, proposal_id: int, *, reason: str = "") -> PromptProposal:
        """Mark a proposal as applied. Caller is responsible for the actual
        prompt-file edit (we only persist the decision; no auto-mutation of files).
        """
        update_prompt_proposal_status(
            proposal_id=proposal_id, status="applied", reason=reason or "manually applied"
        )
        return self.get(proposal_id)

    def reject(self, proposal_id: int, *, reason: str = "") -> PromptProposal:
        update_prompt_proposal_status(
            proposal_id=proposal_id, status="rejected", reason=reason or "manually rejected"
        )
        return self.get(proposal_id)

    @staticmethod
    def _row_to_proposal(row: Any) -> PromptProposal:
        insight = _insight_from_json(row["insight_json"])
        return PromptProposal(
            id=row["id"],
            proposed_at=row["proposed_at"],
            target=row["target"],
            diff_hint=row["diff_hint"],
            insight=insight,
            status=row["status"],
            decided_at=row["decided_at"],
            decided_reason=row["decided_reason"],
        )


def _insight_to_json(insight: Insight) -> str:
    return json.dumps({
        "observation": insight.observation,
        "evidence_refs": list(insight.evidence_refs),
        "action_type": insight.action_type,
        "payload": dict(insight.payload),
        "confidence": insight.confidence,
    })


def _insight_from_json(raw: str) -> Insight:
    d = json.loads(raw)
    return Insight(
        observation=d["observation"],
        evidence_refs=tuple(int(x) for x in d.get("evidence_refs", [])),
        action_type=d["action_type"],
        payload=dict(d.get("payload", {})),
        confidence=float(d["confidence"]),
    )


__all__ = [
    "PromptProposal",
    "PromptEvolver",
    "_insight_to_json",
    "_insight_from_json",
]
