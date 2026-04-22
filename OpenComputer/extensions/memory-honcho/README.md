# memory-honcho — OpenComputer plugin for self-hosted Honcho

Deep cross-session user understanding as an overlay on OpenComputer's
baseline memory (MEMORY.md + USER.md + FTS5 session search).

**Status:** Phase 10f.K skeleton — `register(api)` is a stub.
`HonchoSelfHostedProvider` + docker-compose bootstrap land in 10f.L
and 10f.M.

## What it provides (once fully wired)

- Persistent, cross-session *user model* via Honcho's dialectic reasoning.
- Five agent-facing tools: `honcho_profile`, `honcho_search`, `honcho_context`,
  `honcho_reasoning`, `honcho_conclude`.
- Per-profile isolation (each OpenComputer profile gets its own Honcho
  AI peer via host key `opencomputer.<profile>`, wired in Phase 14.J).

## Licensing

**OpenComputer does NOT redistribute Honcho's source code.**

[Honcho](https://github.com/plastic-labs/honcho) is [AGPL-3.0](https://github.com/plastic-labs/honcho/blob/main/LICENSE)
and maintained by [Plastic Labs](https://plasticlabs.ai). This plugin
orchestrates the upstream Docker image at runtime — users pull the
image from Plastic Labs' registry on first use, at which point they
accept Honcho's AGPL terms for the service running on their machine.

OpenComputer's plugin glue (this directory, minus the pulled image) is
under OpenComputer's existing license.

## Prerequisites

- **Docker** + **docker compose** (v2 syntax). macOS/Linux: install
  [Docker Desktop](https://www.docker.com/products/docker-desktop/) or
  [Colima](https://github.com/abiosoft/colima). Linux users who already
  have `docker` + `docker-compose-plugin` packages are set.
- **Disk:** ~1.5 GB for the Honcho + Postgres + Redis images. ~500 MB
  additional for your accumulated data.
- **RAM:** ~1 GB free (the compose bundle sets a 1 GB mem limit; adjust
  in `docker-compose.yml` for low-memory machines).

## Usage (preview — full flow ships in 10f.M / 10f.N)

```bash
opencomputer memory setup     # bring up the Honcho stack in Docker
opencomputer memory status    # check container health + provider state
opencomputer memory reset     # wipe Honcho data (confirms first)
```

During the first-run wizard (Phase 10f.N), users are asked whether to
enable Honcho. Opting out keeps the zero-dependency built-in memory
fully functional.

## Pinning

The Docker image tag this plugin targets lives in `IMAGE_VERSION` (one
line, no whitespace). Current:

```
latest
```

**Before 10f.M ships, this MUST be pinned to a specific upstream release**
(e.g. `v0.3.1`) after verifying:
1. The upstream image registry + exact image name.
2. Our integration tests (Phase 10f.L + 10f.M) pass against it.
3. The tag is a stable/release ref, not a moving branch.

## What this plugin deliberately does NOT do

- **Ship Honcho source code.** Avoid AGPL copyleft propagation.
- **Ship the Docker image.** Users pull from the upstream registry so
  they're accepting AGPL terms directly from Plastic Labs.
- **Auto-enable on install.** The first-run wizard asks explicit consent.
- **Block the agent on Honcho availability.** If Docker is missing or
  Honcho is down, the bridge disables the provider for the session and
  the built-in memory keeps working.

## Links

- Upstream repo: https://github.com/plastic-labs/honcho
- Plastic Labs: https://plasticlabs.ai
- Phase plan: `/Users/saksham/.claude/plans/in-this-applicaiton-i-zesty-pebble.md`
  (Phases 10f.K / 10f.L / 10f.M / 10f.N)
