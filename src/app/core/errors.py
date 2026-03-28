"""
app/core/errors.py
───────────────────
Centralised error types for the application.

All errors that reach the user should have a user_message() method
that returns something friendly — no stack traces, no internal names.

Error hierarchy:
  AppError                   — base
    ├── ConfigurationError   — missing/invalid setup (onboarding incomplete)
    ├── ChannelError         — can't reach Telegram/WhatsApp
    ├── LLMError             — from llm_client.py (re-exported here)
    ├── ZernioError          — from zernio/client.py (re-exported here)
    ├── SessionError         — session not found / expired
    └── RateLimitExceeded    — from rate_limiter.py (re-exported here)
"""

from app.content.llm_client import LLMError
from app.core.rate_limiter import RateLimitExceeded
from app.zernio.client import ZernioError


class AppError(Exception):
    """Base class for all application errors."""

    def user_message(self) -> str:
        return "Something went wrong. Please try again or /cancel to start fresh."


class ConfigurationError(AppError):
    """Raised when the user's account is not properly set up."""

    def __init__(self, missing: str) -> None:
        self.missing = missing
        super().__init__(f"Configuration missing: {missing}")

    def user_message(self) -> str:
        messages = {
            "zernio_key": "Your Zernio API key is not set up. Run /start to connect.",
            "llm_key": "Your AI provider is not configured. Run /settings to set it up.",
            "linkedin": "Your LinkedIn account is not connected. Run /reconnect.",
            "timezone": "Your timezone is not set. Use /timezone Asia/Kolkata to set it.",
        }
        return messages.get(self.missing, f"Setup incomplete: {self.missing}. Run /start.")


class ChannelError(AppError):
    """Raised when we can't send a message back to the user's channel."""

    def user_message(self) -> str:
        return "I couldn't send you a message. Please check if the bot is still active."


class SessionError(AppError):
    """Raised for session-related issues."""

    def user_message(self) -> str:
        return "Your session expired. Send any message to start a new one."


def format_error_for_user(exc: Exception) -> str:
    """
    Convert any exception into a user-friendly message.
    Used as a last resort in the dispatcher's except block.
    """
    if isinstance(exc, AppError):
        return exc.user_message()
    if isinstance(exc, LLMError):
        return str(exc)
    if isinstance(exc, ZernioError):
        return str(exc)
    if isinstance(exc, RateLimitExceeded):
        return exc.user_message()

    # Generic fallback — never expose internals
    return (
        "Something unexpected happened. Your draft is safe.\n\n"
        "Send a message to continue, or /cancel to start fresh."
    )


# TODO:
# 1. You’re leaking raw messages from LLM/Zernio
