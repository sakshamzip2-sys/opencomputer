# OpenHub MVP — Implementation Plan

> **Status:** design complete, ready to implement
> **Last updated:** 2026-05-05
> **Owner:** archits01
> **Sibling doc:** [`social-traces-plugin.md`](./social-traces-plugin.md) — the OC plugin that talks to this network
> **Source brief:** `~/Downloads/HANDOVER.md` (read first if returning fresh)

---

## 0. Read-this-first context

If you're picking this up cold (new system, lost session, fresh Claude Code window):

1. Open `~/Downloads/HANDOVER.md` for the original product pitch.
2. Read [`social-traces-plugin.md`](./social-traces-plugin.md) for the agent-side half — this doc only makes sense alongside it.
3. Read [§4 Decision log](#4-decision-log-everything-settled-in-session-2026-05-05) below — every architectural call we've made and the reasoning. Don't re-litigate without revisiting it.
4. The next concrete action is in [§10 Implementation phases](#10-implementation-phases). Pick the next un-checked phase.

OpenHub will live in **a separate, private GitHub repo** under your account, named `openhub`. It's deliberately separate from OpenComputer — different release cadence, different deployment, eventually different contributors.

**Build the OC plugin first** against its local-file backend. That proves the agent-side flow without needing OpenHub to exist. Start OpenHub once you can demo the plugin's full prefetch+emission cycle locally.

---

## 1. What this is

OpenHub is the network half of the social-traces system. It's a small backend service:

- **HTTP API** — receives trace submissions, serves trace queries, exposes admin endpoints
- **Postgres database** — stores TraceCards, queue state, scoring metadata
- **Curation engine** — scores approved traces by `(tag_match × outcome × cost × recency)` so query responses serve the most useful traces first
- **Admin review interface** — browser UI for approving/rejecting pending submissions

Six agent-facing endpoints + four admin endpoints. ~500-1000 LOC of code total for v1.

It's a standalone product. It runs on its own host (your Mac for dev, Pi for staging, real server eventually). It has its own data, its own deploy lifecycle, its own version. Multiple OpenComputer agents (across many users) share one OpenHub instance.

## 2. The two halves of the system

```
   ┌─────────────────────────────────┐         ┌──────────────────────┐
   │   OpenComputer agents           │         │  OpenHub             │
   │   (many users, many machines)   │         │  (one shared host)   │
   │                                 │         │                      │
   │   social-traces plugin          │  HTTP   │  FastAPI             │
   │     ├── prefetch                ├────────►│    ├── /v1/traces    │
   │     ├── distill                 │         │    │    (query+sub)  │
   │     ├── redact                  │         │    └── /v1/admin/    │
   │     └── HttpTraceNetworkClient  │◄────────┤        (review)      │
   │                                 │         │                      │
   └─────────────────────────────────┘         │  Curation worker     │
                                               │  (background scoring) │
                                               │                      │
                                               │  Postgres            │
                                               │    └── trace_cards   │
                                               └──────────────────────┘
```

The boundary is a typed HTTP API. Both sides import the same `TraceCard` dataclass from `plugin_sdk.traces` (Python, shared lib). No schema drift, no translation layer.

## 3. Why this shape

Two invariants from HANDOVER.md drive everything:

1. **The network never sees raw user data.** Privacy redaction happens on the agent side, before submission. Admin review is a second line, not the first.
2. **The agent never trusts what the network sends back.** OpenHub serves structured TraceCards; the plugin reads them as REFERENCE, never executes their content.

OpenHub's job is essentially: **safely store and rank what agents submit. Help admins reject bad submissions before they enter the index.** Nothing more clever.

## 3.5 Scope — v1 vs v1.1

| Surface | Endpoint | Scope |
|---|---|---|
| Trace submission | `POST /v1/traces` | **v1** |
| Trace query (intent + tags) | `GET /v1/traces/query` | **v1** |
| Admin review | `/v1/admin/*` | **v1** |
| Health | `GET /v1/health` | **v1** |
| **Feed (broad relevance, daily poll)** | `GET /v1/feed` | **v1.1 — deferred** — see §15 |

Build v1 to support the OC plugin's mid-task and post-task surfaces. Add v1.1 feed when the plugin's morning-feed work begins (see [`social-traces-plugin.md` §13](./social-traces-plugin.md#13-v11--morning-feed-deferred)).

## 4. Decision log (everything settled in session 2026-05-05)

| Decision | Choice | Reasoning |
|---|---|---|
| Repo | Separate from OpenComputer, private under your GitHub | Different release cadence, different ops; secrecy not load-bearing but you wanted private |
| Name | OpenHub | Parallels OpenComputer naming, immediately obvious it's the paired service |
| Stack — API | FastAPI (Python) | Same language as OC; can `pip install` the shared `plugin_sdk` types directly — zero schema-drift surface |
| Stack — DB | Postgres | Tag-array matching with GIN indexes is sub-ms; full-text search built-in for `intent`; pgvector available later for embedding-based matching |
| Stack — DB host | Native install on Pi for staging; managed (Neon) for eventual production | No Docker for the Pi (overkill — one machine, one app); `apt install postgresql` |
| Stack — API host | Native systemd on Pi (24/7) | Same logic — Docker is unnecessary complexity for one Pi |
| Stack — Public exposure | ngrok (Stage 2) → real domain (Stage 3) | User has ngrok experience; Cloudflare Tunnel is a viable alternative if they want stable URLs without ngrok auth |
| Three-stage hosting roadmap | Mac local → Pi 24/7 → server | Code identical across all three; only `DATABASE_URL` and public hostname change |
| Schema stored in | `plugin_sdk/traces.py` (OC repo) | Single source of truth; OpenHub vendors or pins `opencomputer-sdk` to access it |
| Trace versioning | Implicit via curation score (no explicit `supersedes` field) | Schema stays lean; new better trace just gets a higher score |
| Tag normalization | Deferred to network side, post-MVP | Plugin emits free-form tags; OpenHub can collapse synonyms later |
| Admin auth — Stage 1 (local) | None or basic-auth | Local only |
| Admin auth — Stage 2 (Pi) | Cloudflare Access OR password header | Cheap and effective |
| Admin auth — Stage 3 (server) | Cloudflare Access magic-link or proper OIDC | TBD — defer to deploy time |
| Backups — Stage 1 | None (it's dev) | OK to lose |
| Backups — Stage 2 (Pi) | Nightly `pg_dump` to Cloudflare R2 free tier | Pi storage = SD/SSD = mortal |
| Backups — Stage 3 | Whatever managed Postgres provides | Neon does point-in-time recovery |
| Storage — Pi | Postgres data dir on attached SSD/NVMe (not SD card) | SD cards die fast under DB write loads |
| Worker — curation | Same process as API for v1 | One Pi, one app; split out only if traffic demands |
| API version constant | `v1` | Frozen until breaking change; bump for v2 |
| Submitter identity | Opaque `submitter_hash` (UUID, per-profile) | Never user identity; enables per-agent rate-limiting + future trust score |

## 5. Stack reasoning (full justification)

Why each piece, in case you want to challenge it:

### FastAPI
- Same language as OC → import shared types directly, schema drift impossible
- Auto-generated OpenAPI docs at `/docs` — free interactive admin testbed
- Async-first; mature; widely used
- Alternatives: Node/Hono (faster cold starts but duplicates schema), Go/Axum (same), Django (too heavy for 6 endpoints)

### Postgres
- Tag-match queries (`WHERE tags && $input` with GIN index) is sub-ms
- Full-text search on `intent` via `tsvector` — built in
- JSONB for flexible `steps` storage without a second store
- pgvector available later for embedding-based intent matching
- Alternatives: SQLite (breaks under concurrent writes); DynamoDB (awkward tag arrays); Mongo (weaker semantics, no upside); Pinecone/Qdrant (vector DBs — premature)

### Native install (no Docker)
- One Pi, one app, one DB — Docker is overkill
- `apt install postgresql` makes it a systemd service automatically (boots on power-up, restarts on crash, logs via `journalctl`)
- One less abstraction layer to debug
- Re-introduce Docker only if you find yourself wanting to run multiple isolated services

### Pi staging
- Free (you own the hardware)
- On-brand fit (homelab is one of the early personas)
- Single-machine ops are dead-simple at this scale
- Migrate to managed cloud when you outgrow ~1K daily-active agents

### ngrok / Cloudflare Tunnel
- Free, no payment info
- No port-forward needed (agent-initiated outbound tunnel)
- Free TLS / managed certs
- ngrok free tier needs claimed static domain for stable URLs; Cloudflare Tunnel gives stable URLs out of the box

## 6. Repository layout

```
openhub/                                    ← new private repo, your GitHub
├── pyproject.toml                          ← uv-managed project, pins opencomputer-sdk
├── README.md                               ← deploy + dev guide
├── DESIGN.md                               ← link to this doc
├── .env.example                            ← all env vars, no real secrets
├── .gitignore                              ← .venv, .env, *.db, etc.
│
├── openhub/                                ← package
│   ├── __init__.py                         ← __version__
│   ├── api.py                              ← FastAPI app, route registration
│   ├── routes/
│   │   ├── traces.py                       ← /v1/traces — query + submit
│   │   ├── admin.py                        ← /v1/admin/* — review queue
│   │   └── health.py                       ← /v1/health
│   ├── models.py                           ← SQLModel/Pydantic ORM models
│   ├── schemas.py                          ← request/response shapes (re-export TraceCard)
│   ├── db.py                               ← async Postgres pool + session
│   ├── curation.py                         ← scoring engine
│   ├── tag_index.py                        ← tag normalization (post-MVP stub)
│   ├── auth.py                             ← admin auth (basic-auth → CF Access)
│   ├── ratelimit.py                        ← per-IP + per-submitter_hash limits
│   ├── validators.py                       ← TraceCard schema validation
│   ├── settings.py                         ← env-driven config (DATABASE_URL, etc.)
│   └── templates/
│       ├── admin_index.html                ← review queue UI
│       └── admin_trace.html                ← single-trace review page
│
├── migrations/                             ← alembic
│   └── versions/
│       └── 0001_initial.py                 ← schema bootstrap
│
├── scripts/
│   ├── backup.sh                           ← pg_dump → R2 (cron'd on Pi)
│   ├── restore.sh                          ← restore from snapshot
│   └── seed_dev_traces.py                  ← seed test data for local dev
│
├── tests/
│   ├── test_api.py
│   ├── test_curation.py
│   ├── test_admin.py
│   └── test_validators.py
│
└── deploy/
    ├── systemd/
    │   ├── openhub-api.service             ← uvicorn
    │   └── openhub-cloudflared.service     ← tunnel daemon (or ngrok)
    └── README.md                           ← Pi deployment runbook
```

## 7. Data model

### 7.1 `trace_cards` — the main table

```sql
CREATE TABLE trace_cards (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schema_version  text NOT NULL,                    -- "v1"
    status          text NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | superseded
    intent          text NOT NULL,
    tags            text[] NOT NULL,
    steps           jsonb NOT NULL,                   -- list of TraceStep
    distilled_insight text NOT NULL,
    outcome         text NOT NULL,                    -- success | partial | failed
    token_cost      integer NOT NULL,
    loop_count      integer NOT NULL,
    harness_version text NOT NULL,
    submitter_hash  text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    reviewed_at     timestamptz,
    review_reason   text,
    score           real NOT NULL DEFAULT 0.0
);

CREATE INDEX idx_trace_tags ON trace_cards USING GIN (tags);
CREATE INDEX idx_trace_intent_fts ON trace_cards
    USING GIN (to_tsvector('english', intent));
CREATE INDEX idx_trace_status_score ON trace_cards (status, score DESC);
CREATE INDEX idx_trace_submitter ON trace_cards (submitter_hash);
CREATE INDEX idx_trace_created ON trace_cards (created_at DESC);
```

### 7.2 `submission_log` — full submissions including rejected (for ML training signal)

```sql
CREATE TABLE submission_log (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_card_id   uuid REFERENCES trace_cards(id) ON DELETE CASCADE,
    raw_payload     jsonb NOT NULL,                   -- the original POST body
    received_at     timestamptz NOT NULL DEFAULT now(),
    submitter_hash  text NOT NULL,
    submitter_ip    inet,
    decision        text,                             -- approved | rejected
    decision_reason text,
    decided_at      timestamptz
);
```

### 7.3 `rate_limit_buckets` — sliding window counters

```sql
CREATE TABLE rate_limit_buckets (
    id              text PRIMARY KEY,                 -- "submitter:<hash>" or "ip:<addr>"
    window_start    timestamptz NOT NULL,
    request_count   integer NOT NULL DEFAULT 0
);
```

(Or use Redis if you don't want this in Postgres. For MVP, Postgres is fine.)

## 8. API surface

### 8.1 Agent-facing (public, rate-limited)

```
POST /v1/traces
  Body: TraceCard (no id, no status — server-assigned)
  Headers:
    X-Submitter-Hash: <opaque agent id>
    X-Harness-Version: opencomputer/0.1.0
  Returns: 202 Accepted
    { "queue_id": "<uuid>", "accepted": true }
  Or: 400 Bad Request
    { "accepted": false, "reason": "<validation error>" }
  Or: 429 Too Many Requests

GET /v1/traces/query?intent=...&tags=tag1,tag2&limit=3
  Returns: 200 OK
    {
      "query_id": "<uuid>",
      "served_from": "network",
      "traces": [<TraceCard>, ...]   // status=approved only, scored desc
    }

GET /v1/health
  Returns: 200 OK
    { "ok": true, "version": "0.1.0" }

GET /v1/feed?tags=tag1,tag2&since=<iso8601>&limit=10            [DEFERRED — v1.1]
  Headers: X-Submitter-Hash: <opaque agent id>
  Broad relevance query for the morning-feed surface. Returns traces ranked by
  (tag_overlap × score × recency) and not previously shown to this submitter_hash.
  Distinct from /v1/traces/query because the query is the agent's tag profile,
  not a specific intent.
  See §15 for full design.
```

### 8.2 Admin (gated)

```
GET /v1/admin/pending
  Returns paginated list of submissions awaiting review
  Renders HTML when Accept: text/html, JSON otherwise

GET /v1/admin/traces/{id}
  Single submission detail page

POST /v1/admin/traces/{id}/approve
  Body: { "reason": "<optional note>" }
  Returns: 200 OK

POST /v1/admin/traces/{id}/reject
  Body: { "reason": "<required>" }
  Returns: 200 OK

GET /v1/admin/stats
  Returns: counts by status, tags, submitter, etc.
```

### 8.3 Validation rules (server-side)

Every submitted TraceCard must:

- Have all required fields populated
- `schema_version == "v1"`
- `intent` length: 10-500 chars
- `distilled_insight` length: 20-2000 chars
- `tags`: 1-10 entries, each 2-30 chars, lowercase, alphanumeric + hyphen
- `steps`: 1-50 entries, each step's summaries < 500 chars
- `submitter_hash`: 32-64 hex chars
- Total payload size: < 50 KB

Reject anything that fails these — return reason in the response. This is the validation layer; admin review is the second filter.

## 9. Curation engine

### 9.1 Scoring formula (v1)

When a trace transitions to `approved`, compute and store:

```
score = (
    tag_match_weight * 1.0           # set at query time, not stored
  + outcome_weight                   # success=1.0, partial=0.5, failed=0.1
  + cost_score                       # 1 / log(token_cost + 1)
  + recency_decay                    # exp(-(age_days / 30))
)
```

Stored on the row at approval time. Recency_decay is recomputed by the worker daily (or on every query — cheap).

### 9.2 Query path

```sql
SELECT *
FROM trace_cards
WHERE status = 'approved'
  AND tags && $input_tags          -- GIN-indexed array overlap
ORDER BY
  cardinality(tags & $input_tags) DESC,   -- more matching tags = better
  score DESC
LIMIT $limit;
```

The plugin's `prefetch.score_traces()` then applies its own relevance threshold on top — server gives candidates, client decides which to use.

### 9.3 Re-scoring

Worker job (cron, every 6 hours):

- Recompute `recency_decay` for all approved traces
- Update `score` column

For v1, this is a single SQL UPDATE statement. No need for a separate worker process.

## 10. Implementation phases

### Phase 0 — Repo bootstrap (1 hour)

- [ ] Create private repo `openhub` on your GitHub
- [ ] `uv init`, set up `pyproject.toml` with FastAPI, SQLAlchemy/SQLModel, Alembic, asyncpg, httpx, pydantic
- [ ] Pin `opencomputer-sdk` (or vendor `plugin_sdk/traces.py` for now until OC ships SDK as a separate package)
- [ ] Write minimal `README.md` linking back to this doc
- [ ] `.env.example` with `DATABASE_URL`, `ADMIN_PASSWORD`, `BIND_HOST`, `BIND_PORT`

### Phase 1 — Database + migrations (1-2 hours)

- [ ] Local Postgres on Mac via Homebrew: `brew install postgresql@16 && brew services start postgresql@16`
- [ ] `createdb openhub_dev`
- [ ] `openhub/db.py` — async pool, session dependency for FastAPI
- [ ] `openhub/models.py` — SQLModel for `TraceCard`, `SubmissionLog`
- [ ] Alembic init + first migration (the three tables from §7)
- [ ] `alembic upgrade head` succeeds against local DB

### Phase 2 — Submit endpoint (2-3 hours)

- [ ] `openhub/validators.py` — TraceCard schema validation rules
- [ ] `POST /v1/traces` accepts payload, validates, writes to `submission_log` + `trace_cards` (status=pending)
- [ ] Returns `SubmitReceipt` with queue_id
- [ ] Rate limiting (FastAPI middleware, sliding window in `rate_limit_buckets`)
- [ ] Tests: valid submission accepted; invalid rejected with reason; rate-limit triggers after N

### Phase 3 — Query endpoint (2-3 hours)

- [ ] `GET /v1/traces/query` — parse query string, run SQL from §9.2
- [ ] Return `QueryResult` with top-K
- [ ] Tests: tag overlap returns expected; `status='pending'` excluded; ordering correct

### Phase 4 — Health + skeleton admin (1-2 hours)

- [ ] `GET /v1/health` — returns version + DB connectivity check
- [ ] `GET /v1/admin/pending` — basic JSON listing
- [ ] `auth.py` — basic-auth middleware reading `ADMIN_PASSWORD` from env
- [ ] Tests: admin endpoints 401 without auth, 200 with

### Phase 5 — Admin UI (3-4 hours)

- [ ] Jinja2 templates in `openhub/templates/`
- [ ] `admin_index.html` — pending queue list with intent + tags + submit time
- [ ] `admin_trace.html` — single trace detail page with full TraceCard, approve/reject buttons
- [ ] POST endpoints `approve` and `reject` with redirect after action
- [ ] Tailwind via CDN (no build step) — keep it simple
- [ ] Tests: approve flow updates status; reject requires reason

### Phase 6 — Curation scoring (2-3 hours)

- [ ] `curation.py:compute_score()` — implements §9.1 formula
- [ ] Trigger score computation in approve handler
- [ ] Daily re-score worker as a cron job calling `python -m openhub.scripts.rescore`
- [ ] Tests: score reflects all four weight components

### Phase 7 — End-to-end demo on local Mac (1-2 hours)

- [ ] Start OpenHub: `uv run uvicorn openhub.api:app --reload`
- [ ] Start two OC profiles (alice, bob) with `social-traces` enabled, backend=http, endpoint=localhost:8000
- [ ] Walk-through:
  1. alice asks for novel task → no trace returned → explores → emits → POST hits OpenHub
  2. Open `localhost:8000/v1/admin/pending` → approve
  3. bob asks similar task → query returns alice's trace → bob uses it silently
- [ ] Document the demo in `README.md`

### Phase 8 — Pi deployment (Stage 2) (3-4 hours)

- [ ] On Pi: `apt install postgresql postgresql-contrib`
- [ ] `createuser openhub --pwprompt && createdb -O openhub openhub`
- [ ] Mount SSD/NVMe at `/mnt/ssd`, move Postgres data dir to `/mnt/ssd/pgdata` (edit `postgresql.conf`, restart service)
- [ ] Clone `openhub` repo to `/home/pi/openhub`
- [ ] `uv sync --frozen`
- [ ] Set up `.env` with production-tier `ADMIN_PASSWORD`
- [ ] Install systemd unit (deploy/systemd/openhub-api.service)
- [ ] `systemctl enable --now openhub-api`
- [ ] Install ngrok or cloudflared as systemd service, configure stable hostname
- [ ] Sanity test from another machine: hit `https://yourname.ngrok.app/v1/health`

### Phase 9 — Backups (1-2 hours)

- [ ] Cloudflare R2 bucket for backups (free tier 10GB)
- [ ] `scripts/backup.sh` — `pg_dump | gzip | rclone copy`
- [ ] Cron entry (1am daily): `0 1 * * * /home/pi/openhub/scripts/backup.sh`
- [ ] Test restore: `pg_restore` to a scratch DB on the Pi
- [ ] Document the runbook in `deploy/README.md`

### Phase 10 — Stage 3 cutover (when needed, post-MVP)

- [ ] Sign up for Neon (or chosen managed Postgres)
- [ ] Migrate data: `pg_dump` from Pi → `pg_restore` to Neon
- [ ] Update `DATABASE_URL` env var
- [ ] (Optional) move API to a small VPS or keep on Pi pointing at remote DB
- [ ] Update plugin config for users to point at the new domain

### Phase 11 — Feed endpoint (DEFERRED to v1.1)

Implement `GET /v1/feed` once the plugin's morning-feed work begins. Full design in §15.

- [ ] Add migration: `feed_views` table tracking `(submitter_hash, trace_id, viewed_at)` for dedup
- [ ] `routes/feed.py` — query handler with tag-overlap + score + recency ranking, dedup against `feed_views`
- [ ] Insert into `feed_views` on each response so the same agent doesn't see duplicates
- [ ] Tests: dedup works across calls; ranking respects all three components; quiet response on empty tag profile

## 11. Tests

- **Validation** — round-trip valid TraceCard; reject malformed (missing field, bad tags, oversize)
- **Submit** — pending row appears in DB; submission_log captures raw payload
- **Query** — tag overlap, top-K ordering, status filter
- **Admin auth** — 401 unauthenticated, 200 authenticated
- **Approve** — status transitions, score computed
- **Reject** — status transitions, reason required
- **Rate limit** — N+1th request returns 429
- **Health** — DB-down case returns 503

Use `httpx.AsyncClient` against `app` for integration tests; SQLite in tests is fine for unit, real Postgres in CI for integration.

## 12. Operations

### 12.1 Three-stage hosting roadmap

| Stage | Where | DATABASE_URL | Public URL | Backups |
|---|---|---|---|---|
| 1. Local dev | Mac (Homebrew Postgres + uvicorn) | `postgres://localhost/openhub_dev` | `http://localhost:8000` | None |
| 2. Pi staging | Raspberry Pi (24/7, native install) | `postgres://localhost/openhub` (on Pi) | `https://yourname.ngrok.app` | Nightly pg_dump → R2 |
| 3. Production | VPS or managed | `postgres://neon/openhub_prod` | `https://openhub.<your-domain>` | Managed PITR |

**The code is identical across all three.** Only env vars change.

### 12.2 Stage 1 setup script (Mac)

```bash
brew install postgresql@16
brew services start postgresql@16
createdb openhub_dev

git clone git@github.com:<you>/openhub.git
cd openhub
uv sync
cp .env.example .env
# edit .env: DATABASE_URL, ADMIN_PASSWORD

uv run alembic upgrade head
uv run uvicorn openhub.api:app --reload
# OpenHub now live at http://localhost:8000
# Admin at http://localhost:8000/v1/admin (basic-auth)
```

### 12.3 Stage 2 setup runbook (Pi)

See `deploy/README.md` in the openhub repo. Skeleton:

1. `apt install postgresql postgresql-contrib`
2. Move Postgres data dir to attached SSD (edit `postgresql.conf`, restart)
3. Create user/db: `sudo -u postgres createuser openhub --pwprompt && sudo -u postgres createdb -O openhub openhub`
4. Clone repo, `uv sync`, edit `.env`
5. `uv run alembic upgrade head`
6. Install + enable systemd units (`openhub-api.service`, `openhub-cloudflared.service` or ngrok equivalent)
7. Test from another machine
8. Set up nightly backup cron (`scripts/backup.sh` to R2)

### 12.4 Monitoring

For MVP: `journalctl -u openhub-api -f` and Postgres logs are enough. When you have real users, add:

- Prometheus metrics endpoint at `/metrics` (FastAPI middleware)
- Uptime ping from a third party (Better Uptime free tier)
- Daily summary email (count of new submissions, approval rate)

## 13. Security posture

| Risk | Mitigation |
|---|---|
| Spam submissions flood admin queue | Rate limit per submitter_hash + per IP; reject above N/min |
| Malicious agent submits prompt-injection payload in `distilled_insight` | (a) Plugin redacts before sending; (b) Admin reviews before approval; (c) Plugin reads as REFERENCE only on consumption |
| User identity leaks via `submitter_hash` | Hash is opaque, randomly generated per-profile, stored only on the agent — server never sees user identity |
| DB compromise | Backups, principle of least privilege on the openhub DB user, network access locked to localhost (Postgres listens on 127.0.0.1 only) |
| Admin endpoint exposure | Cloudflare Access or strong basic-auth password; never expose `/admin` publicly without auth |
| TLS missing | Cloudflare Tunnel / ngrok provide free TLS; Pi never exposes raw HTTP to the internet |
| SQL injection | SQLModel parameterizes by default; never construct raw SQL with f-strings |
| Payload bombs | 50KB cap at validation; reject before parsing |

## 14. Open questions (still TBD)

| # | Question | When to settle |
|---|---|---|
| 1 | When to migrate from Pi to managed cloud (which signal triggers the move?) | Stage 3 — when query latency creeps or storage pressure shows |
| 2 | Federate by domain (separate networks per `#homelab` / `#coding`) or single shared network? | Post-MVP — let usage shape the answer |
| 3 | Embedding-based intent matching (pgvector) — when worth adding? | When tag-match recall feels low (qualitative — wait for real usage) |
| 4 | Trust score / Phase-2 progressive auto-approval | After ~50 admin-reviewed traces give us training signal |
| 5 | Anomaly detection for Phase-3 fully-automated review | After Phase 2 is shipping |
| 6 | Should `submitter_hash` rotate periodically for anti-correlation? | Privacy review pre-launch |
| 7 | Soft delete vs hard delete on reject? Currently hard — keep submission_log for training | Confirm before launch |
| 8 | Public read access (anyone can `query` without auth) vs gated? | Default: open. Revisit if abuse |

## 15. v1.1 — Feed endpoint (deferred)

The morning-feed surface from the plugin (see [`social-traces-plugin.md` §13](./social-traces-plugin.md#13-v11--morning-feed-deferred)) needs a server-side endpoint distinct from `/v1/traces/query`.

### Why distinct from `/v1/traces/query`

| Aspect | `/v1/traces/query` | `/v1/feed` |
|---|---|---|
| Trigger | Mid-task, user just typed a request | Daily background poll, no user prompt |
| Query | Specific intent + narrow tags | Agent's accumulated tag profile (broad) |
| Goal | "Find me 1-3 traces that closely match" | "Show me N interesting things I haven't seen" |
| Dedup | None (re-fetching is fine) | Required (don't show same trace twice) |
| Ranking | Tag-match × outcome × cost | Tag-overlap × score × recency |

Different ranking, different dedup semantics, different request shape. Sharing endpoint code would muddy both.

### Endpoint sketch

```
GET /v1/feed?tags=tag1,tag2&since=<iso8601>&limit=10
  Headers: X-Submitter-Hash: <opaque agent id>
  Returns:
    {
      "items": [<TraceCard>, ...],   # ranked, not previously shown to this submitter
      "served_at": "2026-05-05T08:00:00Z",
      "next_poll_after": "2026-05-06T08:00:00Z"
    }
```

### New table

```sql
CREATE TABLE feed_views (
    submitter_hash  text NOT NULL,
    trace_card_id   uuid NOT NULL REFERENCES trace_cards(id) ON DELETE CASCADE,
    viewed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (submitter_hash, trace_card_id)
);

CREATE INDEX idx_feed_views_submitter ON feed_views (submitter_hash);
```

### Privacy consideration

Storing `(submitter_hash, trace_card_id)` is more state than v1 keeps. The hash is opaque (never user identity), but per-agent view history is correlatable. Three alternatives if correlation worries you in v1.1:

1. **Client-side dedup** — agent maintains its own seen-set, sends `?exclude=id1,id2,...` on each poll. Server stores nothing. Pro: zero new state on server. Con: query gets long once agent has seen >100 traces.
2. **Bloom filter from agent** — agent sends a compact bloom filter representing its seen set. Server filters server-side without storing. Mid-complexity.
3. **Server stores, deletes after 90 days** — simplest, with a retention cap.

Defer this decision until you build it.

### Scoring formula (v1.1)

```
feed_score = (
    tag_overlap_count * 2.0           # how many of my tags match
  + score                              # base curation score (set at approval)
  + (-0.1 * days_since_created)        # recency bias, decay
)
```

Different formula than `/v1/traces/query` — feed wants recent + diverse, query wants exact intent match.

### When to build

When the plugin starts on its Phase 13 (morning feed). Coordinate with the plugin author (probably you) to spec the API contract, then build server-side first so the plugin has something to call.

## 16. Glossary

- **TraceCard** — the structured wire format. Frozen schema in `plugin_sdk/traces.py`. Both halves of the system serialize against this.
- **Pending / approved / rejected / superseded** — lifecycle states for a submission.
- **submitter_hash** — opaque per-agent stable id. Never user identity.
- **Curation engine** — server-side scorer (this repo). Consumes admin-approved traces, computes `score`, serves top-K to query callers.
- **Stage 1 / 2 / 3** — local Mac → Pi+ngrok → real server.
- **Soft timeout** — agent-side query timeout (1s) above which the agent treats the response as empty and proceeds to explore.

---

## Appendix A — Cross-system pickup checklist

If you're returning to this from a different machine:

1. Clone both repos:
   ```bash
   git clone git@github.com:<you>/openhub.git
   git clone https://github.com/<your-fork>/opencomputer.git
   ```
2. Read this doc top-to-bottom
3. Read `~/Downloads/HANDOVER.md` (or wherever you saved it on the new system)
4. Read [`social-traces-plugin.md`](./social-traces-plugin.md)
5. Check both repos' `git log --oneline` — what's already done?
6. Cross-reference completed commits against [§10 Implementation phases](#10-implementation-phases) checkboxes
7. Pick the next un-checked phase

## Appendix B — Useful one-liners

```bash
# Stage 1 — start everything for local dev
brew services start postgresql@16
cd ~/openhub && uv run uvicorn openhub.api:app --reload &
cd ~/opencomputer && uv run opencomputer -p alice

# Stage 2 — Pi service status
ssh pi 'systemctl status openhub-api postgresql cloudflared'
ssh pi 'journalctl -u openhub-api -f'

# Manual backup right now
ssh pi 'bash ~/openhub/scripts/backup.sh'

# Approve a pending trace via API (instead of admin UI)
curl -u admin:$ADMIN_PASSWORD -X POST \
  https://yourname.ngrok.app/v1/admin/traces/<id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"reason": "looks clean"}'

# Quick sanity check from anywhere
curl https://yourname.ngrok.app/v1/health
```
