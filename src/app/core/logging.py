"""
app/core/logging.py
───────────────────
Structured JSON logging via structlog.
In development: pretty colored output.
In production: JSON lines for log aggregators (Railway, Datadog, etc).
"""

import logging
import sys

import structlog

from app.config import AppEnv, get_settings


def setup_logging() -> None:
    settings = get_settings()

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if settings.app_env == AppEnv.PRODUCTION:
        # JSON output for production log aggregators
        processors = [
            *shared_processors,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Human-readable colored output for development
        processors = [
            *shared_processors,
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Also configure stdlib logging so uvicorn/celery logs go through structlog
    logging.basicConfig(
        format="%(message)s",
        level=logging.getLevelName(settings.log_level),
        stream=sys.stdout,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)
