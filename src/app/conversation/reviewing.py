"""
app/conversation/reviewing.py
──────────────────────────────
Handles the REVIEWING state — user has received a draft and is deciding
what to do with it.

Valid user responses:
  ✅  approve / "looks good" / "perfect"       → SCHEDULING
  ✏️  any edit instruction                      → REFINING (Celery task)
  🗑  discard / "start over" / "scrap it"      → IDLE
  📅  "schedule for..."                         → SCHEDULING
  "post now" / "publish now"                   → publish immediately
"""

from app.channels.base import NormalisedMessage
from app.core.logging import get_logger
from app.db.models import UserRow
from app.session.fsm import Event, transition
from app.session.models import SessionState
from app.session.store import Session

logger = get_logger(__name__)

# Phrases that mean "this draft is approved"
APPROVE_PHRASES = {
    "✅", "approved", "approve", "looks good", "perfect", "great",
    "yes", "good", "send it", "ship it", "done", "ok", "okay",
    "publish", "schedule it", "let's go", "go", "lgtm",
}

# Phrases that mean "throw this away"
DISCARD_PHRASES = {
    "🗑", "discard", "scrap", "trash", "delete", "no", "nope",
    "start over", "restart", "redo", "cancel", "forget it", "nevermind",
}

# Phrases that mean "publish right now"
PUBLISH_NOW_PHRASES = {
    "post now", "publish now", "send now", "now", "right now",
    "immediately", "post immediately",
}


async def handle_reviewing(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """
    Route user response while in REVIEWING state.
    """
    text = msg.text.strip()
    text_lower = text.lower()

    # ── Publish immediately ─────────────────────────────────────────────────
    if any(phrase in text_lower for phrase in PUBLISH_NOW_PHRASES):
        await _handle_publish_now(session, user, sender, msg)
        return

    # ── Approve → go to scheduling ──────────────────────────────────────────
    if text_lower in APPROVE_PHRASES or _is_approval(text_lower):
        session.state = transition(session.state, Event.APPROVE_DRAFT)
        from app.conversation.scheduling import ask_for_schedule_time
        await ask_for_schedule_time(sender, msg.channel_user_id)
        return

    # ── Discard ─────────────────────────────────────────────────────────────
    if text_lower in DISCARD_PHRASES or _is_discard(text_lower):
        session.state = transition(session.state, Event.DISCARD_DRAFT)
        session.context.clear_draft()
        session.draft_id = None
        await sender.send_text(
            msg.channel_user_id,
            "Draft discarded. 🗑\n\nSend me a new topic whenever you're ready!",
        )
        return

    # ── Schedule time mentioned inline (e.g. "great, schedule for Monday") ──
    schedule_keywords = ("schedule", "monday", "friday", "tomorrow", "next week", "at ", "am", "pm")
    if any(kw in text_lower for kw in schedule_keywords) and _is_approval(text_lower):
        session.state = transition(session.state, Event.APPROVE_DRAFT)
        from app.conversation.scheduling import handle_scheduling
        await handle_scheduling(session, user, sender, msg)
        return

    # ── Everything else is an edit instruction ──────────────────────────────
    await _handle_edit_request(session, user, sender, msg, edit_instruction=text)


async def _handle_edit_request(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
    edit_instruction: str,
) -> None:
    """Queue a refinement task for the current draft."""
    draft = session.context.draft_content
    if not draft:
        await sender.send_text(
            msg.channel_user_id,
            "I seem to have lost the draft. Let's start over — send me your topic again.",
        )
        session.state = SessionState.IDLE
        session.context.clear_draft()
        return

    # Save current draft to history before overwriting
    session.context.push_edit_history(draft)
    session.state = transition(session.state, Event.REQUEST_EDIT)

    await sender.send_text(
        msg.channel_user_id,
        f"Refining... ✍️",
    )

    from app.scheduler.tasks import refine_post_task

    refine_post_task.delay(
        user_id=str(user.id),
        session_id=str(session.id),
        channel=user.channel,
        channel_user_id=msg.channel_user_id,
        current_draft=draft,
        edit_instruction=edit_instruction,
        style_prefs=user.style_prefs,
    )

    logger.info(
        "reviewing.refinement_queued",
        user_id=str(user.id),
        instruction=edit_instruction[:80],
    )


async def _handle_publish_now(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Publish the approved draft immediately via Zernio."""
    draft = session.context.draft_content
    if not draft:
        await sender.send_text(msg.channel_user_id, "No draft to publish. Send me a topic to start.")
        return

    await sender.send_text(msg.channel_user_id, "Publishing to LinkedIn now...")

    from app.scheduler.tasks import publish_now_task

    publish_now_task.delay(
        user_id=str(user.id),
        session_id=str(session.id),
        channel=user.channel,
        channel_user_id=msg.channel_user_id,
        content=draft,
    )

    session.state = SessionState.IDLE
    session.context.clear_draft()


def _is_approval(text: str) -> bool:
    """Fuzzy check for approval — catches phrases like 'yes looks great'."""
    approval_words = {"yes", "good", "great", "perfect", "approve", "approved", "go", "ok", "okay", "ship", "send"}
    words = set(text.lower().split())
    return bool(words & approval_words)


def _is_discard(text: str) -> bool:
    discard_words = {"no", "discard", "scrap", "redo", "over", "restart"}
    words = set(text.lower().split())
    return bool(words & discard_words)


def present_draft(content: str, version: int) -> str:
    """Format a draft for presentation to the user."""
    header = f"*Draft v{version}*\n{'─' * 30}\n\n"
    footer = (
        "\n\n"
        "─────────────────────\n"
        "✅ Approve  |  ✏️ Edit  |  🗑 Discard  |  📅 Schedule\n\n"
        "_Reply with your choice or edit instructions (e.g. 'make it shorter', 'add hashtags')_"
    )
    return header + content + footer
