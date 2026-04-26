---
name: docker-workflow
description: Use when writing Dockerfiles, docker-compose, debugging container builds, image size reduction, or layer caching
---

# Docker Workflow

## When to use

- Writing a new Dockerfile
- Build is slow / image is huge
- Container behaves differently from local

## Steps

1. **Multi-stage builds.** `FROM node:20 AS builder` for the build, `FROM node:20-slim` for the runtime. Final image = 5-10× smaller.
2. **Order layers by stability.** Copy `package.json` + `lock` first → `RUN npm ci` → copy source last. Source changes don't bust the install layer.
3. **One process per container.** Don't run nginx + app in the same container. Use compose or kubernetes for multi-process needs.
4. **Don't run as root.** `USER 1000` near the bottom. Many "container escape" CVEs assume root inside.
5. **`.dockerignore` is mandatory.** `node_modules`, `.git`, `__pycache__`, secrets. Anything you wouldn't ship.
6. **Health checks.** `HEALTHCHECK CMD curl -f http://localhost:8080/healthz || exit 1`. Orchestrators rely on this.
7. **Tag with content, not "latest".** `myapp:2026.4.27` or `myapp:<git-sha>`. "latest" is undeployable.

## Notes

- BuildKit (`DOCKER_BUILDKIT=1`) is faster + has cache mounts. Always use it.
- Mount secrets via `--secret`, not `ENV`. ENV bakes into the image layer.
- `docker-compose.override.yml` for local-only tweaks; the main file stays prod-shaped.
