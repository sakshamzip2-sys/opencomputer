# OpenComputer Dockerfile — multi-stage build for cron / gateway / webhook deployment.
#
# Use case: run OpenComputer 24/7 on a VPS so cron jobs (e.g., 8:30 AM daily
# stock briefing) and webhook listeners (TradingView alerts) keep firing
# even when your laptop is asleep. ~$5/mo VPS handles this comfortably.
#
# Build:
#     docker build -t opencomputer:dev .
#
# Run gateway daemon (recommended — covers Telegram + Discord + cron):
#     docker run -d --name oc-gateway \
#         -v oc-data:/home/oc/.opencomputer \
#         -e ANTHROPIC_API_KEY=... \
#         -e TELEGRAM_BOT_TOKEN=... \
#         -p 18790:18790 \
#         opencomputer:dev opencomputer gateway
#
# Run cron daemon only (no channels):
#     docker run -d --name oc-cron \
#         -v oc-data:/home/oc/.opencomputer \
#         opencomputer:dev opencomputer cron daemon
#
# See docker-compose.yml for a full local-dev stack.
#
# Image layout:
#   /home/oc/                        non-root user $HOME
#     .opencomputer/                 mounted as named volume → persists config + sessions + cron
#     opencomputer/                  source (read-only at runtime)
#       opencomputer/                Python package
#       extensions/                  bundled plugins (telegram, discord, webhook, …)
#       plugin_sdk/                  SDK boundary

# =============================================================================
# Stage 1: builder — install deps + opencomputer in editable mode
# =============================================================================
FROM python:3.13-slim AS builder

# System deps for compiled wheels (cryptography, aiohttp, croniter)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/build

# Install the package + all runtime deps into a venv we'll copy across.
# Using a venv keeps the runtime image tidy and lets us drop the build tools.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only the metadata first so dep resolution is layer-cached separately
# from source changes.
COPY pyproject.toml README.md ./
COPY opencomputer/ opencomputer/
COPY plugin_sdk/ plugin_sdk/
COPY extensions/ extensions/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# =============================================================================
# Stage 2: runtime — slim image, non-root user, only what's needed at run time
# =============================================================================
FROM python:3.13-slim AS runtime

# Runtime deps only:
# - ca-certificates: HTTPS to providers (Anthropic, OpenAI) + Telegram Bot API
# - tini: PID 1 init so signals propagate cleanly when running as PID 1
# - libsqlite3-0: SQLite (sessions DB + consent audit log + cron jobs storage)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 oc \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash oc

WORKDIR /home/oc/opencomputer

# Copy the prebuilt venv from the builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Copy source for editable-install to resolve. Owned by oc so the user can
# write entries to ~/.opencomputer/ at runtime (mounted volume).
COPY --chown=oc:oc opencomputer/ opencomputer/
COPY --chown=oc:oc plugin_sdk/ plugin_sdk/
COPY --chown=oc:oc extensions/ extensions/
COPY --chown=oc:oc pyproject.toml README.md ./

USER oc

# Persistent state lives here — mount this as a named volume so config /
# sessions / cron jobs / consent audit chain survive container restarts.
ENV OPENCOMPUTER_HOME=/home/oc/.opencomputer
RUN mkdir -p $OPENCOMPUTER_HOME

# Webhook listener default port (extensions/webhook/adapter.py).
EXPOSE 18790

# tini → opencomputer ensures Ctrl-C / docker stop deliver SIGTERM correctly.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["opencomputer", "doctor"]
