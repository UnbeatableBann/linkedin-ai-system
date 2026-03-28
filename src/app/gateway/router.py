"""
app/gateway/router.py
─────────────────────
The gateway sits between raw webhook handlers and conversation logic.

For every inbound NormalisedMessage it:
  1. Deduplicates (skip if already seen)
  2. Resolves or creates the user row in Supabase (atomic upsert)
  3. Dispatches to the conversation dispatcher

This is the only place that touches user resolution — all downstream
code receives a resolved user_id (UUID string).
"""

from uuid import UUID

from app.channels.base import NormalisedMessage
from app.core.logging import get_logger
from app.db.client import get_db
from app.gateway.dedup import is_duplicate

logger = get_logger(__name__)


async def handle_inbound(msg: NormalisedMessage) -> None:
    """
    Entry point for all inbound messages from all channels.
    Called by the webhook route handlers after signature verification.
    """
    # ── Step 1: Deduplication ───────────────────────────────────────────────
    if await is_duplicate(msg):
        logger.info("gateway.skip.duplicate", key=msg.idempotency_key)
        return

    # ── Step 2: Resolve or create user ─────────────────────────────────────
    user_id = await get_or_create_user(msg.channel, msg.channel_user_id)

    logger.info(
        "gateway.dispatch",
        channel=msg.channel,
        channel_user_id=msg.channel_user_id,
        user_id=str(user_id),
        message_type=msg.message_type,
    )

    # ── Step 3: Dispatch to conversation layer ──────────────────────────────
    # Import here to avoid circular imports at module load time
    from app.conversation.dispatcher import dispatch

    await dispatch(user_id=user_id, msg=msg)


async def get_or_create_user(channel: str, channel_user_id: str) -> UUID:
    """
    Atomically get or create a user row.

    Uses a single Supabase upsert on the unique
    (channel, channel_user_id) key so concurrent deliveries are safe.

    Returns the user's UUID.
    """
    db = await get_db()

    # Single atomic upsert by unique key. On conflict, Supabase updates and returns the row.
    result = await (
        db.table("users")
        .upsert(
            {
                "channel": channel,
                "channel_user_id": channel_user_id,
                "timezone": "UTC",
                "style_prefs": {},
                "is_active": True,
            },
            on_conflict="channel,channel_user_id",
            ignore_duplicates=False,
            returning="representation",
        )
        .execute()
    )

    row = _extract_row(result)
    if row is None:
        # Defensive fallback in case PostgREST returns minimal/no representation.
        retry = await (
            db.table("users")
            .select("id")
            .eq("channel", channel)
            .eq("channel_user_id", channel_user_id)
            .single()
            .execute()
        )
        row = _extract_row(retry)
        if row is None:
            raise RuntimeError(
                "Failed to resolve user after upsert "
                f"for channel={channel} channel_user_id={channel_user_id}"
            )
        return UUID(row["id"])

    user_id = UUID(row["id"])
    logger.info(
        "gateway.user.created",
        channel=channel,
        channel_user_id=channel_user_id,
        user_id=str(user_id),
    )
    return user_id


def _extract_row(result: object) -> dict | None:
    """Safely normalize Supabase response to a single row dict."""
    if not result or not getattr(result, "data", None):
        return None

    data = result.data
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return data[0] if data else None
    return None
