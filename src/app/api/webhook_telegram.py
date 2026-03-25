"""
app/api/webhook_telegram.py
────────────────────────────
FastAPI router for Telegram webhook events.

Telegram calls POST /webhooks/telegram for every update.
We verify the signature, parse the update, and hand off to the gateway.
Always returns 200 — Telegram retries on non-200.
"""

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.channels.telegram import parse_telegram_update, verify_telegram_signature
from app.core.logging import get_logger
from app.gateway.router import handle_inbound

router = APIRouter()
logger = get_logger(__name__)


@router.post("/webhooks/telegram", status_code=status.HTTP_200_OK)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    """
    Receive Telegram webhook updates.

    Security: Telegram sends the secret token we set during setWebhook
    in the X-Telegram-Bot-Api-Secret-Token header.
    """
    body = await request.body()

    # ── Signature verification ───────────────────────────────────────────────
    if not verify_telegram_signature(body, x_telegram_bot_api_secret_token):
        logger.warning(
            "telegram.webhook.invalid_signature",
            client_host=request.client.host if request.client else "unknown",
        )
        # Return 200 to prevent Telegram from retrying a legitimately rejected request.
        # Log but don't expose the reason.
        return {"ok": True}

    # ── Parse payload ────────────────────────────────────────────────────────
    try:
        payload = await request.json()
    except Exception:
        logger.warning("telegram.webhook.invalid_json")
        return {"ok": True}

    msg = parse_telegram_update(payload)
    if msg is None:
        # Update type we don't handle (edited message, channel post, etc.)
        return {"ok": True}

    # ── Dispatch (async — gateway handles dedup, user resolve, conversation) ──
    try:
        await handle_inbound(msg)
    except Exception as exc:
        # Never let an exception propagate to Telegram — it would retry.
        logger.exception("telegram.webhook.dispatch_error", error=str(exc))

    return {"ok": True}
