"""
app/channels/base.py
────────────────────
The NormalisedMessage dataclass is the single contract that every
channel adapter (Telegram, WhatsApp, future channels) must produce.

Once a raw webhook payload is parsed into a NormalisedMessage,
all downstream code is channel-agnostic.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MessageType(StrEnum):
    TEXT = "text"
    COMMAND = "command"   # /start, /cancel, etc.
    CALLBACK = "callback" # inline button presses (Telegram)


@dataclass
class NormalisedMessage:
    """
    Channel-agnostic representation of an inbound user message.

    Fields
    ------
    channel         : 'telegram' | 'whatsapp'
    channel_user_id : Stable user identifier within the channel
                      (Telegram: str(chat_id), WhatsApp: phone number)
    message_id      : Unique message identifier within the channel
                      Used as idempotency key: f"{channel}:{message_id}"
    text            : The message text, stripped of leading/trailing whitespace
    message_type    : TEXT | COMMAND | CALLBACK
    command         : The command string if message_type == COMMAND, e.g. '/start'
    command_args    : Arguments after the command, e.g. '/cancel abc123' → 'abc123'
    raw             : Full original payload for debugging / logging
    """

    channel: str
    channel_user_id: str
    message_id: str
    text: str
    message_type: MessageType = MessageType.TEXT
    command: str | None = None
    command_args: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        return f"{self.channel}:{self.message_id}"

    def is_command(self, name: str) -> bool:
        """Check if this message is a specific command, e.g. is_command('/start')."""
        return self.message_type == MessageType.COMMAND and self.command == name


class BaseChannelSender:
    """
    Abstract base for sending messages back to a channel.
    Each channel adapter implements this and registers itself.
    """

    async def send_text(self, channel_user_id: str, text: str) -> None:
        raise NotImplementedError

    async def send_buttons(
        self,
        channel_user_id: str,
        text: str,
        buttons: list[tuple[str, str]],  # (label, callback_data)
    ) -> None:
        """Send a message with quick-reply buttons (Telegram inline keyboard / WA buttons)."""
        raise NotImplementedError
