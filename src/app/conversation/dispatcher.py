"""
app/conversation/dispatcher.py
───────────────────────────────
The conversation dispatcher is the single entry point from the gateway.

For every inbound message it:
  1. Loads the user row (to check onboarding status)
  2. Loads / creates the session
  3. Handles universal commands (/cancel, /help, /settings, etc.)
  4. Routes to the correct handler based on current FSM state
  5. Handles any exceptions and sends user-friendly error messages

This module never contains business logic — it only routes.
"""

import inspect
from uuid import UUID

from app.channels.base import NormalisedMessage
from app.core.logging import get_logger
from app.core.rate_limiter import LimitType, RateLimitExceeded, check_rate_limit
from app.db.client import get_db
from app.db.models import UserRow
from app.session.fsm import Event
from app.session.models import SessionState
from app.session.store import Session, get_or_create_session, reset_session_to_idle, save_session

logger = get_logger(__name__)


async def dispatch(user_id: UUID, msg: NormalisedMessage) -> None:
    """
    Main dispatcher. Called by gateway.router for every inbound message.
    """
    # ── Load user ───────────────────────────────────────────────────────────
    user = await _load_user(user_id)
    if user is None:
        logger.error("dispatcher.user_not_found", user_id=str(user_id))
        return

    # ── Load session ────────────────────────────────────────────────────────
    session = await get_or_create_session(user_id)

    # ── Get sender for this channel ─────────────────────────────────────────
    sender = _get_sender(msg.channel)

    # ── Rate limiting ───────────────────────────────────────────────────────
    try:
        await check_rate_limit(str(user_id), LimitType.MESSAGE)
    except Exception as rate_exc:
        if isinstance(rate_exc, RateLimitExceeded):
            await sender.send_text(msg.channel_user_id, rate_exc.user_message())
            return

    try:
        # ── Universal commands (work from ANY state) ─────────────────────────
        if msg.is_command("/cancel"):
            await _handle_cancel(session, user, sender, msg)
            return

        if msg.is_command("/help"):
            await _handle_help(sender, msg.channel_user_id, session.state)
            return

        if msg.is_command("/start"):
            await _handle_start(session, user, sender, msg)
            return

        if msg.is_command("/new"):
            await _handle_new(session, user, sender, msg)
            return

        if msg.is_command("/settings"):
            await _handle_settings(session, user, sender, msg)
            return

        if msg.is_command("/schedule"):
            await _handle_schedule_command(session, user, sender, msg)
            return

        if msg.is_command("/reconnect"):
            await _handle_reconnect(session, user, sender, msg)
            return

        if msg.is_command("/timezone"):
            await _handle_timezone(session, user, sender, msg)
            return

        if msg.is_command("/delete"):
            await _handle_delete(session, user, sender, msg)
            return

        if msg.is_command("/drafts"):
            await _handle_drafts(session, user, sender, msg)
            return

        # ── Route by FSM state ───────────────────────────────────────────────
        await _route_by_state(session, user, sender, msg)

    except Exception as exc:
        logger.exception("dispatcher.unhandled_error", error=str(exc), state=session.state)
        from app.core.errors import format_error_for_user
        await sender.send_text(msg.channel_user_id, format_error_for_user(exc))
    finally:
        # Always save session state, even on error
        await save_session(session)


# ── State router ───────────────────────────────────────────────────────────


async def _route_by_state(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Route message to the correct handler based on current FSM state."""

    # New user — always go to onboarding
    if not _is_onboarded(user) or session.state == SessionState.ONBOARDING:
        from app.conversation.onboarding import handle_onboarding
        await handle_onboarding(session, user, sender, msg)
        return

    match session.state:
        case SessionState.IDLE:
            from app.conversation.collecting import handle_idle
            await handle_idle(session, user, sender, msg)

        case SessionState.COLLECTING:
            from app.conversation.collecting import handle_collecting
            await handle_collecting(session, user, sender, msg)

        case SessionState.GENERATING:
            # LLM is working async — queue the message for after completion
            session.context.pending_message = msg.text
            await sender.send_text(
                msg.channel_user_id,
                "I'm still writing your post — hold tight! "
                "I'll handle your message once the draft is ready.",
            )

        case SessionState.REVIEWING:
            from app.conversation.reviewing import handle_reviewing
            await handle_reviewing(session, user, sender, msg)

        case SessionState.REFINING:
            session.context.pending_message = msg.text
            await sender.send_text(
                msg.channel_user_id,
                "Still refining — I'll apply your next edit right after this one.",
            )

        case SessionState.SCHEDULING:
            from app.conversation.scheduling import handle_scheduling
            await handle_scheduling(session, user, sender, msg)

        case SessionState.SCHEDULED:
            # Post is queued — treat any new message as starting a new post
            from app.conversation.collecting import handle_idle
            await sender.send_text(
                msg.channel_user_id,
                "Your post is scheduled! Starting a new one...",
            )
            session.context.clear_draft()
            session.state = SessionState.IDLE
            await handle_idle(session, user, sender, msg)

        case _:
            logger.warning("dispatcher.unknown_state", state=session.state)
            await sender.send_text(
                msg.channel_user_id,
                "I got confused about where we were. Let's start fresh — send me a post idea!",
            )
            session.state = SessionState.IDLE
            session.context.clear_draft()


# ── Universal command handlers ──────────────────────────────────────────────


async def _handle_cancel(
    session: Session, user: UserRow, sender: object, msg: NormalisedMessage
) -> None:
    from app.session.fsm import transition
    was_state = session.state

    if was_state == SessionState.IDLE:
        await sender.send_text(msg.channel_user_id, "Nothing to cancel. Send me a post idea to get started!")
        return

    session.state = transition(session.state, Event.CANCEL)
    session.context.clear_draft()
    session.draft_id = None

    await sender.send_text(
        msg.channel_user_id,
        "Cancelled. Your draft has been discarded.\n\nSend me a new topic whenever you're ready!",
    )


async def _handle_start(
    session: Session, user: UserRow, sender: object, msg: NormalisedMessage
) -> None:
    if not _is_onboarded(user):
        from app.conversation.onboarding import start_onboarding
        await start_onboarding(session, user, sender, msg)
    else:
        name = user.channel_user_id  # Will improve once we store display names
        await sender.send_text(
            msg.channel_user_id,
            f"Welcome back! 👋\n\nSend me a topic or idea and I'll write your next LinkedIn post.\n\n"
            f"*Commands:* /new · /schedule list · /settings · /help",
        )


async def _handle_new(
    session: Session, user: UserRow, sender: object, msg: NormalisedMessage
) -> None:
    if not _is_onboarded(user):
        from app.conversation.onboarding import start_onboarding
        await start_onboarding(session, user, sender, msg)
        return
    session.state = SessionState.IDLE
    session.context.clear_draft()
    session.draft_id = None
    await sender.send_text(
        msg.channel_user_id,
        "Starting fresh! What do you want to post about?",
    )


async def _handle_help(sender: object, channel_user_id: str, state: SessionState) -> None:
    text = (
        "*LinkedIn AI Content System — Commands*\n\n"
        "/new — Start a new post\n"
        "/cancel — Cancel current draft\n"
        "/schedule list — View scheduled posts\n"
        "/schedule auto — Toggle Mon/Fri auto-scheduling\n"
        "/settings — View/change LLM, timezone, style\n"
        "/reconnect — Re-link your LinkedIn account\n"
        "/timezone [tz] — Set timezone (e.g. /timezone Asia/Kolkata)\n"
        "/help — Show this message\n\n"
        f"_Current status: {state}_"
    )
    await sender.send_text(channel_user_id, text)


async def _handle_settings(
    session: Session, user: UserRow, sender: object, msg: NormalisedMessage
) -> None:
    from app.conversation.settings import handle_settings
    await handle_settings(session, user, sender, msg)


async def _handle_schedule_command(
    session: Session, user: UserRow, sender: object, msg: NormalisedMessage
) -> None:
    from app.conversation.scheduling import handle_schedule_command
    await handle_schedule_command(session, user, sender, msg)


async def _handle_reconnect(
    session: Session, user: UserRow, sender: object, msg: NormalisedMessage
) -> None:
    from app.conversation.onboarding import restart_linkedin_oauth
    await restart_linkedin_oauth(session, user, sender, msg)


async def _handle_timezone(
    session: Session, user: UserRow, sender: object, msg: NormalisedMessage
) -> None:
    tz_arg = msg.command_args
    if not tz_arg:
        await sender.send_text(
            msg.channel_user_id,
            "Usage: /timezone Asia/Kolkata\n\nCommon timezones:\n"
            "• Asia/Kolkata (IST)\n• America/New_York (EST)\n• Europe/London (GMT)\n• UTC",
        )
        return

    import pytz
    try:
        pytz.timezone(tz_arg)  # Validate
        db = await get_db()
        await db.table("users").update({"timezone": tz_arg}).eq("id", str(user.id)).execute()
        await sender.send_text(msg.channel_user_id, f"Timezone updated to *{tz_arg}*. ✓")
    except pytz.exceptions.UnknownTimeZoneError:
        await sender.send_text(
            msg.channel_user_id,
            f"Unknown timezone: `{tz_arg}`\n\nTry a value like `Asia/Kolkata`, `America/New_York`, or `UTC`.",
        )


async def _handle_delete(
    session: Session, user: UserRow, sender: object, msg: NormalisedMessage
) -> None:
    """
    /delete — permanently deactivate a user account.

    Two-step: first message shows a warning and asks for confirmation.
    User must reply '/delete confirm' to proceed.
    This prevents accidental deletion from a mistyped command.
    """
    args = (msg.command_args or "").strip().lower()

    if args != "confirm":
        await sender.send_text(
            msg.channel_user_id,
            "⚠️ *Delete your account?*\n\n"
            "This will:\n"
            "• Cancel all your scheduled posts\n"
            "• Remove your Zernio and LLM API keys\n"
            "• Deactivate your account\n\n"
            "Your posts that are already published on LinkedIn are not affected.\n\n"
            "To confirm, send: `/delete confirm`\n"
            "To cancel, send anything else.",
        )
        return

    # User confirmed — proceed with soft deletion
    db = await get_db()
    user_id = str(user.id)

    # 1. Cancel all pending schedule jobs
    await _maybe_await(
        db.table("schedule_jobs")
        .update({"status": "cancelled"})
        .eq("user_id", user_id)
        .eq("status", "pending")
        .execute()
    )

    # 2. Cancel scheduled posts in Zernio if possible
    scheduled_posts = await _maybe_await(
        db.table("posts")
        .select("id, zernio_post_id")
        .eq("user_id", user_id)
        .eq("status", "scheduled")
        .execute()
    )
    if scheduled_posts.data:
        for post in scheduled_posts.data:
            if post.get("zernio_post_id") and user.zernio_api_key_enc:
                try:
                    from app.zernio.client import make_zernio_client
                    client = make_zernio_client(user)
                    await client.cancel_post(post["zernio_post_id"])
                except Exception:
                    pass  # Best-effort cancellation
        await _maybe_await(
            db.table("posts")
            .update({"status": "cancelled"})
            .eq("user_id", user_id)
            .eq("status", "scheduled")
            .execute()
        )

    # 3. Clear encrypted credentials (GDPR: wipe sensitive data)
    await _maybe_await(
        db.table("users")
        .update(
            {
                "is_active": False,
                "zernio_api_key_enc": None,
                "zernio_profile_id": None,
                "zernio_account_id": None,
                "llm_api_key_enc": None,
                "llm_provider": None,
                "llm_model": None,
            }
        )
        .eq("id", user_id)
        .execute()
    )

    # 4. Clear session
    session.state = SessionState.IDLE
    session.context.clear_draft()

    logger.info("dispatcher.user_deleted", user_id=user_id)

    await sender.send_text(
        msg.channel_user_id,
        "✅ Your account has been deactivated and your API keys removed.\n\n"
        "Your published LinkedIn posts are untouched.\n\n"
        "To start fresh, send /start at any time.",
    )


async def _handle_drafts(
    session: Session, user: UserRow, sender: object, msg: NormalisedMessage
) -> None:
    """
    /drafts — list the user's recent posts with their statuses.
    Shows last 5 posts across all statuses: draft, scheduled, published, failed.
    """
    db = await get_db()
    result = await (
        db.table("posts")
        .select("id, content, status, scheduled_for, published_at, created_at")
        .eq("user_id", str(user.id))
        .order("created_at", desc=True)
        .limit(5)
        .execute()
    )

    if not result or not result.data:
        await sender.send_text(
            msg.channel_user_id,
            "You don't have any posts yet.\n\nSend me a topic to write your first one!",
        )
        return

    status_emoji = {
        "draft":     "📝",
        "approved":  "✅",
        "scheduled": "📅",
        "published": "🟢",
        "failed":    "❌",
        "cancelled": "🚫",
    }

    lines = ["*Your recent posts:*\n"]
    for post in result.data:
        emoji = status_emoji.get(post["status"], "•")
        preview = post["content"][:60].replace("\n", " ")
        post_id_short = post["id"][:8]

        date_info = ""
        if post["status"] == "scheduled" and post.get("scheduled_for"):
            date_info = f" — {post['scheduled_for'][:16]}"
        elif post["status"] == "published" and post.get("published_at"):
            date_info = f" — published {post['published_at'][:10]}"

        lines.append(
            f"{emoji} `{post_id_short}` *{post['status']}*{date_info}\n"
            f"   _{preview}..._\n"
        )

    lines.append("\n_Use /schedule cancel {id} to cancel a scheduled post_")
    await sender.send_text(msg.channel_user_id, "\n".join(lines))


# ── Helpers ────────────────────────────────────────────────────────────────


def _is_onboarded(user: UserRow) -> bool:
    """User is fully onboarded if they have a Zernio account and an LLM configured."""
    return bool(
        user.zernio_api_key_enc
        and user.zernio_account_id
        and user.llm_api_key_enc
        and user.llm_provider
    )


def _get_sender(channel: str) -> object:
    """Return the appropriate channel sender."""
    if channel == "telegram":
        from app.channels.telegram import TelegramSender
        return TelegramSender()
    elif channel == "whatsapp":
        from app.channels.whatsapp import WhatsAppSender
        return WhatsAppSender()
    else:
        raise ValueError(f"Unknown channel: {channel}")


async def _load_user(user_id: UUID) -> UserRow | None:
    db = await get_db()
    result = await (
        db.table("users")
        .select("*")
        .eq("id", str(user_id))
        .eq("is_active", True)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        return None
    return UserRow(**result.data)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value
