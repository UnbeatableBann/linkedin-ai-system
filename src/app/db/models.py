"""
app/db/models.py
────────────────
Pydantic models that mirror every Supabase table.
These are used for reading rows out of the DB — not for inserts
(we use dicts for inserts to avoid sending None for optional fields).
"""

from datetime import datetime, time
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


# ── Enums ──────────────────────────────────────────────────────────────────


class Channel(StrEnum):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"


class SessionState(StrEnum):
    IDLE = "idle"
    ONBOARDING = "onboarding"
    COLLECTING = "collecting"
    GENERATING = "generating"
    REVIEWING = "reviewing"
    REFINING = "refining"
    SCHEDULING = "scheduling"
    SCHEDULED = "scheduled"


class PostStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    PUBLISH = "publish"
    REMINDER = "reminder"
    EXPIRE_SESSION = "expire_session"


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GROQ = "groq"


# ── Table models ───────────────────────────────────────────────────────────


class UserRow(BaseModel):
    """Maps to the `users` table."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel: Channel
    channel_user_id: str
    zernio_api_key_enc: str | None = None
    zernio_profile_id: str | None = None
    zernio_account_id: str | None = None
    llm_provider: LLMProvider | None = None
    llm_api_key_enc: str | None = None
    llm_model: str | None = None
    timezone: str = "UTC"
    style_prefs: dict[str, Any] = {}
    is_active: bool = True
    created_at: datetime


class SessionRow(BaseModel):
    """Maps to the `sessions` table."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    state: SessionState
    context: dict[str, Any] = {}
    draft_id: UUID | None = None
    updated_at: datetime
    expires_at: datetime


class PostRow(BaseModel):
    """Maps to the `posts` table."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    content: str
    status: PostStatus
    zernio_post_id: str | None = None
    scheduled_for: datetime | None = None
    published_at: datetime | None = None
    version: int = 1
    edit_history: list[str] = []
    metadata: dict[str, Any] = {}
    created_at: datetime


class ScheduleJobRow(BaseModel):
    """Maps to the `schedule_jobs` table."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    post_id: UUID
    job_type: JobType
    run_at: datetime
    status: JobStatus
    attempts: int = 0
    last_error: str | None = None
    celery_task_id: str | None = None
    created_at: datetime


class WebhookLogRow(BaseModel):
    """Maps to the `webhook_log` table."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    idempotency_key: str
    channel: Channel
    user_channel_id: str
    payload: dict[str, Any]
    processed_at: datetime


class UserScheduleRow(BaseModel):
    """Maps to the `user_schedules` table."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    days_of_week: list[int] = [0, 4]  # Monday=0, Friday=4
    time_of_day: time
    timezone: str
    enabled: bool = False
    auto_generate: bool = False
