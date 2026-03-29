"""
app/scheduler/celery_app.py
────────────────────────────
Celery application configuration and Beat schedule.

Queues:
  generation  — LLM API calls (can be slow, isolated)
  publishing  — Zernio API calls (time-sensitive)
  default     — reminders, cleanup, misc

Beat schedule (cron jobs):
  Every 5 min  — watchdog: re-queue stale pending jobs
  Every hour   — cleanup: delete webhook_log rows older than 30 days
  Every day    — auto-schedule: queue posts for users with enabled auto-schedule
"""

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "linkedin_ai",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.scheduler.tasks"],
)

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Task routing
    task_routes={
        "app.scheduler.tasks.generate_post_task": {"queue": "generation"},
        "app.scheduler.tasks.refine_post_task": {"queue": "generation"},
        "app.scheduler.tasks.schedule_post_task": {"queue": "publishing"},
        "app.scheduler.tasks.publish_now_task": {"queue": "publishing"},
        "app.scheduler.tasks.send_reminder_task": {"queue": "default"},
        "app.scheduler.tasks.cleanup_webhook_log": {"queue": "default"},
        "app.scheduler.tasks.watchdog_stale_jobs": {"queue": "default"},
    },
    # Reliability
    task_acks_late=True,  # Don't ack until task completes (survive worker crash)
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # One task at a time per worker slot
    # Retries
    task_max_retries=3,
    task_default_retry_delay=60,  # 1 minute default retry delay
    # Results
    result_expires=86400,  # 24 hours
    # Beat schedule
    beat_schedule={
        "watchdog-stale-jobs": {
            "task": "app.scheduler.tasks.watchdog_stale_jobs",
            "schedule": crontab(minute="*/5"),  # Every 5 minutes
        },
        "cleanup-webhook-log": {
            "task": "app.scheduler.tasks.cleanup_webhook_log",
            "schedule": crontab(minute=0, hour=3),  # 3am UTC daily
        },
        "process-auto-schedules": {
            "task": "app.scheduler.tasks.process_auto_schedules",
            "schedule": crontab(minute=0, hour="*/1"),  # Every hour
        },
    },
)
