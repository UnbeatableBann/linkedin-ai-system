"""
app/channels/telegram.py
────────────────────────
Telegram Bot API adapter.

Responsibilities:
  - Verify HMAC-SHA256 webhook signature
  - Parse Update objects into NormalisedMessage
  - Send text messages, inline keyboard buttons, typing indicators
  - Respect Telegram's 4096 char limit and 1 msg/s flood control
"""

import hmac
from typing import Any

import httpx

from app.channels.base import BaseChannelSender, MessageType, NormalisedMessage
from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Telegram enforces 4096 char limit per message
TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def verify_telegram_signature(
    body: bytes,
    secret_token_header: str | None,
) -> bool:
    """
    Verify the X-Telegram-Bot-Api-Secret-Token header.
    Telegram sends this header with every webhook request when a secret_token
    was provided during setWebhook. Value must match exactly.
    """
    settings = get_settings()
    if secret_token_header is None:
        return False
    return hmac.compare_digest(
        secret_token_header,
        settings.telegram_webhook_secret,
    )


def parse_telegram_update(payload: dict[str, Any]) -> NormalisedMessage | None:
    """
    Parse a Telegram Update object into a NormalisedMessage.
    Returns None for updates we don't handle (e.g. channel posts, edited messages).

    Handles:
      - Regular text messages
      - Bot commands (/start, /cancel, etc.)
      - Callback queries (inline button presses)
    """
    # ── Callback query (button press) ──────────────────────────────────────
    if "callback_query" in payload:
        cq = payload["callback_query"]
        chat_id = str(cq["from"]["id"])
        return NormalisedMessage(
            channel="telegram",
            channel_user_id=chat_id,
            message_id=f"cq_{cq['id']}",
            text=cq.get("data", ""),
            message_type=MessageType.CALLBACK,
            raw=payload,
        )

    # ── Regular message ─────────────────────────────────────────────────────
    message = payload.get("message")
    if not message:
        logger.debug("telegram.parse.skip", reason="no message in update")
        return None

    chat_id = str(message["chat"]["id"])
    text: str = message.get("text", "").strip()

    if not text:
        # Non-text message (photo, sticker, etc.) — ignore for now
        logger.debug("telegram.parse.skip", reason="non-text message", chat_id=chat_id)
        return None

    message_id = str(message["message_id"])

    # ── Detect commands (/start, /cancel, /help, etc.) ─────────────────────
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        # Strip the @botname suffix Telegram appends in groups: /start@MyBot → /start
        command = parts[0].split("@")[0].lower()
        args = parts[1].strip() if len(parts) > 1 else None
        return NormalisedMessage(
            channel="telegram",
            channel_user_id=chat_id,
            message_id=message_id,
            text=text,
            message_type=MessageType.COMMAND,
            command=command,
            command_args=args,
            raw=payload,
        )

    # ── Plain text ──────────────────────────────────────────────────────────
    return NormalisedMessage(
        channel="telegram",
        channel_user_id=chat_id,
        message_id=message_id,
        text=text,
        message_type=MessageType.TEXT,
        raw=payload,
    )


class TelegramSender(BaseChannelSender):
    """
    Sends messages back to Telegram users.
    Uses httpx for async HTTP calls to the Telegram Bot API.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._base_url = self._settings.telegram_api_url

    async def send_text(self, channel_user_id: str, text: str) -> None:
        """
        Send a plain text message. Automatically splits messages that exceed
        Telegram's 4096 character limit.
        Parses Markdown so we can use *bold*, _italic_, `code`.
        """
        chunks = _split_message(text, TELEGRAM_MAX_MESSAGE_LENGTH)
        async with httpx.AsyncClient(timeout=10.0) as client:
            for chunk in chunks:
                payload = {
                    "chat_id": channel_user_id,
                    "text": chunk,
                    "parse_mode": "Markdown",
                }
                try:
                    await _post_with_retry(
                        client,
                        f"{self._base_url}/sendMessage",
                        payload,
                    )
                except httpx.HTTPStatusError as exc:
                    if not _should_retry_without_parse_mode(exc, payload):
                        raise

                    logger.warning(
                        "telegram.send.markdown_fallback",
                        chat_id=channel_user_id,
                        status=exc.response.status_code,
                    )
                    plain_payload = dict(payload)
                    plain_payload.pop("parse_mode", None)
                    try:
                        await _post_with_retry(
                            client,
                            f"{self._base_url}/sendMessage",
                            plain_payload,
                        )
                    except httpx.HTTPStatusError as plain_exc:
                        logger.error(
                            "telegram.send.plain_fallback_failed",
                            chat_id=channel_user_id,
                            status=plain_exc.response.status_code,
                            description=_extract_telegram_error_description(plain_exc.response),
                        )
                        await _post_with_retry(
                            client,
                            f"{self._base_url}/sendMessage",
                            {
                                "chat_id": channel_user_id,
                                "text": "I could not render one of my messages. Please send any message to continue.",
                            },
                        )

    async def send_buttons(
        self,
        channel_user_id: str,
        text: str,
        buttons: list[tuple[str, str]],
    ) -> None:
        """
        Send a message with an inline keyboard.
        buttons: list of (label, callback_data) pairs.
        Each button gets its own row for clarity.
        """
        keyboard = {"inline_keyboard": [[{"text": label, "callback_data": data}] for label, data in buttons]}
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "chat_id": channel_user_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            }
            try:
                await _post_with_retry(
                    client,
                    f"{self._base_url}/sendMessage",
                    payload,
                )
            except httpx.HTTPStatusError as exc:
                if not _should_retry_without_parse_mode(exc, payload):
                    raise

                logger.warning(
                    "telegram.send_buttons.markdown_fallback",
                    chat_id=channel_user_id,
                    status=exc.response.status_code,
                )
                plain_payload = dict(payload)
                plain_payload.pop("parse_mode", None)
                await _post_with_retry(
                    client,
                    f"{self._base_url}/sendMessage",
                    plain_payload,
                )

    async def send_url_button(
        self,
        channel_user_id: str,
        text: str,
        label: str,
        url: str,
    ) -> None:
        """Send a message with a single URL button."""
        keyboard = {"inline_keyboard": [[{"text": label, "url": url}]]}
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "chat_id": channel_user_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            }
            try:
                await _post_with_retry(
                    client,
                    f"{self._base_url}/sendMessage",
                    payload,
                )
            except httpx.HTTPStatusError as exc:
                if not _should_retry_without_parse_mode(exc, payload):
                    raise

                logger.warning(
                    "telegram.send_url_button.markdown_fallback",
                    chat_id=channel_user_id,
                    status=exc.response.status_code,
                )
                plain_payload = dict(payload)
                plain_payload.pop("parse_mode", None)
                await _post_with_retry(
                    client,
                    f"{self._base_url}/sendMessage",
                    plain_payload,
                )

    async def send_typing(self, channel_user_id: str) -> None:
        """Show 'typing...' indicator while the LLM is generating."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                await client.post(
                    f"{self._base_url}/sendChatAction",
                    json={"chat_id": channel_user_id, "action": "typing"},
                )
            except Exception:
                pass  # Typing indicator failures are non-critical

    async def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        """Acknowledge a callback query to remove the loading spinner from the button."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                await client.post(
                    f"{self._base_url}/answerCallbackQuery",
                    json={"callback_query_id": callback_query_id, "text": text},
                )
            except Exception:
                pass


# ── Helpers ────────────────────────────────────────────────────────────────


def _split_message(text: str, max_len: int) -> list[str]:
    """Split text into chunks that never exceed Telegram's max message length."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    soft_cut_threshold = max_len // 2

    while len(remaining) > max_len:
        window = remaining[:max_len]
        cut = window.rfind("\n")
        if cut >= soft_cut_threshold:
            chunk = remaining[:cut]
            remaining = remaining[cut + 1 :]
        else:
            chunk = window
            remaining = remaining[max_len:]

        if chunk:
            chunks.append(chunk)

    if remaining:
        chunks.append(remaining)

    return chunks


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    max_retries: int = 3,
) -> None:
    """
    POST to Telegram API with retry on flood control (429).
    Respects the retry_after value from Telegram's response.
    """
    import asyncio

    for attempt in range(max_retries):
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                logger.warning("telegram.flood_control", retry_after=retry_after, attempt=attempt)
                await asyncio.sleep(retry_after)
                continue
            resp.raise_for_status()
            return
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if 400 <= status_code < 500 and status_code != 429:
                log_fn = logger.error
                event = "telegram.send.client_error"
                if _is_markdown_entity_error(exc, payload):
                    log_fn = logger.warning
                    event = "telegram.send.markdown_entity_error"

                log_fn(
                    event,
                    error=str(exc),
                    url=url,
                    description=_extract_telegram_error_description(exc.response),
                    payload_len=len(str(payload.get("text", ""))),
                    parse_mode=payload.get("parse_mode"),
                )
                raise
            if attempt == max_retries - 1:
                logger.error("telegram.send.failed", error=str(exc), url=url)
                raise
            await asyncio.sleep(2**attempt)


def _should_retry_without_parse_mode(
    exc: httpx.HTTPStatusError,
    payload: dict[str, Any],
) -> bool:
    """Retry once as plain text when Markdown-formatted sends fail with 400."""
    return bool(payload.get("parse_mode")) and exc.response.status_code == 400


def _is_markdown_entity_error(
    exc: httpx.HTTPStatusError,
    payload: dict[str, Any],
) -> bool:
    """Detect Telegram markdown parsing failures that should be treated as recoverable."""
    return (
        bool(payload.get("parse_mode"))
        and exc.response.status_code == 400
        and "can't parse entities" in _extract_telegram_error_description(exc.response).lower()
    )


def _extract_telegram_error_description(response: httpx.Response) -> str:
    """Best-effort parse of Telegram error detail for diagnostics."""
    try:
        body = response.json()
    except Exception:
        return response.text[:300]

    return str(body.get("description", "")).strip()[:300]
