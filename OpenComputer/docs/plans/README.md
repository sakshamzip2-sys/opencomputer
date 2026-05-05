# Plans

Living design docs for in-flight work. Each doc is self-contained — readable cold without other context — and tracks decisions, open questions, and phase-by-phase implementation steps.

## Active plans

### Social Traces — collective agent knowledge network

A two-part system: a bundled OpenComputer plugin that lets agents query and emit structured traces, paired with **OpenHub**, a separate network service where traces are curated and reviewed.

- [`social-traces-plugin.md`](./social-traces-plugin.md) — the OC plugin half (lives in `extensions/social-traces/`)
- [`openhub-mvp.md`](./openhub-mvp.md) — the network half (separate `openhub` repo)
- Source brief: `~/Downloads/HANDOVER.md`

**Status:** design complete (2026-05-05), ready to implement. Build the plugin first against its local-file backend; OpenHub second.

## How to use these docs

Each plan has the same shape:
- §0 read-this-first context (so a fresh session can pick up cold)
- §1-3 what + why
- §4 decision log with reasoning (don't re-litigate without revisiting)
- §5-9 architecture and data
- §10 numbered, checkbox'd implementation phases (this is the actionable bit)
- Appendices: glossary, cross-system pickup checklist

When you finish a phase, tick the checkbox in the doc and commit. The doc + git log together are the persistent state of the project.
