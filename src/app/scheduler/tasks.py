"""
app/scheduler/tasks.py
───────────────────────
All Celery task definitions.

Tasks:
  generate_post_task    — Call LLM to write a post draft
  refine_post_task      — Call LLM to refine an existing draft
  schedule_post_task    — Call Zernio to publish at scheduled time
  publish_now_task      — Call Zernio to publish immediately
  send_reminder_task    — Send 1-hour-before reminder to user
  watchdog_stale_jobs   — Re-queue any pending jobs past their run_at
  cleanup_webhook_log   — Delete old webhook_log rows
  process_auto_schedules — Trigger auto-schedule for eligible users
"""

import asyncio
import atexit
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from celery import Task
from celery.utils.log import get_task_logger

from app.scheduler.celery_app import celery_app

logger = get_task_logger(__name__)

_worker_loop: asyncio.AbstractEventLoop | None = None


def _get_worker_loop() -> asyncio.AbstractEventLoop:
    """Return a process-local loop reused across sync Celery task invocations."""
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
    return _worker_loop


def _shutdown_worker_loop() -> None:
    """Close the shared worker loop cleanly when the worker process exits."""
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        return

    pending = asyncio.all_tasks(_worker_loop)
    for task in pending:
        task.cancel()
    if pending:
        _worker_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    _worker_loop.run_until_complete(_worker_loop.shutdown_asyncgens())
    _worker_loop.close()
    _worker_loop = None


atexit.register(_shutdown_worker_loop)


def run_async(coro):
    """Run an async coroutine from a synchronous Celery task."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("run_async() cannot be called from an active event loop")

    loop = _get_worker_loop()
    return loop.run_until_complete(coro)


# ── Content Generation ─────────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    queue="generation",
    max_retries=2,
    default_retry_delay=30,
    name="app.scheduler.tasks.generate_post_task",
)
def generate_post_task(
    self: Task,
    user_id: str,
    session_id: str,
    channel: str,
    channel_user_id: str,
    topic: str | None,
    tone: str | None,
    audience: str | None,
    length_pref: str | None,
    style_prefs: dict[str, Any],
) -> None:
    """
    Generate a LinkedIn post draft using the user's configured LLM.
    On completion, updates the session and sends the draft to the user.
    """
    run_async(
        _generate_post_async(
            user_id,
            session_id,
            channel,
            channel_user_id,
            topic,
            tone,
            audience,
            length_pref,
            style_prefs,
            self,
        )
    )


async def _generate_post_async(
    user_id: str,
    session_id: str,
    channel: str,
    channel_user_id: str,
    topic: str | None,
    tone: str | None,
    audience: str | None,
    length_pref: str | None,
    style_prefs: dict[str, Any],
    task: Task,
) -> None:
    from app.content.generator import generate_post
    from app.conversation.reviewing import present_draft
    from app.db.client import get_db
    from app.db.models import UserRow
    from app.session.fsm import Event, transition
    from app.session.models import SessionState
    from app.session.store import get_or_create_session, save_session

    db = await get_db()
    sender = _get_sender(channel)

    try:
        # Load user and session
        user_row = await db.table("users").select("*").eq("id", user_id).single().execute()
        user = UserRow(**user_row.data)
        session = await get_or_create_session(UUID(user_id))

        # Generate the post
        content = await generate_post(
            user=user,
            topic=topic,
            tone=tone,
            audience=audience,
            length_pref=length_pref,
            style_prefs=style_prefs,
        )

        if not content:
            raise ValueError("LLM returned empty content")

        # Update session with draft
        session.context.draft_content = content
        session.context.draft_version += 1
        session.state = transition(SessionState.GENERATING, Event.DRAFT_READY)
        await save_session(session)

        # Send draft to user
        from app.conversation.reviewing import present_draft

        await sender.send_text(
            channel_user_id, present_draft(content, session.context.draft_version)
        )

        # Process any pending message that arrived during generation
        if session.context.pending_message:
            pending = session.context.pending_message
            session.context.pending_message = None
            await save_session(session)
            logger.info("tasks.generate.processing_pending_message", user_id=user_id)
            # Dispatch the queued message through reviewing
            from app.channels.base import MessageType, NormalisedMessage
            from app.conversation.reviewing import handle_reviewing

            pending_msg = NormalisedMessage(
                channel=channel,
                channel_user_id=channel_user_id,
                message_id=f"pending_{session_id}",
                text=pending,
                message_type=MessageType.TEXT,
            )
            await handle_reviewing(session, user, sender, pending_msg)
            await save_session(session)

    except Exception as exc:
        logger.error("tasks.generate.failed", user_id=user_id, error=str(exc))
        try:
            # Retry if we have retries left
            raise task.retry(exc=exc, countdown=30)
        except task.MaxRetriesExceededError:
            # All retries exhausted — tell the user
            session = await get_or_create_session(UUID(user_id))
            session.state = transition(SessionState.GENERATING, Event.GENERATION_FAILED)
            session.context.clear_draft()
            await save_session(session)
            await sender.send_text(
                channel_user_id,
                "❌ I had trouble generating your post. This might be an API issue.\n\n"
                "Please try again, or check your LLM API key in /settings.",
            )


# ── Refinement ─────────────────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    queue="generation",
    max_retries=2,
    default_retry_delay=30,
    name="app.scheduler.tasks.refine_post_task",
)
def refine_post_task(
    self: Task,
    user_id: str,
    session_id: str,
    channel: str,
    channel_user_id: str,
    current_draft: str,
    edit_instruction: str,
    style_prefs: dict[str, Any],
) -> None:
    run_async(
        _refine_post_async(
            user_id,
            session_id,
            channel,
            channel_user_id,
            current_draft,
            edit_instruction,
            style_prefs,
            self,
        )
    )


async def _refine_post_async(
    user_id: str,
    session_id: str,
    channel: str,
    channel_user_id: str,
    current_draft: str,
    edit_instruction: str,
    style_prefs: dict[str, Any],
    task: Task,
) -> None:
    from app.content.refiner import refine_post
    from app.conversation.reviewing import present_draft
    from app.db.client import get_db
    from app.db.models import UserRow
    from app.session.fsm import Event, transition
    from app.session.models import SessionState
    from app.session.store import get_or_create_session, save_session

    db = await get_db()
    sender = _get_sender(channel)

    try:
        user_row = await db.table("users").select("*").eq("id", user_id).single().execute()
        user = UserRow(**user_row.data)
        session = await get_or_create_session(UUID(user_id))

        refined = await refine_post(
            user=user,
            current_draft=current_draft,
            edit_instruction=edit_instruction,
            style_prefs=style_prefs,
        )

        session.context.draft_content = refined
        session.context.draft_version += 1
        session.state = transition(SessionState.REFINING, Event.REFINEMENT_READY)

        # Process pending message if any
        pending = session.context.pending_message
        session.context.pending_message = None
        await save_session(session)

        await sender.send_text(
            channel_user_id, present_draft(refined, session.context.draft_version)
        )

        if pending:
            from app.channels.base import MessageType, NormalisedMessage
            from app.conversation.reviewing import handle_reviewing

            pending_msg = NormalisedMessage(
                channel=channel,
                channel_user_id=channel_user_id,
                message_id=f"pending_refine_{session_id}",
                text=pending,
                message_type=MessageType.TEXT,
            )
            await handle_reviewing(session, user, sender, pending_msg)
            await save_session(session)

    except Exception as exc:
        logger.error("tasks.refine.failed", user_id=user_id, error=str(exc))
        try:
            raise task.retry(exc=exc, countdown=30)
        except task.MaxRetriesExceededError:
            session = await get_or_create_session(UUID(user_id))
            session.state = transition(SessionState.REFINING, Event.REFINEMENT_FAILED)
            await save_session(session)
            await sender.send_text(
                channel_user_id,
                "❌ Refinement failed. Your previous draft is still saved.\n\n"
                "Try a different edit instruction or /cancel to start over.",
            )


# ── Publishing ─────────────────────────────────────────────────────────────


@celery_app.task(
    bind=True,
    queue="publishing",
    max_retries=3,
    name="app.scheduler.tasks.schedule_post_task",
)
def schedule_post_task(
    self: Task,
    user_id: str,
    post_id: str,
    channel: str,
    channel_user_id: str,
) -> None:
    run_async(_publish_scheduled_async(user_id, post_id, channel, channel_user_id, self))


async def _publish_scheduled_async(
    user_id: str,
    post_id: str,
    channel: str,
    channel_user_id: str,
    task: Task,
) -> None:
    from app.core.encryption import decrypt
    from app.db.client import get_db
    from app.db.models import UserRow
    from app.zernio.client import ZernioClient, ZernioError

    db = await get_db()
    sender = _get_sender(channel)

    try:
        # Load user and post
        user_row = await db.table("users").select("*").eq("id", user_id).single().execute()
        user = UserRow(**user_row.data)
        post_row = await db.table("posts").select("*").eq("id", post_id).single().execute()
        post = post_row.data

        if post["status"] in ("published", "cancelled"):
            logger.info("tasks.publish.skip", post_id=post_id, status=post["status"])
            return

        # Mark as running
        await db.table("posts").update({"status": "scheduled"}).eq("id", post_id).execute()

        # Check LinkedIn token health before attempting publish
        from app.zernio.health import assert_account_healthy

        try:
            await assert_account_healthy(user)
        except ZernioError as health_exc:
            await sender.send_text(channel_user_id, str(health_exc))
            await db.table("posts").update({"status": "failed"}).eq("id", post_id).execute()
            return

        api_key = decrypt(user.zernio_api_key_enc)
        client = ZernioClient(api_key=api_key)

        # Publish via Zernio
        scheduled_for = post.get("scheduled_for", "")
        zernio_post = await client.schedule_post(
            content=post["content"],
            account_id=user.zernio_account_id,
            scheduled_for=scheduled_for[:19] if scheduled_for else "",
            timezone=user.timezone,
        )

        # Update post with Zernio ID and published status
        await (
            db.table("posts")
            .update(
                {
                    "status": "published",
                    "zernio_post_id": zernio_post.id,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", post_id)
            .execute()
        )

        await sender.send_text(
            channel_user_id,
            f"✅ *Published!* Your LinkedIn post is now live.\n\n" f"_{post['content'][:80]}..._",
        )

    except Exception as exc:
        logger.error("tasks.publish.failed", post_id=post_id, error=str(exc))
        attempts = task.request.retries + 1
        delays = [60, 300, 900]  # 1min, 5min, 15min
        if attempts <= len(delays):
            raise task.retry(exc=exc, countdown=delays[attempts - 1])
        else:
            db = await get_db()
            await (
                db.table("posts")
                .update({"status": "failed", "metadata": {"last_error": str(exc)[:500]}})
                .eq("id", post_id)
                .execute()
            )
            sender_obj = _get_sender(channel)
            await sender_obj.send_text(
                channel_user_id,
                f"❌ Failed to publish your post after 3 attempts.\n\n"
                f"Error: {str(exc)[:200]}\n\n"
                "Please check your LinkedIn connection with /reconnect and try rescheduling.",
            )


@celery_app.task(
    bind=True,
    queue="publishing",
    max_retries=2,
    name="app.scheduler.tasks.publish_now_task",
)
def publish_now_task(
    self: Task,
    user_id: str,
    session_id: str,
    channel: str,
    channel_user_id: str,
    content: str,
) -> None:
    run_async(_publish_now_async(user_id, session_id, channel, channel_user_id, content, self))


async def _publish_now_async(
    user_id: str,
    session_id: str,
    channel: str,
    channel_user_id: str,
    content: str,
    task: Task,
) -> None:
    from app.core.encryption import decrypt
    from app.db.client import get_db
    from app.db.models import UserRow
    from app.zernio.client import ZernioClient, ZernioError

    db = await get_db()
    sender = _get_sender(channel)

    try:
        user_row = await db.table("users").select("*").eq("id", user_id).single().execute()
        user = UserRow(**user_row.data)

        api_key = decrypt(user.zernio_api_key_enc)
        client = ZernioClient(api_key=api_key)

        # Save post first
        post_result = (
            await db.table("posts")
            .insert(
                {
                    "user_id": user_id,
                    "content": content,
                    "status": "approved",
                }
            )
            .execute()
        )
        if not post_result or not post_result.data:
            raise RuntimeError("Failed to save post record before publishing")
        post_id = post_result.data[0]["id"]

        zernio_post = await client.publish_now(content=content, account_id=user.zernio_account_id)

        await (
            db.table("posts")
            .update(
                {
                    "status": "published",
                    "zernio_post_id": zernio_post.id,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", post_id)
            .execute()
        )

        await sender.send_text(
            channel_user_id,
            "✅ *Published!* Your post is live on LinkedIn.",
        )

    except Exception as exc:
        logger.error("tasks.publish_now.failed", user_id=user_id, error=str(exc))
        await sender.send_text(
            channel_user_id,
            f"❌ Couldn't publish: {str(exc)[:200]}\n\nPlease try again or check /settings.",
        )


# ── Reminders & Maintenance ─────────────────────────────────────────────────


@celery_app.task(queue="default", name="app.scheduler.tasks.send_reminder_task")
def send_reminder_task(user_id: str, post_id: str, channel: str, channel_user_id: str) -> None:
    run_async(_send_reminder_async(user_id, post_id, channel, channel_user_id))


async def _send_reminder_async(
    user_id: str, post_id: str, channel: str, channel_user_id: str
) -> None:
    from app.db.client import get_db

    db = await get_db()
    sender = _get_sender(channel)

    post_row = (
        await db.table("posts")
        .select("content, scheduled_for, status")
        .eq("id", post_id)
        .single()
        .execute()
    )
    post = post_row.data

    if post["status"] in ("cancelled", "published", "failed"):
        return  # Post was already handled

    scheduled = post.get("scheduled_for", "")[:16]
    preview = post["content"][:100]

    await sender.send_text(
        channel_user_id,
        f"⏰ *Reminder:* Your post publishes in 1 hour!\n\n"
        f"Scheduled: `{scheduled}`\n\n"
        f"_{preview}..._\n\n"
        f"To cancel: /schedule cancel {post_id[:8]}",
    )


@celery_app.task(queue="default", name="app.scheduler.tasks.watchdog_stale_jobs")
def watchdog_stale_jobs() -> None:
    """Re-queue any schedule_jobs that are past their run_at but still pending."""
    run_async(_watchdog_stale_jobs_async())


async def _watchdog_stale_jobs_async() -> None:
    from app.db.client import get_db

    db = await get_db()
    now = datetime.now(timezone.utc)

    stale = await (
        db.table("schedule_jobs")
        .select("id, user_id, post_id, attempts")
        .eq("status", "pending")
        .lt("run_at", now.isoformat())
        .execute()
    )

    for job in stale.data:
        # Fetch channel info from users table so we can notify them
        user_result = await (
            db.table("users")
            .select("channel, channel_user_id")
            .eq("id", job["user_id"])
            .maybe_single()
            .execute()
        )
        if not user_result or not user_result.data:
            logger.warning("watchdog.user_not_found", job_id=job["id"])
            continue

        channel = user_result.data["channel"]
        channel_user_id = user_result.data["channel_user_id"]

        logger.warning(
            "watchdog.stale_job",
            job_id=job["id"],
            run_at=job.get("run_at"),
            attempts=job.get("attempts", 0),
        )

        schedule_post_task.apply_async(
            kwargs={
                "user_id": job["user_id"],
                "post_id": job["post_id"],
                "channel": channel,
                "channel_user_id": channel_user_id,
            }
        )


@celery_app.task(queue="default", name="app.scheduler.tasks.cleanup_webhook_log")
def cleanup_webhook_log() -> None:
    """Delete webhook_log rows older than 30 days to keep the table lean."""
    run_async(_cleanup_webhook_log_async())


async def _cleanup_webhook_log_async() -> None:
    from app.db.client import get_db

    db = await get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    await db.table("webhook_log").delete().lt("processed_at", cutoff).execute()
    logger.info("cleanup.webhook_log.done", cutoff=cutoff)


@celery_app.task(queue="default", name="app.scheduler.tasks.process_auto_schedules")
def process_auto_schedules() -> None:
    """
    Celery Beat task — runs every hour.

    For each user with auto-schedule enabled, checks if there is a
    Mon/Fri slot coming up in the next 25 hours with no post already
    scheduled. If so, checks if the user has a queued draft or sends
    them a reminder to write a post.
    """
    run_async(_process_auto_schedules_async())


async def _process_auto_schedules_async() -> None:
    from datetime import datetime, timedelta, timezone

    import pytz

    from app.db.client import get_db

    db = await get_db()
    now = datetime.now(timezone.utc)
    lookahead = now + timedelta(hours=25)

    # Find all users with auto-schedule enabled
    result = await (
        db.table("user_schedules")
        .select("user_id, days_of_week, time_of_day, timezone, auto_generate")
        .eq("enabled", True)
        .execute()
    )

    for sched in result.data if result and result.data else []:
        try:
            await _check_user_auto_schedule(db, sched, now, lookahead)
        except Exception as exc:
            logger.error(
                "tasks.auto_schedule.user_error",
                user_id=sched.get("user_id"),
                error=str(exc),
            )


async def _check_user_auto_schedule(
    db: object,
    sched: dict,
    now: "datetime",
    lookahead: "datetime",
) -> None:
    """Check one user's auto-schedule and queue a post if a slot is coming up."""
    from datetime import datetime, timedelta, timezone

    import pytz

    user_id = sched["user_id"]
    days_of_week = sched.get("days_of_week", [0, 4])
    time_str = sched.get("time_of_day", "09:00:00")
    tz_name = sched.get("timezone", "UTC")

    try:
        user_tz = pytz.timezone(tz_name)
    except Exception:
        user_tz = pytz.UTC

    # Parse the time
    hour, minute = (int(x) for x in time_str[:5].split(":"))

    # Find slot(s) in the lookahead window
    now_local = datetime.now(user_tz)
    slots_in_window = []

    for days_ahead in range(2):  # Check today and tomorrow
        candidate = now_local + timedelta(days=days_ahead)
        if candidate.weekday() in days_of_week:
            slot = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
            slot_utc = slot.astimezone(timezone.utc)
            if now < slot_utc <= lookahead:
                slots_in_window.append(slot_utc)

    if not slots_in_window:
        return

    for slot_utc in slots_in_window:
        # Check if a post is already scheduled for this slot (±1 hour window)
        window_start = (slot_utc - timedelta(hours=1)).isoformat()
        window_end = (slot_utc + timedelta(hours=1)).isoformat()
        existing = await (
            db.table("posts")
            .select("id")
            .eq("user_id", user_id)
            .in_("status", ["scheduled", "approved"])
            .gte("scheduled_for", window_start)
            .lte("scheduled_for", window_end)
            .execute()
        )
        if existing.data:
            logger.debug(
                "tasks.auto_schedule.slot_taken",
                user_id=user_id,
                slot=slot_utc.isoformat(),
            )
            continue

        # Slot is free — remind the user to write a post
        await _send_auto_schedule_nudge(db, user_id, slot_utc, user_tz)


async def _send_auto_schedule_nudge(
    db: object,
    user_id: str,
    slot_utc: "datetime",
    user_tz: object,
) -> None:
    """Send a nudge to a user reminding them to write a post for an upcoming slot."""
    from datetime import datetime

    user_result = (
        await db.table("users")
        .select("channel, channel_user_id, is_active")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    if not user_result or not user_result.data or not user_result.data.get("is_active"):
        return

    channel = user_result.data["channel"]
    channel_user_id = user_result.data["channel_user_id"]
    sender = _get_sender(channel)

    local_str = slot_utc.astimezone(user_tz).strftime("%A %d %b at %I:%M %p %Z")

    await sender.send_text(
        channel_user_id,
        f"📅 *Auto-schedule reminder*\n\n"
        f"You have an open slot: *{local_str}*\n\n"
        "Send me a topic or idea and I'll write a post for it! "
        "Or say /schedule auto to turn off auto-reminders.",
    )
    logger.info(
        "tasks.auto_schedule.nudge_sent",
        user_id=user_id,
        slot=slot_utc.isoformat(),
    )


@celery_app.task(queue="default", name="app.scheduler.tasks.update_style_memory_task")
def update_style_memory_task(user_id: str, content: str) -> None:
    """Update user style memory after a post is approved. Fire-and-forget."""
    run_async(_update_style_memory_async(user_id, content))


async def _update_style_memory_async(user_id: str, content: str) -> None:
    try:
        from app.content.style_memory import update_style_from_post

        await update_style_from_post(user_id, content)
    except Exception as exc:
        logger.warning("tasks.style_memory.failed", user_id=user_id, error=str(exc))


# ── Helpers ────────────────────────────────────────────────────────────────


def _get_sender(channel: str):
    if channel == "telegram":
        from app.channels.telegram import TelegramSender

        return TelegramSender()
    elif channel == "whatsapp":
        from app.channels.whatsapp import WhatsAppSender

        return WhatsAppSender()
    raise ValueError(f"Unknown channel: {channel}")
