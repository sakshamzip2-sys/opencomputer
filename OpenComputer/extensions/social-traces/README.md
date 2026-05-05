# social-traces

Bundled OpenComputer plugin. Lets the agent participate in a collective
trace network — query for similar tasks before exploring, contribute
distilled traces of novel work back to the network for other agents to
reuse.

**Status:** Phase 2 — scaffold only. Hook is wired, real query/inject
and emission paths land in Phases 4-9. See
`docs/plans/social-traces-plugin.md` for the full roadmap.

## What it does (when complete)

1. **Pre-task lookup.** When you type a request, the plugin extracts
   `(intent, tags)` and asks the network whether anyone has solved
   something similar. If a good trace comes back, it gets injected into
   the agent's context — your agent doesn't have to figure it out from
   scratch.
2. **Post-task emission.** When your agent figures out something new
   (no existing trace matched, or the LLM judge thinks you improved on
   what was there), it distills the session into a structured TraceCard
   and submits it to the network. Privacy-redacted client-side; admin-
   reviewed network-side before it ever shows up in another agent's
   feed.

The network half is **OpenHub** — a separate service. See
`docs/plans/openhub-mvp.md`.

## Privacy

* User identity NEVER leaves the device. Submissions carry an opaque
  per-profile id (random, regeneratable by deleting `<profile_home>/
  traces/agent_id`).
* PII / paths / hostnames / secrets are scrubbed client-side BEFORE
  submission. Admin review on the network is a second filter, not the
  first.
* Tags are abstract — `#homelab`, `#filesync`, etc. — never raw user
  data.

## Enabling

The plugin ships **disabled** in two layers. Both must be flipped:

```bash
# Layer 1: load the plugin into your profile
opencomputer plugin enable social-traces

# Layer 2: turn the feature on
oc traces enable
```

Inspect the state:

```bash
oc traces status
```

Disable:

```bash
oc traces disable
# (or `oc plugin disable social-traces` to unload the plugin entirely)
```

## Configuration

Add to `~/.opencomputer/<profile>/config.yaml`:

```yaml
social_traces:
  backend: local              # 'local' (dev stub) or 'http' (OpenHub)
  endpoint: http://localhost:8000   # only for backend=http

  privacy:
    redact_paths: true
    redact_hostnames: true

  novelty_judge:
    enabled: true             # rule (d): when a trace was used, judge novelty
    cost_guard_usd_per_session: 0.05

  query:
    soft_timeout_s: 1.0       # fall through to explore if network slow
    top_k: 3
    relevance_threshold: 0.6
```

All keys are optional — defaults match the example above.

## Layout

```
extensions/social-traces/
├── plugin.json           ← manifest
├── plugin.py             ← register(api): hooks + subscriber
├── README.md             ← this file
├── config.py             ← SocialTracesConfig dataclass
├── identity.py           ← per-profile submitter_hash
├── state.py              ← on-disk enabled flag + heartbeat
├── prefetch.py           ← Phase 2 stub for BEFORE_TASK handler
├── subscriber.py         ← Phase 2 stub for SessionEndEvent subscriber
└── (Phase 3+ adds: client/, redactor.py, distiller.py, novelty_judge.py,
   tag_extractor.py, outbox.py, cache.py)
```

## Status of each surface

| Surface | Phase | Status |
|---|---|---|
| Plugin scaffold | 2 | ✅ this scaffold |
| BEFORE_TASK hook registration | 2 | ✅ stub (returns pass) |
| Local-file backend (`LocalFileTraceNetworkClient`) | 3 | pending |
| Pre-task query/inject | 4 | pending |
| Post-task subscriber pipeline | 5 | pending |
| Novelty judge (rule d) | 6 | pending |
| Redactor + distiller | 7 | pending |
| LLM tag extractor | 8 | pending |
| HTTP client + outbox | 9 | pending |
| Morning feed | 13 (v1.1) | deferred |
