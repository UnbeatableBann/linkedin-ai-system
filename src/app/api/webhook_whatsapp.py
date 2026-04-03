"""
app/api/webhook_whatsapp.py
────────────────────────────
FastAPI router for WhatsApp Cloud API webhook events.

Two endpoints:
  GET  /webhooks/whatsapp — Meta's verification challenge (one-time setup)
  POST /webhooks/whatsapp — Inbound messages and status updates
"""

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import PlainTextResponse

from app.channels.whatsapp import (
    parse_whatsapp_payload,
    verify_whatsapp_challenge,
    verify_whatsapp_signature,
)
from app.core.logging import get_logger
from app.gateway.router import handle_inbound

router = APIRouter()
logger = get_logger(__name__)


@router.get("/webhooks/whatsapp")
async def whatsapp_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """
    Meta webhook verification challenge.
    Called once when you register the webhook in Meta Developer Console.
    Must return the challenge string as plain text with 200.
    """
    challenge = verify_whatsapp_challenge(hub_mode, hub_verify_token, hub_challenge)
    if challenge:
        logger.info("whatsapp.webhook.verified")
        return PlainTextResponse(content=challenge, status_code=status.HTTP_200_OK)

    logger.warning("whatsapp.webhook.verification_failed", mode=hub_mode)
    return PlainTextResponse(content="Forbidden", status_code=status.HTTP_403_FORBIDDEN)


@router.post("/webhooks/whatsapp", status_code=status.HTTP_200_OK)
async def whatsapp_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    """
    Receive WhatsApp Cloud API webhook events.

    Security: Meta sends X-Hub-Signature-256 header for all POST events.
    We verify it before processing.

    Always returns 200 — Meta retries on non-200.
    """
    body = await request.body()

    # ── Signature verification ───────────────────────────────────────────────
    if not verify_whatsapp_signature(body, x_hub_signature_256):
        logger.warning(
            "whatsapp.webhook.invalid_signature",
            client_host=request.client.host if request.client else "unknown",
        )
        return {"status": "ok"}

    # ── Parse payload ────────────────────────────────────────────────────────
    try:
        payload = await request.json()
    except Exception:
        logger.warning("whatsapp.webhook.invalid_json")
        return {"status": "ok"}

    msg = parse_whatsapp_payload(payload)
    if msg is None:
        # Status update (delivery/read receipts), not a message — ignore
        return {"status": "ok"}

    # ── Mark as read (shows double blue ticks to user) ───────────────────────
    from app.channels.whatsapp import WhatsAppSender

    try:
        sender = WhatsAppSender()
        await sender.mark_as_read(msg.message_id)
    except Exception:
        pass  # Non-critical

    # ── Dispatch ─────────────────────────────────────────────────────────────
    try:
        await handle_inbound(msg)
    except Exception as exc:
        logger.exception("whatsapp.webhook.dispatch_error", error=str(exc))

    return {"status": "ok"}
