# ─────────────────────────────────────────────────────────────────────────────
# Multi-stage Dockerfile
#
# Stage 1 (base):  Python 3.12 slim + system deps
# Stage 2 (dev):   base + all dev tools, source mounted as volume
# Stage 3 (prod):  base + only production deps, source copied in
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS base

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# ── Dev stage ─────────────────────────────────────────────────────────────────
FROM base AS dev

# Install all dependencies including dev tools
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# Source is mounted as a volume in docker-compose (hot reload)
EXPOSE 8000

# ── Production stage ──────────────────────────────────────────────────────────
FROM base AS prod

# Copy and install only production deps
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy source
COPY app/ ./app/
COPY scripts/health_check.sh ./scripts/health_check.sh
RUN chmod +x scripts/health_check.sh

# Non-root user for security
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ./scripts/health_check.sh || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
