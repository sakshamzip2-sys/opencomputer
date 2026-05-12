# `oc workspace` — Senior-Engineer Workflow Phases

Companion to the design spec. Captures the brainstorm / audit / plan / audit-plan
walkthrough requested by Saksham 2026-05-12.

## Brainstorm — 8 approaches scored

(see chat transcript; winning approach is "OpenAI-compat shim in dashboard").

## Audit-design — 9 lens findings

Changes vs. original spec:
* Drop `oc workspace install` subcommand (YAGNI).
* Document "Sessions/Skills tabs will be empty" honestly in CLI startup banner.
* Per-profile AgentLoop cache → v2 followup (flagged in route handler with file:line TODO).

## Plan + audit-plan revisions

* M2 re-sized L (bundles 5 modules ~1500 LOC).
* Corrupt-install detection: check `node_modules/.modules.yaml` presence.
* Dashboard port-collision: refuse to reuse existing dashboard — always spawn
  our own in-process dashboard on chosen port. Fail-loud on conflict.
* Integration test gated by `@pytest.mark.integration` marker.

## Execution order

M1 (openai_compat) ✅ done → M2 (launcher) → M3 (CLI) → M4 (tests) → M5 (docs+PR)
