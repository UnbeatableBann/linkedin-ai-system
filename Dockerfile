# ─────────────────────────────────────────────────────────────────────────────
# Multi-stage Dockerfile
#
# Stage 1 (base):  Python 3.12 slim + system deps
# Stage 2 (dev):   base + all dev tools, source mounted as volume
# Stage 3 (prod):  base + only production deps, source copied in
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Use an in-project virtual environment for predictable runtime commands.
ENV UV_PROJECT_ENVIRONMENT=.venv

# Copy dependency files FIRST (for caching)
COPY pyproject.toml uv.lock ./

# ── Dev stage ─────────────────────────────────────────────────────────────────
FROM base AS dev

# Install all dependencies including dev/docs
RUN uv sync --frozen --all-extras --no-install-project --link-mode=copy

# Source is mounted as a volume in docker-compose (hot reload)
EXPOSE 8000

# ── Production stage ──────────────────────────────────────────────────────────
FROM base AS prod

# Copy and install only production deps (no dev or docs extras)
RUN uv sync --frozen --no-dev --no-install-project --link-mode=copy

# Copy source
COPY src/ ./src/
COPY scripts/health_check.sh ./scripts/health_check.sh
RUN chmod +x scripts/health_check.sh

# Non-root user for security
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# HEALTHCHECK --interval=40s --timeout=10s --start-period=15s --retries=3 \
#     CMD ./scripts/health_check.sh || exit 1

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
