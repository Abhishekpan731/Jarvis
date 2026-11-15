# syntax=docker/dockerfile:1
#
# Multi-stage build for the independent JARVIS worker (apps/worker).
#
# The worker is part of the single-pyproject monorepo (see root pyproject.toml,
# [tool.setuptools.packages.find]) and imports from three source trees:
#   - jarvis_worker  (apps/worker)       -- this service's own code
#   - app             (apps/api/app)     -- loop.py/health.py import app.config,
#                                          app.db, app.briefing, app.execution,
#                                          app.integrations.runner, app.jobs,
#                                          app.models, app.observe
#   - jarvis_shared   (packages/shared)  -- transitively used by app.*
# All three must be present for `uv sync` / the package install to succeed and
# for the worker to actually import at runtime.

ARG PYTHON_VERSION=3.12-slim

# ---------------------------------------------------------------------------
# Stage 1: builder - resolve and install dependencies with uv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

RUN pip install --no-cache-dir uv

WORKDIR /build

# Copy dependency manifests first for layer caching.
COPY pyproject.toml uv.lock ./

# Copy the monorepo source trees the worker depends on (see note above).
COPY packages/shared ./packages/shared
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker

# Install dependencies only. [tool.uv] package = false, so a second `uv sync`
# without --no-install-project tries to install the project and fails.
RUN uv sync --frozen --no-dev --no-install-project --python-preference only-system

# ---------------------------------------------------------------------------
# Stage 2: runtime - minimal image, non-root user
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS runtime

RUN groupadd --system jarvis && useradd --system --gid jarvis --no-create-home jarvis

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/apps/worker:/app/apps/api:/app/packages/shared"

COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/packages/shared /app/packages/shared
COPY --from=builder /build/apps/api /app/apps/api
COPY --from=builder /build/apps/worker /app/apps/worker

USER jarvis

# No HTTP server/API surface on this service. The worker exposes a local,
# loopback-only health endpoint (apps/worker/jarvis_worker/health.py) bound
# to 127.0.0.1:${WORKER_HEALTH_PORT} (default 8091) that reports process
# status, DB reachability, and queue depth. Use it instead of a generic
# process-liveness check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request,sys; port=os.environ.get('WORKER_HEALTH_PORT','8091'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/'%port, timeout=3).status==200 else 1)"

# Configuration is supplied entirely via environment variables, consumed by
# app.config.settings (pydantic-settings). No secrets are baked into the image.
CMD ["python", "-m", "jarvis_worker"]
