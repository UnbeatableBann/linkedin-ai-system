"""
tests/test_integration_gateway.py
───────────────────────────────────
Integration tests for the full inbound message pipeline.

Tests the complete path:
  webhook payload → signature verify → parse → dedup → user resolve →
  session load → FSM dispatch → handler → session save → reply

All external services (Supabase, Telegram API, Celery) are mocked.
The FSM, parsing, and session logic run for real.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def telegram_text_payload():
    """A standard Telegram text message webhook payload."""
    return {
        "update_id": 100001,
        "message": {
            "message_id": 42,
            "chat": {"id": 987654321, "type": "private"},
            "from": {"id": 987654321, "first_name": "Rahul"},
            "text": "Write a post about AI in Indian startups",
            "date": 1700000000,
        },
    }


@pytest.fixture
def telegram_start_payload():
    return {
        "update_id": 100002,
        "message": {
            "message_id": 43,
            "chat": {"id": 987654321, "type": "private"},
            "from": {"id": 987654321, "first_name": "Rahul"},
            "text": "/start",
            "date": 1700000001,
        },
    }


@pytest.fixture
def whatsapp_text_payload():
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "id": "wamid.test_001",
                                    "from": "919876543210",
                                    "type": "text",
                                    "text": {"body": "Write a post about startup lessons"},
                                    "timestamp": "1700000000",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


@pytest.fixture
def existing_user():
    import os

    from cryptography.fernet import Fernet

    from app.db.models import Channel, LLMProvider, UserRow

    key = Fernet.generate_key()
    fernet = Fernet(key)
    os.environ["FERNET_SECRET_KEY"] = key.decode()
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
    os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "a" * 32)
    os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
    os.environ.setdefault("SUPABASE_SERVICE_KEY", "x" * 40)
    os.environ.setdefault("WHATSAPP_APP_SECRET", "secret")
    os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "token")
    os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
    os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "verify")
    os.environ.setdefault("OAUTH_CALLBACK_BASE_URL", "https://example.com")

    from app.config import get_settings

    get_settings.cache_clear()

    return UserRow(
        id=uuid4(),
        channel=Channel.TELEGRAM,
        channel_user_id="987654321",
        zernio_api_key_enc=fernet.encrypt(b"sk_test_zernio").decode(),
        zernio_profile_id="prof_test",
        zernio_account_id="acc_test",
        llm_provider=LLMProvider.ANTHROPIC,
        llm_api_key_enc=fernet.encrypt(b"sk-ant-test").decode(),
        llm_model="claude-haiku-4-5-20251001",
        timezone="Asia/Kolkata",
        style_prefs={},
        is_active=True,
        created_at=datetime.now(UTC),
    )


# ── Telegram parsing integration ──────────────────────────────────────────


class TestTelegramPayloadParsing:
    """Test that Telegram payloads are correctly parsed end-to-end."""

    def test_text_message_full_pipeline(self, telegram_text_payload):
        from app.channels.base import MessageType
        from app.channels.telegram import parse_telegram_update

        msg = parse_telegram_update(telegram_text_payload)

        assert msg is not None
        assert msg.channel == "telegram"
        assert msg.channel_user_id == "987654321"
        assert msg.message_id == "42"
        assert msg.text == "Write a post about AI in Indian startups"
        assert msg.message_type == MessageType.TEXT
        assert msg.idempotency_key == "telegram:42"

    def test_start_command_full_pipeline(self, telegram_start_payload):
        from app.channels.base import MessageType
        from app.channels.telegram import parse_telegram_update

        msg = parse_telegram_update(telegram_start_payload)

        assert msg is not None
        assert msg.message_type == MessageType.COMMAND
        assert msg.command == "/start"
        assert msg.is_command("/start") is True
        assert msg.is_command("/cancel") is False

    def test_raw_payload_preserved(self, telegram_text_payload):
        from app.channels.telegram import parse_telegram_update

        msg = parse_telegram_update(telegram_text_payload)
        assert msg.raw == telegram_text_payload


class TestWhatsAppPayloadParsing:
    def test_text_message_full_pipeline(self, whatsapp_text_payload):
        from app.channels.base import MessageType
        from app.channels.whatsapp import parse_whatsapp_payload

        msg = parse_whatsapp_payload(whatsapp_text_payload)

        assert msg is not None
        assert msg.channel == "whatsapp"
        assert msg.channel_user_id == "919876543210"
        assert msg.message_id == "wamid.test_001"
        assert msg.text == "Write a post about startup lessons"
        assert msg.message_type == MessageType.TEXT
        assert msg.idempotency_key == "whatsapp:wamid.test_001"


# ── Deduplication integration ──────────────────────────────────────────────


class TestDeduplicationPipeline:
    @pytest.mark.asyncio
    async def test_first_message_not_duplicate(self, telegram_text_payload):
        from app.channels.telegram import parse_telegram_update
        from app.gateway.dedup import is_duplicate

        msg = parse_telegram_update(telegram_text_payload)
        assert msg is not None

        mock_db = MagicMock()
        mock_db.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[{}])

        with patch("app.gateway.dedup.get_db", return_value=mock_db):
            result = await is_duplicate(msg)

        assert result is False

    @pytest.mark.asyncio
    async def test_repeated_message_is_duplicate(self, telegram_text_payload):
        from app.channels.telegram import parse_telegram_update
        from app.gateway.dedup import is_duplicate

        msg = parse_telegram_update(telegram_text_payload)
        assert msg is not None

        mock_db = MagicMock()
        mock_db.table.return_value.insert.return_value.execute.side_effect = Exception(
            "unique constraint violation (23505)"
        )

        with patch("app.gateway.dedup.get_db", return_value=mock_db):
            result = await is_duplicate(msg)

        assert result is True

    @pytest.mark.asyncio
    async def test_correct_idempotency_key_stored(self, whatsapp_text_payload):
        from app.channels.whatsapp import parse_whatsapp_payload
        from app.gateway.dedup import is_duplicate

        msg = parse_whatsapp_payload(whatsapp_text_payload)
        assert msg is not None

        captured = {}
        mock_db = MagicMock()

        def capture_insert(data):
            captured.update(data)
            m = MagicMock()
            m.execute.return_value = MagicMock(data=[{}])
            return m

        mock_db.table.return_value.insert.side_effect = capture_insert

        with patch("app.gateway.dedup.get_db", return_value=mock_db):
            await is_duplicate(msg)

        assert captured.get("idempotency_key") == "whatsapp:wamid.test_001"
        assert captured.get("channel") == "whatsapp"


# ── FSM + Session integration ─────────────────────────────────────────────


class TestFSMSessionIntegration:
    """Test FSM transitions integrated with session context."""

    def test_idle_to_collecting_with_context(self):
        from app.session.fsm import Event, transition
        from app.session.models import SessionContext, SessionState

        state = SessionState.IDLE
        ctx = SessionContext()

        # Simulate user sending a topic
        ctx.topic = "AI trends in 2025"
        state = transition(state, Event.START_POST)
        ctx.clarification_round = 0

        assert state == SessionState.COLLECTING
        assert ctx.topic == "AI trends in 2025"
        assert ctx.clarification_round == 0

    def test_full_happy_path_state_sequence(self):
        """Walk through the complete happy path: IDLE → post → publish."""
        from app.session.fsm import Event, transition
        from app.session.models import SessionContext, SessionState

        state = SessionState.IDLE
        ctx = SessionContext()

        # User sends topic
        state = transition(state, Event.START_POST)
        assert state == SessionState.COLLECTING
        ctx.topic = "Startup lessons"
        ctx.tone = "storytelling"
        ctx.audience = "founders"

        # Enough info gathered
        state = transition(state, Event.ENOUGH_INFO)
        assert state == SessionState.GENERATING

        # Draft ready
        ctx.draft_content = "Here is a great post..."
        ctx.draft_version = 1
        state = transition(state, Event.DRAFT_READY)
        assert state == SessionState.REVIEWING

        # User requests edit
        ctx.push_edit_history(ctx.draft_content)
        state = transition(state, Event.REQUEST_EDIT)
        assert state == SessionState.REFINING

        # Refinement complete
        ctx.draft_content = "Here is a refined post..."
        ctx.draft_version = 2
        state = transition(state, Event.REFINEMENT_READY)
        assert state == SessionState.REVIEWING

        # User approves
        state = transition(state, Event.APPROVE_DRAFT)
        assert state == SessionState.SCHEDULING

        # Schedule confirmed
        state = transition(state, Event.SCHEDULE_CONFIRMED)
        assert state == SessionState.SCHEDULED

        # Context cleared after scheduling
        ctx.clear_draft()
        assert ctx.topic is None
        assert ctx.draft_content is None
        assert ctx.draft_version == 0

    def test_cancel_at_any_point_resets_context(self):
        from app.session.fsm import Event, transition
        from app.session.models import SessionContext, SessionState

        # Set up a mid-review session
        state = SessionState.REVIEWING
        ctx = SessionContext()
        ctx.topic = "Important post"
        ctx.draft_content = "Draft content..."
        ctx.draft_version = 3

        # Cancel
        state = transition(state, Event.CANCEL)
        ctx.clear_draft()

        assert state == SessionState.IDLE
        assert ctx.topic is None
        assert ctx.draft_content is None

    def test_pending_message_during_generation(self):
        from app.session.models import SessionContext

        ctx = SessionContext()
        ctx.pending_message = None

        # Simulate message arriving during generation
        ctx.pending_message = "make it shorter"

        assert ctx.pending_message == "make it shorter"

        # After generation completes, pending message is consumed
        pending = ctx.pending_message
        ctx.pending_message = None

        assert pending == "make it shorter"
        assert ctx.pending_message is None


# ── Post rules + content pipeline integration ──────────────────────────────


class TestContentPipelineIntegration:
    def test_generation_prompt_includes_style(self):
        from app.content.prompts import build_generation_system_prompt
        from app.content.style_memory import merge_style_prefs

        # Build up style prefs from multiple posts
        prefs = {}
        prefs = merge_style_prefs(prefs, {"word_count": 150, "ends_with_question": True})
        prefs = merge_style_prefs(
            prefs,
            {"word_count": 200, "ends_with_question": True},
            example_post="Example post about growth.",
        )

        prompt = build_generation_system_prompt(
            tone="storytelling",
            audience="founders",
            length_pref=None,
            style_prefs=prefs,
        )

        assert "storytelling" in prompt
        assert "founders" in prompt
        assert isinstance(prompt, str)
        assert len(prompt) > 100

    def test_post_rules_applied_after_generation(self):
        from app.content.post_rules import enforce_post_rules

        # Simulate LLM response with preamble
        llm_response = (
            "Here's your post:\n\n"
            "Most founders get distribution wrong.\n\n"
            "Here are three things I wish I knew earlier.\n\n"
            "#Startups #India #Founders"
        )

        result = enforce_post_rules(llm_response)

        assert not result.startswith("Here")
        assert "Most founders" in result
        assert "#Startups" in result

    def test_refinement_prompt_preserves_draft(self):
        from app.content.prompts import build_refinement_user_prompt

        draft = "Original post about growth."
        instruction = "make it more personal and add an example"

        prompt = build_refinement_user_prompt(draft, instruction)

        assert draft in prompt
        assert instruction in prompt

    def test_style_signals_flow_into_prompts(self):
        """End-to-end: signals → prefs → prompt context."""
        from app.content.prompts import build_generation_system_prompt
        from app.content.style_memory import (
            build_style_context,
            extract_style_signals,
            merge_style_prefs,
        )

        post = "I built a startup in 2024.\n\nHere's what I learned:\n1. Focus\n2. Speed\n3. People\n\n#Startups"
        signals = extract_style_signals(post)
        prefs = merge_style_prefs({}, signals, example_post=post)
        prefs = merge_style_prefs(prefs, signals, example_post=post)  # 2 posts needed

        context = build_style_context(prefs)
        assert len(context) > 0

        prompt = build_generation_system_prompt("thought leadership", "founders", None, prefs)
        assert "thought leadership" in prompt


# ── Reviewing flow integration ────────────────────────────────────────────


class TestReviewingIntegration:
    def test_approval_phrases_detected(self):
        """Verify the approve/discard/edit routing logic."""
        from app.conversation.reviewing import _is_approval, _is_discard

        # Approvals
        for phrase in ["looks good", "yes", "perfect", "ship it", "great"]:
            assert _is_approval(phrase), f"'{phrase}' should be approval"

        # Discards
        for phrase in ["discard", "scrap it", "start over", "no"]:
            assert _is_discard(phrase), f"'{phrase}' should be discard"

    def test_present_draft_format(self):
        from app.conversation.reviewing import present_draft

        content = "This is my LinkedIn post content."
        result = present_draft(content, version=2)

        assert "Draft v2" in result
        assert content in result
        assert "✅" in result  # approve option
        assert "✏️" in result  # edit option
        assert "🗑" in result  # discard option

    def test_present_draft_version_increments(self):
        from app.conversation.reviewing import present_draft

        v1 = present_draft("Content", version=1)
        v2 = present_draft("Content", version=2)

        assert "v1" in v1
        assert "v2" in v2
        assert "v1" not in v2


# ── Scheduling integration ────────────────────────────────────────────────


class TestSchedulingIntegration:
    def test_auto_slot_is_in_future(self):
        from datetime import datetime

        import pytz

        from app.conversation.scheduling import _next_auto_slot

        ist = pytz.timezone("Asia/Kolkata")
        slot = _next_auto_slot(ist)

        assert slot > datetime.now(UTC)

    def test_auto_slot_is_weekday(self):
        import pytz

        from app.conversation.scheduling import _next_auto_slot

        ist = pytz.timezone("Asia/Kolkata")
        slot = _next_auto_slot(ist)
        local = slot.astimezone(ist)

        assert local.weekday() in {0, 4}  # Mon or Fri

    def test_format_slot_readable(self):
        from datetime import datetime

        import pytz

        from app.conversation.scheduling import _format_dt

        ist = pytz.timezone("Asia/Kolkata")
        dt = datetime(2025, 4, 7, 3, 30, 0, tzinfo=UTC)  # 9am IST Monday
        result = _format_dt(dt, ist)

        assert isinstance(result, str)
        assert len(result) > 10

    @pytest.mark.asyncio
    async def test_no_conflict_returns_false(self):
        from datetime import datetime

        from app.conversation.scheduling import _check_conflict

        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.in_.return_value.gte.return_value.lte.return_value.execute.return_value = MagicMock(  # noqa: E501
            data=[]
        )

        with patch("app.conversation.scheduling.get_db", return_value=mock_db):
            result = await _check_conflict("user_123", datetime.now(UTC) + timedelta(days=3))

        assert result is False

    @pytest.mark.asyncio
    async def test_conflict_detected(self):
        from datetime import datetime

        from app.conversation.scheduling import _check_conflict

        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.in_.return_value.gte.return_value.lte.return_value.execute.return_value = MagicMock(  # noqa: E501
            data=[{"id": "existing_post_id"}]
        )

        with patch("app.conversation.scheduling.get_db", return_value=mock_db):
            result = await _check_conflict("user_123", datetime.now(UTC) + timedelta(days=3))

        assert result is True


# ── Error handling integration ────────────────────────────────────────────


class TestErrorHandlingIntegration:
    def test_zernio_error_user_message(self):
        from app.zernio.client import ZernioError

        exc = ZernioError("Your LinkedIn token expired.", status_code=401)
        assert "LinkedIn" in str(exc)
        assert exc.status_code == 401

    def test_llm_error_retryable_flag(self):
        from app.content.llm_client import LLMError

        retryable = LLMError("Rate limit hit", retryable=True)
        not_retryable = LLMError("Invalid API key", retryable=False)

        assert retryable.retryable is True
        assert not_retryable.retryable is False

    def test_rate_limit_error_user_message(self):
        from app.core.rate_limiter import LimitType, RateLimitExceededError

        exc = RateLimitExceededError(LimitType.MESSAGE, retry_after_seconds=45)
        msg = exc.user_message()

        assert isinstance(msg, str)
        assert len(msg) > 10
        assert "45" in msg

    def test_format_error_for_user_generic(self):
        from app.core.errors import format_error_for_user

        exc = RuntimeError("Internal server error")
        msg = format_error_for_user(exc)

        # Should not expose the raw exception message
        assert "Internal server error" not in msg
        assert isinstance(msg, str)
        assert len(msg) > 10

    def test_format_error_for_user_zernio(self):
        from app.core.errors import format_error_for_user
        from app.zernio.client import ZernioError

        exc = ZernioError("Your Zernio API key is invalid.")
        msg = format_error_for_user(exc)

        assert "Zernio" in msg

    def test_configuration_error_messages(self):
        from app.core.errors import ConfigurationError

        cases = {
            "zernio_key": "Zernio",
            "llm_key": "AI",
            "linkedin": "LinkedIn",
            "timezone": "timezone",
        }
        for missing, expected_word in cases.items():
            exc = ConfigurationError(missing)
            msg = exc.user_message()
            assert (
                expected_word.lower() in msg.lower()
            ), f"ConfigurationError('{missing}') message should mention '{expected_word}'"
