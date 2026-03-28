"""
app/conversation/reviewing.py
-----------------------------
Handles the REVIEWING state after the user receives a draft.
"""

import re

from app.channels.base import NormalisedMessage
from app.core.logging import get_logger
from app.db.models import UserRow
from app.session.fsm import Event, transition
from app.session.models import SessionState
from app.session.store import Session

logger = get_logger(__name__)

APPROVE_PHRASES = {
    "approve",
    "approved",
    "looks good",
    "perfect",
    "great",
    "yes",
    "good",
    "send it",
    "ship it",
    "done",
    "ok",
    "okay",
    "publish",
    "schedule it",
    "let s go",
    "go",
    "lgtm",
}

DISCARD_PHRASES = {
    "discard",
    "scrap",
    "trash",
    "delete",
    "no",
    "nope",
    "start over",
    "restart",
    "redo",
    "cancel",
    "forget it",
    "nevermind",
}

PUBLISH_NOW_PHRASES = {
    "post now",
    "publish now",
    "send now",
    "right now",
    "immediately",
    "post immediately",
    "publish immediately",
    "post it",
    "publish it",
}

SCHEDULE_PHRASES = {
    "schedule",
    "schedule it",
    "schedule this",
    "set a time",
    "pick a time",
}


async def handle_reviewing(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """Route user response while in REVIEWING state."""
    text = msg.text.strip()
    normalized = _normalize_text(text)

    if _contains_phrase(normalized, PUBLISH_NOW_PHRASES):
        await _handle_publish_now(session, user, sender, msg)
        return

    if normalized in SCHEDULE_PHRASES:
        session.state = transition(session.state, Event.APPROVE_DRAFT)
        from app.conversation.scheduling import ask_for_schedule_time

        await ask_for_schedule_time(sender, msg.channel_user_id)
        return

    if normalized in APPROVE_PHRASES or _is_approval(normalized):
        session.state = transition(session.state, Event.APPROVE_DRAFT)
        from app.conversation.scheduling import ask_for_schedule_time

        await ask_for_schedule_time(sender, msg.channel_user_id)
        return

    if normalized in DISCARD_PHRASES or _is_discard(normalized):
        session.state = transition(session.state, Event.DISCARD_DRAFT)
        session.context.clear_draft()
        session.draft_id = None
        await sender.send_text(
            msg.channel_user_id,
            "Draft discarded.\n\nSend me a new topic whenever you're ready!",
        )
        return

    schedule_keywords = ("schedule", "monday", "friday", "tomorrow", "next week", "at ", "am", "pm")
    if any(kw in text.lower() for kw in schedule_keywords) and _is_approval(normalized):
        session.state = transition(session.state, Event.APPROVE_DRAFT)
        from app.conversation.scheduling import handle_scheduling

        await handle_scheduling(session, user, sender, msg)
        return

    if _looks_unclear_review_reply(normalized):
        await sender.send_text(
            msg.channel_user_id,
            "I didn't quite catch that.\n\n"
            "Reply with *approve*, *post now*, *schedule*, *discard*, or give an edit instruction like `make it shorter`.",
        )
        return

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
            "I seem to have lost the draft. Let's start over. Send me your topic again.",
        )
        session.state = SessionState.IDLE
        session.context.clear_draft()
        return

    session.context.push_edit_history(draft)
    session.state = transition(session.state, Event.REQUEST_EDIT)

    await sender.send_text(msg.channel_user_id, "Refining...")

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
    approval_words = {"yes", "good", "great", "perfect", "approve", "approved", "go", "ok", "okay", "ship", "send"}
    words = set(text.split())
    return bool(words & approval_words)


def _is_discard(text: str) -> bool:
    discard_words = {"no", "discard", "scrap", "redo", "over", "restart", "cancel"}
    words = set(text.split())
    return bool(words & discard_words)


def _normalize_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", text.lower()).split())


def _contains_phrase(text: str, phrases: set[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _looks_unclear_review_reply(text: str) -> bool:
    if not text:
        return True

    tokens = text.split()
    if len(tokens) == 1 and len(tokens[0]) <= 4:
        return True
    return False


def present_draft(content: str, version: int) -> str:
    """Format a draft for presentation to the user."""
    header = f"*Draft v{version}*\n{'-' * 30}\n\n"
    footer = (
        "\n\n"
        "---------------------\n"
        "✅ Approve  |  ✏️ Edit  |  🗑 Discard  |  📅 Schedule\n\n"
        "_Reply with your choice or edit instructions (e.g. 'make it shorter', 'add hashtags')_"
    )
    return header + content + footer
