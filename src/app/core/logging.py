"""
app/core/logging.py
───────────────────
Structured JSON logging via structlog.
In development: pretty colored output.
In production: JSON lines for log aggregators.
"""

import logging
import sys

import structlog

from app.config import AppEnv, LogLevel, get_settings

LOG_LEVEL_MAP = {
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.ERROR: logging.ERROR,
}


def setup_logging() -> None:
    settings = get_settings()
    log_level = LOG_LEVEL_MAP[settings.log_level]

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if settings.app_env == AppEnv.PRODUCTION:
        processors = [
            *shared_processors,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            *shared_processors,
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Ensure stdlib logs (uvicorn, celery, etc.) go through structlog
    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        stream=sys.stdout,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)
