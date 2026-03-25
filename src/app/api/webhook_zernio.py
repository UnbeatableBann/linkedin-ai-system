"""
app/api/webhook_zernio.py
──────────────────────────
Receives publish status callbacks from Zernio.

Zernio can POST to this endpoint when a post's status changes:
  - published  → post went live on LinkedIn
  - failed     → publish attempt failed
  - cancelled  → post was cancelled

This is optional but gives us real-time status without polling.
Register it in your Zernio dashboard under Settings → Webhooks.
"""

from fastapi import APIRouter, Request, status
from app.core.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/webhooks/zernio", status_code=status.HTTP_200_OK)
async def zernio_webhook(request: Request) -> dict:
    """
    Receive Zernio publish callbacks.

    Payload shape (Zernio docs):
    {
      "event": "post.published" | "post.failed" | "post.cancelled",
      "post": {
        "_id": "zernio_post_id",
        "status": "published" | "failed" | "cancelled",
        "publishedAt": "2024-04-07T09:00:00Z"
      }
    }
    """
    try:
        payload = await request.json()
    except Exception:
        logger.warning("zernio_webhook.invalid_json")
        return {"ok": True}

    event = payload.get("event", "")
    post_data = payload.get("post", {})
    zernio_post_id = post_data.get("_id")

    if not zernio_post_id:
        return {"ok": True}

    logger.info("zernio_webhook.received", event=event, zernio_post_id=zernio_post_id)

    try:
        await _handle_zernio_event(event, zernio_post_id, post_data)
    except Exception as exc:
        logger.error("zernio_webhook.handler_error", error=str(exc))

    return {"ok": True}


async def _handle_zernio_event(
    event: str, zernio_post_id: str, post_data: dict
) -> None:
    from datetime import datetime, timezone
    from app.db.client import get_db

    db = get_db()

    # Find our post by zernio_post_id
    result = (
        db.table("posts")
        .select("id, user_id, status")
        .eq("zernio_post_id", zernio_post_id)
        .maybe_single()
        .execute()
    )

    if not result.data:
        logger.warning(
            "zernio_webhook.post_not_found",
            zernio_post_id=zernio_post_id
        )
        return

    post = result.data
    post_id = post["id"]
    user_id = post["user_id"]

    if event == "post.published":
        published_at = post_data.get("publishedAt") or datetime.now(timezone.utc).isoformat()
        db.table("posts").update(
            {"status": "published", "published_at": published_at}
        ).eq("id", post_id).execute()
        logger.info("zernio_webhook.post_published", post_id=post_id)

    elif event == "post.failed":
        error_msg = post_data.get("error", "Unknown error from Zernio")
        db.table("posts").update(
            {"status": "failed", "metadata": {"zernio_error": error_msg}}
        ).eq("id", post_id).execute()
        logger.warning("zernio_webhook.post_failed", post_id=post_id, error=error_msg)

        # Notify the user
        await _notify_user_of_failure(user_id, post_id, error_msg)

    elif event == "post.cancelled":
        db.table("posts").update({"status": "cancelled"}).eq("id", post_id).execute()
        logger.info("zernio_webhook.post_cancelled", post_id=post_id)


async def _notify_user_of_failure(user_id: str, post_id: str, error: str) -> None:
    """Send a failure notification back to the user via their channel."""
    from app.db.client import get_db
    from app.db.models import UserRow

    db = get_db()
    result = (
        db.table("users")
        .select("channel, channel_user_id")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        return

    user_data = result.data
    channel = user_data["channel"]
    channel_user_id = user_data["channel_user_id"]

    if channel == "telegram":
        from app.channels.telegram import TelegramSender
        sender = TelegramSender()
    else:
        from app.channels.whatsapp import WhatsAppSender
        sender = WhatsAppSender()

    await sender.send_text(
        channel_user_id,
        f"❌ Your LinkedIn post failed to publish.\n\n"
        f"Error: {error[:200]}\n\n"
        f"Post ID: `{post_id[:8]}`\n\n"
        "Check your LinkedIn connection with /reconnect, then try rescheduling.",
    )
