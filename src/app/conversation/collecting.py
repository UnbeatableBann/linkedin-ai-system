"""
app/conversation/collecting.py
───────────────────────────────
Handles the IDLE and COLLECTING states.

IDLE: Receives the user's first message (a topic, an idea, or a full draft).
      Decides whether to ask clarifying questions or jump straight to generation.

COLLECTING: Asks guided questions (tone, audience, length).
            After enough context, fires GENERATING via Celery.

Intent detection:
  - Full draft (>150 chars, has paragraphs) → GENERATING directly
  - Short topic/idea → COLLECTING (ask 1–2 questions)
  - User says "just write it" → GENERATING with what we have
"""

import re

from app.channels.base import NormalisedMessage
from app.core.logging import get_logger
from app.db.models import UserRow
from app.session.fsm import Event, transition
from app.session.store import Session

logger = get_logger(__name__)

# If the user sends text longer than this, assume it's a full draft
FULL_DRAFT_THRESHOLD = 150
COMMON_SHORT_TOPICS = {
    "ai",
    "ml",
    "llm",
    "ux",
    "ui",
    "qa",
    "hr",
    "seo",
    "api",
    "saas",
    "b2b",
    "b2c",
}

# Phrases that mean "skip questions and just generate"
SKIP_PHRASES = {
    "just write it",
    "go ahead",
    "write it",
    "just do it",
    "don't ask",
    "skip",
    "generate",
    "write now",
    "just write",
}


async def handle_idle(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """
    Handle a message in IDLE state — the starting point for every new post.
    """
    text = msg.text.strip()

    # Detect full draft — user pasted their own content
    if len(text) > FULL_DRAFT_THRESHOLD and "\n" in text:
        session.context.clear_draft()
        session.context.topic = text
        session.state = transition(session.state, Event.START_POST_DIRECT)
        await sender.send_text(
            msg.channel_user_id,
            "Got your draft! Let me polish it into a LinkedIn post...",
        )
        await _enqueue_generation(session, user, msg.channel_user_id, sender)
        return

    if not _looks_like_meaningful_topic(text):
        await sender.send_text(
            msg.channel_user_id,
            "I need a clearer topic or idea before I write the post.\n\n"
            "Try something like:\n"
            "- `how AI is saving me time at work`\n"
            "- `lessons from building my first product`\n"
            "- `why engineers should write on LinkedIn`",
        )
        return

    session.context.clear_draft()
    session.context.topic = text
    session.context.clarification_round = 0
    session.state = transition(session.state, Event.START_POST)

    # Check if user has style prefs already set — if so, skip some questions
    has_prefs = bool(user.style_prefs.get("tone") or user.style_prefs.get("audience"))

    if has_prefs:
        # Use saved prefs, only ask if they want to override for this post
        tone = user.style_prefs.get("tone", "professional")
        audience = user.style_prefs.get("audience", "professionals")
        session.context.tone = tone
        session.context.audience = audience

        await sender.send_text(
            msg.channel_user_id,
            f"Great topic! Using your saved style: *{tone}* tone for *{audience}*.\n\n"
            "How long should the post be?\n\n"
            "1. Short (~150 words)\n"
            "2. Medium (~300 words) — recommended\n"
            "3. Long (~500 words)\n\n"
            "Or just say *go* to use the default (medium).",
        )
        session.context.clarification_round = 2  # Only 1 question left
    else:
        # No saved prefs — ask tone first
        await _ask_tone(sender, msg.channel_user_id)
        session.context.clarification_round = 0


async def handle_collecting(
    session: Session,
    user: UserRow,
    sender: object,
    msg: NormalisedMessage,
) -> None:
    """
    Handle messages in COLLECTING state — gathering post parameters.
    """
    text = msg.text.strip().lower()

    # User wants to skip questions
    if any(phrase in text for phrase in SKIP_PHRASES):
        _apply_defaults(session, user)
        await sender.send_text(msg.channel_user_id, "Got it! Writing your post now...")
        session.state = transition(session.state, Event.ENOUGH_INFO)
        await _enqueue_generation(session, user, msg.channel_user_id, sender)
        return

    round_num = session.context.clarification_round

    # Round 0: collecting tone
    if round_num == 0:
        tone = _parse_tone(msg.text)
        if tone is None:
            await sender.send_text(
                msg.channel_user_id,
                "I didn't quite catch the tone.\n\n"
                "Pick *1-4* or describe it in a few words like `bold and opinionated` or `friendly and simple`.",
            )
            await _ask_tone(sender, msg.channel_user_id)
            return
        session.context.tone = tone
        session.context.clarification_round = 1
        await _ask_audience(sender, msg.channel_user_id)
        return

    # Round 1: collecting audience
    if round_num == 1:
        audience = _parse_audience(msg.text)
        if audience is None:
            await sender.send_text(
                msg.channel_user_id,
                "I didn't quite catch the audience.\n\n"
                "Pick *1-4* or describe the audience like `backend engineers`, `job seekers`, or `early-stage founders`.",
            )
            await _ask_audience(sender, msg.channel_user_id)
            return
        session.context.audience = audience
        session.context.clarification_round = 2
        await _ask_length(sender, msg.channel_user_id)
        return

    # Round 2: collecting length preference
    if round_num == 2:
        length_pref = _parse_length(msg.text)
        if length_pref is None:
            await sender.send_text(
                msg.channel_user_id,
                "I didn't catch the length.\n\n"
                "Reply with *1*, *2*, *3*, or say *go* to use medium.",
            )
            await _ask_length(sender, msg.channel_user_id)
            return
        session.context.length_pref = length_pref
        session.context.clarification_round = 3

        # Enough info — generate
        session.state = transition(session.state, Event.ENOUGH_INFO)
        await sender.send_text(
            msg.channel_user_id,
            f"Writing a *{session.context.length_pref}* post in a *{session.context.tone}* "
            f"tone for *{session.context.audience}*...\n\n"
            "This takes a few seconds ✍️",
        )
        await _enqueue_generation(session, user, msg.channel_user_id, sender)
        return

    # Fallback: we have enough, generate
    _apply_defaults(session, user)
    session.state = transition(session.state, Event.ENOUGH_INFO)
    await sender.send_text(msg.channel_user_id, "Writing your post now...")
    await _enqueue_generation(session, user, msg.channel_user_id, sender)


# ── Question helpers ───────────────────────────────────────────────────────


async def _ask_tone(sender: object, channel_user_id: str) -> None:
    await sender.send_text(
        channel_user_id,
        "What tone do you want?\n\n"
        "1. *Thought leadership* — insights and strong opinions\n"
        "2. *Storytelling* — personal narrative with a lesson\n"
        "3. *Tactical tips* — practical how-to advice\n"
        "4. *Conversational* — casual and relatable\n\n"
        "Reply with a number, or describe your own style:",
    )


async def _ask_audience(sender: object, channel_user_id: str) -> None:
    await sender.send_text(
        channel_user_id,
        "Who's your target audience?\n\n"
        "1. Founders / startup people\n"
        "2. Product managers\n"
        "3. Engineers / tech people\n"
        "4. General professionals\n\n"
        "Reply with a number, or describe your audience:",
    )


async def _ask_length(sender: object, channel_user_id: str) -> None:
    await sender.send_text(
        channel_user_id,
        "How long should the post be?\n\n"
        "1. Short (~150 words)\n"
        "2. Medium (~300 words) — recommended\n"
        "3. Long (~500 words)\n\n"
        "Or say *go* to use medium:",
    )


def _parse_tone(text: str) -> str | None:
    t = text.strip().lower()
    if t in ("1", "thought leadership", "thought", "leadership"):
        return "thought leadership"
    if t in ("2", "storytelling", "story", "narrative"):
        return "storytelling"
    if t in ("3", "tactical", "tips", "tactical tips", "how-to"):
        return "tactical tips"
    if t in ("4", "conversational", "casual", "relatable"):
        return "conversational"
    if _looks_unclear_freeform_reply(text):
        return None
    return text.strip()


def _parse_audience(text: str) -> str | None:
    t = text.strip().lower()
    if t in ("1", "founders", "founder", "startup", "startup people"):
        return "founders / startup people"
    if t in ("2", "product managers", "product manager", "pm", "pms"):
        return "product managers"
    if t in ("3", "engineers", "engineer", "tech", "tech people", "developers", "developer"):
        return "engineers / tech people"
    if t in ("4", "general professionals", "professionals", "professional", "general"):
        return "general professionals"
    if _looks_unclear_freeform_reply(text):
        return None
    return text.strip()


def _parse_length(text: str) -> str | None:
    t = text.strip().lower()
    if t in ("1", "short", "brief"):
        return "short (around 150 words)"
    if t in ("2", "medium", "go", "default"):
        return "medium (around 300 words)"
    if t in ("3", "long"):
        return "long (around 500 words)"
    if "150" in t:
        return "short (around 150 words)"
    if "300" in t:
        return "medium (around 300 words)"
    if "500" in t:
        return "long (around 500 words)"
    return None


def _apply_defaults(session: Session, user: UserRow) -> None:
    """Fill in missing context with defaults or saved user prefs."""
    if not session.context.tone:
        session.context.tone = user.style_prefs.get("tone", "professional")
    if not session.context.audience:
        session.context.audience = user.style_prefs.get("audience", "professionals")
    if not session.context.length_pref:
        session.context.length_pref = "medium (around 300 words)"


def _looks_like_meaningful_topic(text: str) -> bool:
    cleaned = _normalise_freeform_text(text)
    if not cleaned:
        return False

    tokens = cleaned.split()
    if len(tokens) == 1:
        token = tokens[0]
        if token in COMMON_SHORT_TOPICS:
            return True
        if len(token) <= 3:
            return False
        if len(token) <= 5 and not _has_vowel(token):
            return False
        return True

    return len([token for token in tokens if len(token) >= 2]) >= 2


def _looks_unclear_freeform_reply(text: str) -> bool:
    cleaned = _normalise_freeform_text(text)
    if not cleaned:
        return True

    tokens = cleaned.split()
    if len(tokens) == 1:
        token = tokens[0]
        if token in COMMON_SHORT_TOPICS:
            return False
        if len(token) <= 3:
            return True
        if len(token) <= 5 and not _has_vowel(token):
            return True
    return False


def _normalise_freeform_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", text.lower()).split())


def _has_vowel(text: str) -> bool:
    return any(ch in "aeiou" for ch in text.lower())


async def _enqueue_generation(
    session: Session,
    user: UserRow,
    channel_user_id: str,
    sender: object,
) -> None:
    """
    Fire the Celery content generation task.
    The task runs async — it will update the session and send the draft
    back to the user when complete.
    """
    from app.scheduler.tasks import generate_post_task

    # Pass everything the task needs
    generate_post_task.delay(
        user_id=str(user.id),
        session_id=str(session.id),
        channel=user.channel,
        channel_user_id=channel_user_id,
        topic=session.context.topic,
        tone=session.context.tone,
        audience=session.context.audience,
        length_pref=session.context.length_pref,
        style_prefs=user.style_prefs,
    )

    logger.info(
        "collecting.generation_queued",
        user_id=str(user.id),
        topic=session.context.topic,
    )
