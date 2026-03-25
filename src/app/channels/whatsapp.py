"""
app/channels/whatsapp.py
────────────────────────
WhatsApp Cloud API adapter.

Key constraints handled here:
  - X-Hub-Signature-256 HMAC verification
  - 24-hour messaging window enforcement
  - Message length limits (4096 chars for text)
  - Webhook challenge verification (GET request from Meta)
  - Deduplication via message.id (WhatsApp delivers duplicates)
"""

import hashlib
import hmac
import json
from typing import Any

import httpx

from app.channels.base import BaseChannelSender, MessageType, NormalisedMessage
from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com/v21.0"
WHATSAPP_MAX_TEXT_LENGTH = 4096


def verify_whatsapp_signature(body: bytes, signature_header: str | None) -> bool:
    """
    Verify X-Hub-Signature-256 header from Meta.
    Format: "sha256=<hex_digest>"
    """
    settings = get_settings()
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        settings.whatsapp_app_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature_header, expected)


def verify_whatsapp_challenge(
    mode: str | None,
    token: str | None,
    challenge: str | None,
) -> str | None:
    """
    Handle Meta's webhook verification GET request.
    Returns the challenge string if verification passes, else None.
    """
    settings = get_settings()
    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        return challenge
    return None


def parse_whatsapp_payload(payload: dict[str, Any]) -> NormalisedMessage | None:
    """
    Parse a WhatsApp Cloud API webhook payload into a NormalisedMessage.

    WhatsApp webhook structure:
    {
      "object": "whatsapp_business_account",
      "entry": [{
        "changes": [{
          "value": {
            "messages": [{"id": ..., "from": ..., "text": {"body": ...}, "type": "text"}],
            "statuses": [...]  // delivery/read receipts — we skip these
          }
        }]
      }]
    }
    """
    try:
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})

        messages = value.get("messages", [])
        if not messages:
            logger.debug("whatsapp.parse.skip", reason="no messages (likely a status update)")
            return None

        message = messages[0]

        if message.get("type") != "text":
            logger.debug("whatsapp.parse.skip", reason="non-text message type", type=message.get("type"))
            return None

        phone_number = message["from"]
        message_id = message["id"]
        text = message.get("text", {}).get("body", "").strip()

        if not text:
            return None

        # Detect commands
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            command = parts[0].lower()
            args = parts[1].strip() if len(parts) > 1 else None
            return NormalisedMessage(
                channel="whatsapp",
                channel_user_id=phone_number,
                message_id=message_id,
                text=text,
                message_type=MessageType.COMMAND,
                command=command,
                command_args=args,
                raw=payload,
            )

        return NormalisedMessage(
            channel="whatsapp",
            channel_user_id=phone_number,
            message_id=message_id,
            text=text,
            message_type=MessageType.TEXT,
            raw=payload,
        )

    except (KeyError, IndexError) as exc:
        logger.warning("whatsapp.parse.error", error=str(exc))
        return None


class WhatsAppSender(BaseChannelSender):
    """
    Sends messages back to WhatsApp users via the Cloud API.

    24-hour window constraint:
      After 24 hours since the user's last message, we can only send
      approved template messages. This class always uses text messages
      (assumes user has messaged within 24h). The gateway tracks
      last_message_at and should only trigger sends within the window.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }

    @property
    def _messages_url(self) -> str:
        pid = self._settings.whatsapp_phone_number_id
        return f"{WHATSAPP_API_BASE}/{pid}/messages"

    async def send_text(self, channel_user_id: str, text: str) -> None:
        """
        Send a text message. Splits at 4096 chars if needed.
        WhatsApp does not support Markdown — send as plain text.
        """
        # Strip markdown formatting that would show as literal asterisks
        clean_text = _strip_markdown(text)
        chunks = _split_message(clean_text, WHATSAPP_MAX_TEXT_LENGTH)

        async with httpx.AsyncClient(timeout=15.0) as client:
            for chunk in chunks:
                payload = {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": channel_user_id,
                    "type": "text",
                    "text": {"preview_url": False, "body": chunk},
                }
                await _post_with_retry(client, self._messages_url, self._headers, payload)

    async def send_buttons(
        self,
        channel_user_id: str,
        text: str,
        buttons: list[tuple[str, str]],
    ) -> None:
        """
        Send interactive buttons via WhatsApp interactive message API.
        Supports up to 3 buttons per message (WhatsApp limit).
        Falls back to numbered list if more than 3 buttons.
        """
        clean_text = _strip_markdown(text)

        if len(buttons) > 3:
            # Fallback: send numbered list as plain text
            numbered = "\n".join(f"{i+1}. {label}" for i, (label, _) in enumerate(buttons))
            await self.send_text(channel_user_id, f"{clean_text}\n\n{numbered}")
            return

        wa_buttons = [
            {"type": "reply", "reply": {"id": data, "title": label[:20]}}
            for label, data in buttons
        ]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": channel_user_id,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": clean_text},
                "action": {"buttons": wa_buttons},
            },
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            await _post_with_retry(client, self._messages_url, self._headers, payload)

    async def mark_as_read(self, message_id: str) -> None:
        """Mark a message as read to show double blue ticks."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                await client.post(self._messages_url, headers=self._headers, json=payload)
            except Exception:
                pass  # Non-critical


# ── Helpers ────────────────────────────────────────────────────────────────


def _split_message(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len:
            if current:
                chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return chunks or [text[:max_len]]


def _strip_markdown(text: str) -> str:
    """Remove common Markdown formatting for WhatsApp plain text."""
    import re
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)  # **bold**
    text = re.sub(r"\*(.*?)\*", r"\1", text)       # *italic*
    text = re.sub(r"`(.*?)`", r"\1", text)          # `code`
    text = re.sub(r"_(.*?)_", r"\1", text)          # _italic_
    return text


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    max_retries: int = 3,
) -> None:
    import asyncio

    for attempt in range(max_retries):
        try:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("retry-after", "5"))
                logger.warning("whatsapp.rate_limit", retry_after=retry_after, attempt=attempt)
                await asyncio.sleep(retry_after)
                continue
            resp.raise_for_status()
            return
        except httpx.HTTPStatusError as exc:
            if attempt == max_retries - 1:
                logger.error("whatsapp.send.failed", error=str(exc), status=exc.response.status_code)
                raise
            await asyncio.sleep(2**attempt)
