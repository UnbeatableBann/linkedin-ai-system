"""
app/conversation/scheduling.py
────────────────────────────────
Handles the SCHEDULING state and /schedule command variants.

Features:
  - Natural language date/time parsing ("next Monday 9am", "Friday", "tomorrow")
  - Timezone-aware scheduling using the user's stored timezone
  - Conflict detection (±30 min window)
  - Auto Mon/Fri schedule suggestion when no time given
  - Two-step confirm: parse → show → user confirms → enqueue
"""

from datetime import UTC, datetime, timedelta

import dateparser
import pytz

from app.channels.base import NormalisedMessage
from app.core.logging import get_logger
from app.db.client import get_db
from app.db.models import UserRow
from app.session.fsm import Event, transition
from app.session.models import SessionState
from app.session.store import Session

logger = get_logger(__name__)

# Phrases that mean "yes, confirm this time slot"
CONFIRM_PHRASES = {
    "yes",
    "confirm",
    "ok",
    "okay",
    "go",
    "sure",
    "sounds good",
    "that works",
    "perfect",
    "yep",
    "yup",
    "✅",
}


async def handle_scheduling(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """
    Handle user input while in SCHEDULING state.

    Two sub-phases:
      1. No proposed_schedule_time yet → parse the user's input
      2. proposed_schedule_time exists → user is confirming or rejecting
    """
    text = msg.text.strip()
    text_lower = text.lower()

    # ── Phase 2: User is confirming a proposed time ──────────────────────────
    if session.context.proposed_schedule_time:
        if text_lower in CONFIRM_PHRASES or text_lower.startswith("yes"):
            await _confirm_and_enqueue(session, user, sender, msg)
            return
        elif text_lower in ("no", "nope", "cancel", "change", "different"):
            session.context.proposed_schedule_time = None
            await ask_for_schedule_time(sender, msg.channel_user_id)
            return
        else:
            # Treat it as a new time expression
            session.context.proposed_schedule_time = None
            # Fall through to phase 1 parsing

    # ── Phase 1: Parse the time expression ──────────────────────────────────
    user_tz = pytz.timezone(user.timezone)
    parsed_dt = _parse_datetime(text, user_tz)

    if parsed_dt is None:
        await sender.send_text(
            msg.channel_user_id,
            "I couldn't understand that time. Please try a format like:\n\n"
            "• `next Monday 9am`\n"
            "• `Friday at 2pm`\n"
            "• `tomorrow morning`\n"
            "• `7 Apr at 9am`\n\n"
            "Or say *auto* to use the next available Mon/Fri 9am slot.",
        )
        return

    # Must be in the future (at least 5 minutes from now)
    now_utc = datetime.now(UTC)
    if parsed_dt < now_utc + timedelta(minutes=5):
        next_slot = _next_auto_slot(user_tz)
        await sender.send_text(
            msg.channel_user_id,
            "That time has already passed ⏰\n\n"
            f"The next available auto-slot is *{_format_dt(next_slot, user_tz)}*.\n\n"
            "Use that, or send another time:",
        )
        return

    # Check for conflicts with other scheduled posts
    conflict = await _check_conflict(str(user.id), parsed_dt)
    if conflict:
        next_slot = _suggest_next_slot(parsed_dt, user_tz)
        await sender.send_text(
            msg.channel_user_id,
            f"⚠️ You already have a post scheduled around that time.\n\n"
            f"Suggested alternative: *{_format_dt(next_slot, user_tz)}*\n\n"
            "Use that, or send a different time:",
        )
        return

    # Propose the parsed time for confirmation
    session.context.proposed_schedule_time = parsed_dt.isoformat()
    local_str = _format_dt(parsed_dt, user_tz)

    await sender.send_text(
        msg.channel_user_id,
        f"Schedule for *{local_str}*?\n\n" "Reply *yes* to confirm or send a different time.",
    )


async def ask_for_schedule_time(sender: object, channel_user_id: str) -> None:
    """Prompt the user for when to schedule the post."""
    await sender.send_text(
        channel_user_id,
        "When should this go live? 📅\n\n"
        "• `next Monday 9am`\n"
        "• `Friday at 2pm`\n"
        "• `tomorrow`\n"
        "• *auto* — next available Mon/Fri slot\n"
        "• *now* — publish immediately",
    )


async def handle_schedule_command(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """/schedule command handler — supports subcommands."""
    arg = (msg.command_args or "").strip().lower()

    if arg == "list":
        await _handle_list(sender, msg.channel_user_id, str(user.id))
    elif arg == "auto":
        await _handle_toggle_auto(sender, msg.channel_user_id, str(user.id))
    elif arg.startswith("cancel "):
        post_id = arg.split(" ", 1)[1].strip()
        await _handle_cancel_post(sender, msg.channel_user_id, str(user.id), post_id)
    elif not arg:
        # /schedule with no arg — go to scheduling flow if there's an active draft
        if session.context.draft_content and session.state == SessionState.REVIEWING:
            session.state = transition(session.state, Event.APPROVE_DRAFT)
            await ask_for_schedule_time(sender, msg.channel_user_id)
        else:
            await sender.send_text(
                msg.channel_user_id,
                "Subcommands:\n"
                "• /schedule list — view upcoming posts\n"
                "• /schedule auto — toggle Mon/Fri auto-scheduling\n"
                "• /schedule cancel {id} — cancel a scheduled post",
            )
    else:
        await sender.send_text(
            msg.channel_user_id,
            "Unknown subcommand. Try:\n" "• /schedule list\n" "• /schedule auto\n" "• /schedule cancel {post_id}",
        )


# ── Private helpers ────────────────────────────────────────────────────────


async def _confirm_and_enqueue(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """User confirmed the schedule time — save post and enqueue Celery job."""
    from app.scheduler.tasks import schedule_post_task

    proposed = session.context.proposed_schedule_time
    if not proposed:
        await ask_for_schedule_time(sender, msg.channel_user_id)
        return

    scheduled_for = datetime.fromisoformat(proposed)
    content = session.context.draft_content

    if not content:
        await sender.send_text(
            msg.channel_user_id,
            "I lost the draft. Please start again with your topic.",
        )
        session.state = SessionState.IDLE
        return

    # Save post to DB
    db = await get_db()
    post_result = await (
        db.table("posts")
        .insert(
            {
                "user_id": str(user.id),
                "content": content,
                "status": "approved",
                "scheduled_for": scheduled_for.isoformat(),
                "version": session.context.draft_version,
                "edit_history": session.context.edit_history,
                "metadata": {
                    "tone": session.context.tone,
                    "audience": session.context.audience,
                    "length_pref": session.context.length_pref,
                    "llm_model": user.llm_model,
                },
            }
        )
        .execute()
    )
    if not post_result or not post_result.data:
        await sender.send_text(
            msg.channel_user_id,
            "Failed to save your post. Please try again or /cancel to start fresh.",
        )
        return
    post_id = post_result.data[0]["id"]
    session.draft_id = None

    # Enqueue Celery task to publish at scheduled_for
    schedule_post_task.apply_async(
        kwargs={
            "user_id": str(user.id),
            "post_id": post_id,
            "channel": user.channel,
            "channel_user_id": msg.channel_user_id,
        },
        eta=scheduled_for,
    )

    # Also enqueue a reminder 1 hour before
    reminder_time = scheduled_for - timedelta(hours=1)
    now_utc = datetime.now(UTC)
    if reminder_time > now_utc:
        from app.scheduler.tasks import send_reminder_task

        send_reminder_task.apply_async(
            kwargs={
                "user_id": str(user.id),
                "post_id": post_id,
                "channel": user.channel,
                "channel_user_id": msg.channel_user_id,
            },
            eta=reminder_time,
        )

    user_tz = pytz.timezone(user.timezone)
    local_str = _format_dt(scheduled_for, user_tz)

    session.state = transition(session.state, Event.SCHEDULE_CONFIRMED)
    session.context.clear_draft()

    # Update style memory from this approved post (async, non-blocking)
    from app.scheduler.tasks import update_style_memory_task

    update_style_memory_task.delay(user_id=str(user.id), content=content)

    await sender.send_text(
        msg.channel_user_id,
        f"✅ *Scheduled!*\n\n"
        f"Your post will go live on *{local_str}*.\n"
        f"I'll remind you 1 hour before.\n\n"
        f"Post ID: `{post_id}`\n"
        f"_(Use /schedule cancel {post_id} to cancel)_\n\n"
        "Send me another topic whenever you're ready!",
    )

    logger.info(
        "scheduling.post_scheduled",
        user_id=str(user.id),
        post_id=post_id,
        scheduled_for=scheduled_for.isoformat(),
    )


async def _check_conflict(user_id: str, proposed_dt: datetime) -> bool:
    """Check if user already has a post scheduled within ±30 minutes."""
    from app.config import get_settings

    settings = get_settings()
    window = timedelta(minutes=settings.conflict_window_minutes)

    db = await get_db()
    result = await (
        db.table("posts")
        .select("id")
        .eq("user_id", user_id)
        .eq("status", "scheduled")
        .gte("scheduled_for", (proposed_dt - window).isoformat())
        .lte("scheduled_for", (proposed_dt + window).isoformat())
        .execute()
    )
    return len(result.data) > 0 if result and result.data else False


async def _handle_list(sender: object, channel_user_id: str, user_id: str) -> None:
    """Show the user's upcoming scheduled posts."""
    db = await get_db()
    result = await (
        db.table("posts")
        .select("id, content, status, scheduled_for")
        .eq("user_id", user_id)
        .in_("status", ["scheduled", "approved"])
        .order("scheduled_for", desc=False)
        .limit(5)
        .execute()
    )

    if not result or not result.data:
        await sender.send_text(channel_user_id, "No upcoming scheduled posts. Send me a topic to write one!")
        return

    lines = ["*Upcoming posts:*\n"]
    for post in result.data:
        sf = post.get("scheduled_for", "")
        preview = post["content"][:60].replace("\n", " ")
        lines.append(f"📅 `{sf[:16]}`\n   {preview}...\n   ID: `{post['id'][:8]}`\n")

    await sender.send_text(channel_user_id, "\n".join(lines))


async def _handle_toggle_auto(sender: object, channel_user_id: str, user_id: str) -> None:
    """Toggle Mon/Fri auto-scheduling on or off."""
    db = await get_db()
    result = await db.table("user_schedules").select("enabled").eq("user_id", user_id).maybe_single().execute()

    current = result.data["enabled"] if result and result.data else False
    new_state = not current

    await (
        db.table("user_schedules")
        .upsert(
            {"user_id": user_id, "enabled": new_state},
            on_conflict="user_id",
        )
        .execute()
    )

    status = "ON ✅" if new_state else "OFF ❌"
    await sender.send_text(
        channel_user_id,
        f"Auto-schedule is now *{status}*\n\n"
        + (
            "Posts will be queued for the next Mon/Fri 9am slot automatically."
            if new_state
            else "Posts will require manual scheduling."
        ),
    )


async def _handle_cancel_post(
    sender: object,
    channel_user_id: str,
    user_id: str,
    post_id_prefix: str,
) -> None:
    """Cancel a scheduled post by ID (or ID prefix)."""
    db = await get_db()
    result = await (
        db.table("posts")
        .select("id, status, zernio_post_id, content")
        .eq("user_id", user_id)
        .like("id", f"{post_id_prefix}%")
        .maybe_single()
        .execute()
    )

    if not result or not result.data:
        await sender.send_text(channel_user_id, f"No post found with ID starting with `{post_id_prefix}`.")
        return

    post = result.data
    if post["status"] == "published":
        await sender.send_text(
            channel_user_id,
            "That post has already been published — it can't be cancelled.\n"
            "You can delete it directly from LinkedIn.",
        )
        return

    if post["status"] in ("cancelled", "failed"):
        await sender.send_text(channel_user_id, f"That post is already `{post['status']}`.")
        return

    # Cancel in Zernio if it has a Zernio ID
    if post.get("zernio_post_id"):
        try:
            user_result = await db.table("users").select("zernio_api_key_enc").eq("id", user_id).single().execute()
            from app.core.encryption import decrypt
            from app.zernio.client import ZernioClient

            api_key = decrypt(user_result.data["zernio_api_key_enc"])
            client = ZernioClient(api_key=api_key)
            await client.cancel_post(post["zernio_post_id"])
        except Exception as exc:
            logger.warning("scheduling.zernio_cancel_failed", error=str(exc))

    # Mark as cancelled in our DB
    await db.table("posts").update({"status": "cancelled"}).eq("id", post["id"]).execute()
    await db.table("schedule_jobs").update({"status": "cancelled"}).eq("post_id", post["id"]).execute()

    await sender.send_text(
        channel_user_id,
        f"✅ Post cancelled.\n\nPreview: _{post['content'][:80]}..._",
    )


def _parse_datetime(text: str, user_tz: pytz.BaseTzInfo) -> datetime | None:
    """
    Parse a natural language datetime string in the user's timezone.
    Returns UTC datetime or None if parsing fails.
    """
    # Handle "now" / "immediately" separately
    if text.lower() in ("now", "immediately", "right now", "today now"):
        return datetime.now(UTC) + timedelta(minutes=1)

    # Handle "auto" — next Mon/Fri slot
    if text.lower() in ("auto", "auto schedule", "next slot"):
        return _next_auto_slot(user_tz)

    parsed = dateparser.parse(
        text,
        settings={
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": str(user_tz),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DAY_OF_MONTH": "first",
        },
    )
    if parsed is None:
        return None

    # Convert to UTC
    return parsed.astimezone(UTC)


def _next_auto_slot(user_tz: pytz.BaseTzInfo) -> datetime:
    """Find the next Monday or Friday at 9am in the user's timezone."""
    now_local = datetime.now(user_tz)
    target_days = {0, 4}  # Monday=0, Friday=4

    for days_ahead in range(1, 8):
        candidate = now_local + timedelta(days=days_ahead)
        if candidate.weekday() in target_days:
            slot = candidate.replace(hour=9, minute=0, second=0, microsecond=0)
            return slot.astimezone(UTC)

    # Fallback: 7 days from now
    return (now_local + timedelta(days=7)).astimezone(UTC)


def _suggest_next_slot(conflicted_dt: datetime, user_tz: pytz.BaseTzInfo) -> datetime:
    """Suggest the next Mon/Fri 9am slot after a conflicted time."""
    local_dt = conflicted_dt.astimezone(user_tz)
    target_days = {0, 4}

    for days_ahead in range(1, 8):
        candidate = local_dt + timedelta(days=days_ahead)
        if candidate.weekday() in target_days:
            slot = candidate.replace(hour=9, minute=0, second=0, microsecond=0)
            return slot.astimezone(UTC)

    return conflicted_dt + timedelta(days=7)


def _format_dt(dt: datetime, user_tz: pytz.BaseTzInfo) -> str:
    """Format a UTC datetime in the user's timezone for display."""
    local = dt.astimezone(user_tz)
    return local.strftime("%A, %d %b %Y at %I:%M %p %Z")
