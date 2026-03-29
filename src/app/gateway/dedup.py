"""
app/gateway/dedup.py
────────────────────
Webhook deduplication using the webhook_log table.

Both Telegram and WhatsApp can deliver the same webhook multiple times
(network retries, their internal retry logic). This module ensures each
message is processed exactly once.

Strategy: INSERT the idempotency key before processing. If a UNIQUE
violation occurs, the message was already processed — skip silently.
"""

from app.channels.base import NormalisedMessage
from app.core.logging import get_logger
from app.db.client import get_db

logger = get_logger(__name__)


async def is_duplicate(msg: NormalisedMessage) -> bool:
    """
    Attempt to insert the message's idempotency key into webhook_log.

    Returns True  → message is a duplicate, skip processing.
    Returns False → message is new, safe to process.

    This is intentionally a "mark then check" pattern (not "check then mark")
    to be safe under concurrent delivery.
    """
    db = await get_db()
    key = msg.idempotency_key

    try:
        await (
            db.table("webhook_log")
            .insert(
                {
                    "idempotency_key": key,
                    "channel": msg.channel,
                    "user_channel_id": msg.channel_user_id,
                    "payload": msg.raw,
                }
            )
            .execute()
        )
        logger.debug("dedup.new", key=key)
        return False  # New message — process it

    except Exception as exc:
        error_str = str(exc).lower()
        if "unique" in error_str or "duplicate" in error_str or "23505" in error_str:
            logger.info("dedup.duplicate", key=key)
            return True  # Already processed
        # Unexpected error — log but don't block processing
        logger.error("dedup.unexpected_error", key=key, error=str(exc))
        return False
