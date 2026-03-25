"""
app/api/health.py
─────────────────
Health check endpoint for Railway/Render uptime monitoring.
Also checks Supabase and Redis connectivity.
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from app.core.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/health")
async def health_check() -> dict:
    """
    Lightweight health check. Returns:
      - status: ok | degraded
      - timestamp
      - service connectivity checks
    """
    checks: dict[str, str] = {}

    # ── Supabase ─────────────────────────────────────────────────────────────
    try:
        from app.db.client import get_db
        db = get_db()
        db.table("users").select("id").limit(1).execute()
        checks["supabase"] = "ok"
    except Exception as exc:
        logger.error("health.supabase_failed", error=str(exc))
        checks["supabase"] = f"error: {str(exc)[:80]}"

    # ── Redis ─────────────────────────────────────────────────────────────────
    try:
        import redis as redis_lib

        from app.config import get_settings
        settings = get_settings()
        r = redis_lib.from_url(settings.redis_url, socket_connect_timeout=2)
        r.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.error("health.redis_failed", error=str(exc))
        checks["redis"] = f"error: {str(exc)[:80]}"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"

    return {
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }
