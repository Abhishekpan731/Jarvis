# syntax=docker/dockerfile:1
#
# Multi-stage build for the JARVIS API (apps/api).
#
# The API is part of the single-pyproject monorepo (see root pyproject.toml,
# [tool.setuptools.packages.find]) and imports from two source trees:
#   - app            (apps/api/app)      -- this service's own code
#   - jarvis_shared  (packages/shared)   -- agent registry / task state machine,
#                                          imported by app.jobs, app.execution,
#                                          app.ai.supervisor, several routers
# Both must be present for `uv sync` / the package install to succeed and for
# the API to actually import at runtime. apps/worker is not imported by the
# API and is intentionally not copied into this image.

ARG PYTHON_VERSION=3.12-slim

# ---------------------------------------------------------------------------
# Stage 1: builder - resolve and install dependencies with uv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

RUN pip install --no-cache-dir uv

WORKDIR /build

# Copy dependency manifests first for layer caching.
COPY pyproject.toml uv.lock ./

# Copy the monorepo source trees the API depends on (see note above).
COPY packages/shared ./packages/shared
COPY apps/api ./apps/api

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
    PYTHONPATH="/app/apps/api:/app/packages/shared"

COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/packages/shared /app/packages/shared
COPY --from=builder /build/apps/api /app/apps/api
# alembic.ini's script_location is ../../database/migrations relative to
# apps/api. The one-shot migrate service runs from this same image.
COPY database /app/database

USER jarvis

WORKDIR /app/apps/api

# Health endpoint is apps/api/app/main.py:/api/v1/health. It reports AI/speech
# provider configuration state and does not require a database connection, so
# it is safe to call before migrations have run.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health'%port, timeout=3).status==200 else 1)"

# Configuration is supplied entirely via environment variables, consumed by
# app.config.settings (pydantic-settings). No secrets are baked into the image.
# `python -m uvicorn`: the console script's shebang points at the builder
# path (/build/.venv/bin/python), which does not exist in this image.
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
