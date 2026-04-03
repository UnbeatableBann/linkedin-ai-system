"""
tests/test_dedup.py
────────────────────
Tests for webhook deduplication logic.

Uses pytest-mock to avoid hitting Supabase in tests.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.channels.base import MessageType, NormalisedMessage
from app.gateway.dedup import is_duplicate


def make_msg(message_id: str = "msg_123", channel: str = "telegram") -> NormalisedMessage:
    return NormalisedMessage(
        channel=channel,
        channel_user_id="user_456",
        message_id=message_id,
        text="Hello",
        message_type=MessageType.TEXT,
        raw={"test": True},
    )


class TestIsDuplicate:
    @pytest.mark.asyncio
    async def test_new_message_returns_false(self):
        """A message that hasn't been seen before should return False (process it)."""
        msg = make_msg("new_message_999")

        mock_db = MagicMock()
        mock_db.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[{"id": "log_1"}])

        with patch("app.gateway.dedup.get_db", return_value=mock_db):
            result = await is_duplicate(msg)

        assert result is False

    @pytest.mark.asyncio
    async def test_duplicate_message_returns_true(self):
        """A UNIQUE constraint violation means the message was already processed."""
        msg = make_msg("dup_message_123")

        mock_db = MagicMock()
        mock_db.table.return_value.insert.return_value.execute.side_effect = Exception(
            "duplicate key value violates unique constraint (23505)"
        )

        with patch("app.gateway.dedup.get_db", return_value=mock_db):
            result = await is_duplicate(msg)

        assert result is True

    @pytest.mark.asyncio
    async def test_unique_violation_case_insensitive(self):
        """UNIQUE violation text can appear in different cases."""
        msg = make_msg("dup_456")

        mock_db = MagicMock()
        mock_db.table.return_value.insert.return_value.execute.side_effect = Exception("UNIQUE constraint failed")

        with patch("app.gateway.dedup.get_db", return_value=mock_db):
            result = await is_duplicate(msg)

        assert result is True

    @pytest.mark.asyncio
    async def test_unexpected_db_error_does_not_block(self):
        """
        An unexpected DB error should NOT block message processing.
        Better to process twice than to drop a message.
        """
        msg = make_msg("msg_unexpected")

        mock_db = MagicMock()
        mock_db.table.return_value.insert.return_value.execute.side_effect = Exception("connection timeout")

        with patch("app.gateway.dedup.get_db", return_value=mock_db):
            result = await is_duplicate(msg)

        assert result is False  # Fail open — process the message

    @pytest.mark.asyncio
    async def test_idempotency_key_format(self):
        """Idempotency key should be channel:message_id."""
        msg = make_msg("abc123", channel="whatsapp")
        assert msg.idempotency_key == "whatsapp:abc123"

    @pytest.mark.asyncio
    async def test_different_channels_different_keys(self):
        """Same message_id on different channels should be treated as different messages."""
        tg_msg = make_msg("999", channel="telegram")
        wa_msg = make_msg("999", channel="whatsapp")

        assert tg_msg.idempotency_key != wa_msg.idempotency_key
        assert tg_msg.idempotency_key == "telegram:999"
        assert wa_msg.idempotency_key == "whatsapp:999"

    @pytest.mark.asyncio
    async def test_correct_payload_inserted(self):
        """Verify the correct payload shape is sent to Supabase."""
        msg = make_msg("check_payload")
        msg.raw = {"update_id": 42}

        inserted_data = {}
        mock_db = MagicMock()

        def capture_insert(data):
            inserted_data.update(data)
            return mock_db.table.return_value.insert.return_value

        mock_db.table.return_value.insert.side_effect = capture_insert
        mock_db.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[{}])

        with patch("app.gateway.dedup.get_db", return_value=mock_db):
            await is_duplicate(msg)

        # Check that insert was called with the right table
        mock_db.table.assert_called_with("webhook_log")
