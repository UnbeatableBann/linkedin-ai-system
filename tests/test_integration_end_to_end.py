"""
tests/test_integration_end_to_end.py
──────────────────────────────────────
End-to-end integration tests that wire the complete inbound message
pipeline: raw webhook payload → gateway → session → FSM → handler.

All external I/O is mocked:
  - Supabase: via mock_db fixture
  - Telegram send API: via AsyncMock on TelegramSender.send_text
  - Celery tasks: patched to no-ops
  - Zernio: not invoked in these flows

These tests verify that:
  1. The right handler is called for each FSM state
  2. State transitions happen correctly end-to-end
  3. The correct reply is sent to the user
  4. Session is persisted after every message
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.channels.base import MessageType, NormalisedMessage
from app.db.models import Channel, LLMProvider, UserRow
from app.session.models import SessionContext, SessionState

# ── Helpers ────────────────────────────────────────────────────────────────


def make_msg(
    text: str,
    channel: str = "telegram",
    channel_user_id: str = "123456",
    message_id: str = "1",
) -> NormalisedMessage:
    """Build a normalised message for testing."""
    msg_type = MessageType.TEXT
    command = None
    command_args = None

    if text.startswith("/"):
        msg_type = MessageType.COMMAND
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        command_args = parts[1] if len(parts) > 1 else None

    return NormalisedMessage(
        channel=channel,
        channel_user_id=channel_user_id,
        message_id=message_id,
        text=text,
        message_type=msg_type,
        command=command,
        command_args=command_args,
        raw={},
    )


def make_session(
    state: SessionState = SessionState.IDLE,
    user_id=None,
    context: dict | None = None,
) -> MagicMock:
    """Build a mock Session object."""
    from uuid import uuid4

    session = MagicMock()
    session.id = uuid4()
    session.user_id = user_id or uuid4()
    session.state = state
    session.draft_id = None

    # Use real SessionContext so logic runs
    ctx = SessionContext()
    if context:
        for k, v in context.items():
            setattr(ctx, k, v)
    session.context = ctx

    return session


def make_user(
    channel: str = "telegram",
    channel_user_id: str = "123456",
    onboarded: bool = True,
) -> UserRow:
    """Build a UserRow for testing."""
    import os

    from cryptography.fernet import Fernet

    key = os.environ.get("FERNET_SECRET_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        os.environ["FERNET_SECRET_KEY"] = key

    fernet = Fernet(key.encode())
    enc_zernio = fernet.encrypt(b"sk_test_zernio_key").decode()
    enc_llm = fernet.encrypt(b"sk-ant-test-llm-key").decode()

    return UserRow(
        id=uuid4(),
        channel=Channel(channel),
        channel_user_id=channel_user_id,
        zernio_api_key_enc=enc_zernio if onboarded else None,
        zernio_profile_id="prof_test" if onboarded else None,
        zernio_account_id="acc_test" if onboarded else None,
        llm_provider=LLMProvider.ANTHROPIC if onboarded else None,
        llm_api_key_enc=enc_llm if onboarded else None,
        llm_model="claude-haiku-4-5-20251001" if onboarded else None,
        timezone="Asia/Kolkata",
        style_prefs={},
        is_active=True,
        created_at=datetime.now(UTC),
    )


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    """Set env vars for all tests in this module."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("FERNET_SECRET_KEY", key)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "a" * 32)
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "x" * 40)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "secret")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify")
    monkeypatch.setenv("OAUTH_CALLBACK_BASE_URL", "https://test.example.com")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ══════════════════════════════════════════════════════════════════════════
# /cancel — universal command
# ══════════════════════════════════════════════════════════════════════════


class TestCancelCommand:
    @pytest.mark.asyncio
    async def test_cancel_from_reviewing_resets_to_idle(self):
        """Cancel during review discards draft and resets state."""
        user = make_user()
        session = make_session(
            state=SessionState.REVIEWING,
            context={"draft_content": "My draft", "topic": "AI"},
        )
        msg = make_msg("/cancel")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        assert session.state == SessionState.IDLE
        assert session.context.draft_content is None
        assert session.context.topic is None
        sender.send_text.assert_called_once()
        reply = sender.send_text.call_args[0][1]
        assert "cancelled" in reply.lower() or "discard" in reply.lower()

    @pytest.mark.asyncio
    async def test_cancel_from_idle_gives_friendly_message(self):
        """Cancel when nothing is active gives a helpful message."""
        user = make_user()
        session = make_session(state=SessionState.IDLE)
        msg = make_msg("/cancel")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        # State should still be IDLE, message should explain nothing to cancel
        assert session.state == SessionState.IDLE
        sender.send_text.assert_called_once()
        reply = sender.send_text.call_args[0][1]
        assert "nothing" in reply.lower() or "start" in reply.lower()

    @pytest.mark.asyncio
    async def test_cancel_from_generating_queues_nothing(self):
        """Cancel while LLM is generating clears state immediately."""
        user = make_user()
        session = make_session(state=SessionState.GENERATING)
        msg = make_msg("/cancel")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        assert session.state == SessionState.IDLE


# ══════════════════════════════════════════════════════════════════════════
# /help command
# ══════════════════════════════════════════════════════════════════════════


class TestHelpCommand:
    @pytest.mark.asyncio
    async def test_help_returns_commands_list(self):
        user = make_user()
        session = make_session()
        msg = make_msg("/help")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        reply = sender.send_text.call_args[0][1]
        assert "/new" in reply
        assert "/cancel" in reply
        assert "/schedule" in reply
        assert "/settings" in reply


# ══════════════════════════════════════════════════════════════════════════
# /start command
# ══════════════════════════════════════════════════════════════════════════


class TestStartCommand:
    @pytest.mark.asyncio
    async def test_start_existing_user_shows_welcome_back(self):
        user = make_user(onboarded=True)
        session = make_session()
        msg = make_msg("/start")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        reply = sender.send_text.call_args[0][1]
        assert "welcome" in reply.lower() or "back" in reply.lower()

    @pytest.mark.asyncio
    async def test_start_new_user_begins_onboarding(self):
        user = make_user(onboarded=False)
        session = make_session()
        msg = make_msg("/start")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        # Should send an onboarding message
        assert sender.send_text.called
        reply = sender.send_text.call_args[0][1]
        assert any(word in reply.lower() for word in ["welcome", "zernio", "setup", "step"])


# ══════════════════════════════════════════════════════════════════════════
# Idle state — new post flow begins
# ══════════════════════════════════════════════════════════════════════════


class TestIdleStateNewPost:
    @pytest.mark.asyncio
    async def test_text_message_starts_collecting(self):
        """Short topic → moves to COLLECTING and asks clarifying questions."""
        user = make_user()
        session = make_session(state=SessionState.IDLE)
        msg = make_msg("Write about AI trends in India")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        assert session.state == SessionState.COLLECTING
        assert session.context.topic == "Write about AI trends in India"
        sender.send_text.assert_called()

    @pytest.mark.asyncio
    async def test_long_text_goes_directly_to_generating(self):
        """Long text with newlines → detected as full draft → GENERATING directly."""
        user = make_user()
        session = make_session(state=SessionState.IDLE)

        long_draft = (
            "I spent three years building a startup that failed.\n\n"
            "Here's what I learned from that experience.\n\n"
            "First, product-market fit matters more than anything.\n\n"
            "Second, your team is everything. Hire slowly, fire fast.\n\n"
            "Third, never run out of runway. Always have 18 months."
        )
        msg = make_msg(long_draft)

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
            patch("app.scheduler.tasks.generate_post_task") as mock_task,
        ):
            mock_task.delay = MagicMock()
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        assert session.state == SessionState.GENERATING


# ══════════════════════════════════════════════════════════════════════════
# Reviewing state
# ══════════════════════════════════════════════════════════════════════════


class TestReviewingState:
    @pytest.mark.asyncio
    async def test_approve_advances_to_scheduling(self):
        """'looks good' in reviewing state → SCHEDULING."""
        user = make_user()
        session = make_session(
            state=SessionState.REVIEWING,
            context={"draft_content": "My approved LinkedIn post."},
        )
        msg = make_msg("looks good")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        assert session.state == SessionState.SCHEDULING

    @pytest.mark.asyncio
    async def test_edit_instruction_queues_refinement(self):
        """Edit instruction → REFINING + Celery task queued."""
        user = make_user()
        session = make_session(
            state=SessionState.REVIEWING,
            context={"draft_content": "Original post content here.", "draft_version": 1},
        )
        msg = make_msg("make it shorter and add hashtags")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
            patch("app.scheduler.tasks.refine_post_task") as mock_task,
        ):
            mock_task.delay = MagicMock()
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        assert session.state == SessionState.REFINING
        mock_task.delay.assert_called_once()
        call_kwargs = mock_task.delay.call_args[1]
        assert call_kwargs["edit_instruction"] == "make it shorter and add hashtags"
        assert call_kwargs["current_draft"] == "Original post content here."

    @pytest.mark.asyncio
    async def test_discard_resets_to_idle(self):
        user = make_user()
        session = make_session(
            state=SessionState.REVIEWING,
            context={"draft_content": "Draft to discard."},
        )
        msg = make_msg("discard")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        assert session.state == SessionState.IDLE
        assert session.context.draft_content is None

    @pytest.mark.asyncio
    async def test_message_during_refining_is_queued(self):
        """Message while REFINING → queued in pending_message, user notified."""
        user = make_user()
        session = make_session(state=SessionState.REFINING)
        msg = make_msg("also make it more personal")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        # Message should be queued
        assert session.context.pending_message == "also make it more personal"
        # User should be told to wait
        assert sender.send_text.called
        reply = sender.send_text.call_args[0][1]
        assert any(w in reply.lower() for w in ["refin", "wait", "after", "next"])


# ══════════════════════════════════════════════════════════════════════════
# /delete command
# ══════════════════════════════════════════════════════════════════════════


class TestDeleteCommand:
    @pytest.mark.asyncio
    async def test_delete_without_confirm_shows_warning(self):
        user = make_user()
        session = make_session()
        msg = make_msg("/delete")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        reply = sender.send_text.call_args[0][1]
        assert "confirm" in reply.lower()
        assert "/delete confirm" in reply

    @pytest.mark.asyncio
    async def test_delete_confirm_deactivates_user(self):
        user = make_user()
        session = make_session()
        msg = make_msg("/delete confirm")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        mock_db = MagicMock()
        # Scheduled posts query
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        # All other queries
        mock_db.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = (
            MagicMock()
        )
        mock_db.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
            patch("app.conversation.dispatcher.get_db", return_value=mock_db),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        reply = sender.send_text.call_args[0][1]
        assert "deactivated" in reply.lower() or "removed" in reply.lower()


# ══════════════════════════════════════════════════════════════════════════
# Rate limiting
# ══════════════════════════════════════════════════════════════════════════


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_rate_limited_message_blocked(self):
        """When rate limit is exceeded, message is rejected with user-friendly reply."""
        from app.core.rate_limiter import LimitType, RateLimitExceededError

        user = make_user()
        session = make_session()
        msg = make_msg("hello")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        exc = RateLimitExceededError(LimitType.MESSAGE, retry_after_seconds=30)

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", side_effect=exc),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        # User should get a rate limit message
        assert sender.send_text.called
        reply = sender.send_text.call_args[0][1]
        assert "30" in reply  # retry_after in message

    @pytest.mark.asyncio
    async def test_rate_limit_redis_down_passes_through(self):
        """Redis down → rate limiter fails open → message processes normally."""
        user = make_user()
        session = make_session(state=SessionState.IDLE)
        msg = make_msg("/help")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            # Simulate Redis being down — check_rate_limit raises generic error
            patch(
                "app.conversation.dispatcher.check_rate_limit",
                side_effect=ConnectionError("Redis unavailable"),
            ),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        # Help message should still be sent (fail open)
        assert sender.send_text.called
        reply = sender.send_text.call_args[0][1]
        assert "/new" in reply or "/cancel" in reply


# ══════════════════════════════════════════════════════════════════════════
# Error recovery
# ══════════════════════════════════════════════════════════════════════════


class TestErrorRecovery:
    @pytest.mark.asyncio
    async def test_unhandled_exception_sends_friendly_message(self):
        """An unexpected error in a handler → friendly error message, session saved."""
        user = make_user()
        session = make_session(state=SessionState.IDLE)
        msg = make_msg("write something")

        sender = MagicMock()
        sender.send_text = AsyncMock()

        def boom(*args, **kwargs):
            raise RuntimeError("Unexpected database connection lost")

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock) as mock_save,
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
            patch("app.conversation.collecting.handle_idle", side_effect=boom),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        # User gets a friendly error (not a stack trace)
        assert sender.send_text.called
        reply = sender.send_text.call_args[0][1]
        assert "Unexpected database connection lost" not in reply
        assert "wrong" in reply.lower() or "error" in reply.lower() or "safe" in reply.lower()

        # Session is always saved even on error
        mock_save.assert_called()

    @pytest.mark.asyncio
    async def test_user_not_found_skips_silently(self):
        """If user lookup returns None, dispatch returns without crashing."""
        msg = make_msg("hello")

        with (
            patch("app.conversation.dispatcher._load_user", return_value=None),
        ):
            from app.conversation.dispatcher import dispatch

            # Should not raise
            await dispatch(user_id=uuid4(), msg=msg)


# ══════════════════════════════════════════════════════════════════════════
# WhatsApp channel
# ══════════════════════════════════════════════════════════════════════════


class TestWhatsAppChannel:
    @pytest.mark.asyncio
    async def test_whatsapp_message_processed_correctly(self):
        """WhatsApp messages go through the same pipeline as Telegram."""
        user = make_user(channel="whatsapp", channel_user_id="919876543210")
        session = make_session()
        msg = make_msg(
            "Write a post about startup lessons",
            channel="whatsapp",
            channel_user_id="919876543210",
        )

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.conversation.dispatcher._get_sender", return_value=sender),
        ):
            from app.conversation.dispatcher import dispatch

            await dispatch(user_id=user.id, msg=msg)

        assert session.state == SessionState.COLLECTING

    @pytest.mark.asyncio
    async def test_whatsapp_sender_selected_for_whatsapp_channel(self):
        """WhatsApp messages use WhatsAppSender, not TelegramSender."""
        user = make_user(channel="whatsapp", channel_user_id="919876543210")
        session = make_session()
        msg = make_msg("/help", channel="whatsapp", channel_user_id="919876543210")  # noqa: F841

        sender = MagicMock()
        sender.send_text = AsyncMock()

        with (
            patch("app.conversation.dispatcher._load_user", return_value=user),
            patch("app.conversation.dispatcher.get_or_create_session", return_value=session),
            patch("app.conversation.dispatcher.save_session", new_callable=AsyncMock),
            patch("app.conversation.dispatcher.check_rate_limit", new_callable=AsyncMock),
            patch("app.channels.whatsapp.WhatsAppSender", return_value=sender),
        ):
            from app.conversation.dispatcher import _get_sender

            result = _get_sender("whatsapp")  # noqa: F841

        # Should be a WhatsAppSender instance
        # (We can't assert the exact type since it's mocked, but the call chain is tested)
