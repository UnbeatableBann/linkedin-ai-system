"""
app/main.py
───────────
FastAPI application factory.

Startup sequence:
  1. Validate all environment variables (Pydantic Settings fails fast)
  2. Configure structured logging
  3. Register all API routers
  4. Log readiness

The Celery worker and Beat scheduler run as separate processes (see docker-compose.yml).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.logging import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown logic."""
    settings = get_settings()
    logger = get_logger(__name__)

    logger.info(
        "app.startup",
        env=settings.app_env,
        oauth_callback_url=settings.oauth_callback_url,
    )

    yield

    logger.info("app.shutdown")


def create_app() -> FastAPI:
    # Setup logging first so all subsequent imports get the right config
    setup_logging()

    settings = get_settings()

    app = FastAPI(
        title="LinkedIn AI Content System",
        description="AI-powered LinkedIn post generation and scheduling via Telegram and WhatsApp",
        version="1.0.0",
        # Disable docs in production (no sensitive route exposure)
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # ── CORS (only needed if you add a web UI later) ─────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_production else [],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    from app.api.health import router as health_router
    from app.api.oauth_callback import router as oauth_router
    from app.api.webhook_telegram import router as telegram_router
    from app.api.webhook_whatsapp import router as whatsapp_router
    from app.api.webhook_zernio import router as zernio_router

    app.include_router(health_router, tags=["health"])
    app.include_router(telegram_router, tags=["telegram"])
    app.include_router(whatsapp_router, tags=["whatsapp"])
    app.include_router(zernio_router, tags=["zernio"])
    app.include_router(oauth_router, tags=["oauth"])

    # ── Scalar docs ───────────────────────────────────────────────────────────────
    if not settings.is_production:

        @app.get("/scalar", include_in_schema=False)
        async def scalar_docs():
            from scalar_fastapi import get_scalar_api_reference

            return get_scalar_api_reference(
                openapi_url=app.openapi_url,
                title=app.title,
            )

    return app


app = create_app()
