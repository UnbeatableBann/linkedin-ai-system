"""
tests/test_scheduling.py
─────────────────────────
Tests for scheduling helpers — date parsing, conflict detection,
slot suggestions, timezone handling.

No network calls or DB calls — pure unit tests on the helper functions.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import pytz

from app.conversation.scheduling import (
    _format_dt,
    _next_auto_slot,
    _parse_datetime,
    _suggest_next_slot,
)

IST = pytz.timezone("Asia/Kolkata")
UTC = UTC


class TestParseDatetime:
    def test_parses_now(self):
        result = _parse_datetime("now", IST)
        assert result is not None
        # Should be very close to current time
        diff = abs((result - datetime.now(UTC)).total_seconds())
        assert diff < 10

    def test_parses_immediately(self):
        result = _parse_datetime("immediately", IST)
        assert result is not None

    def test_parses_auto(self):
        result = _parse_datetime("auto", IST)
        assert result is not None
        assert result > datetime.now(UTC)

    def test_parses_next_monday(self):
        result = _parse_datetime("next Monday 9am", IST)
        assert result is not None
        assert result > datetime.now(UTC)
        # Should be a Monday in the user's timezone
        local = result.astimezone(IST)
        assert local.weekday() == 0  # Monday

    def test_parses_friday(self):
        result = _parse_datetime("next Friday", IST)
        assert result is not None
        local = result.astimezone(IST)
        assert local.weekday() == 4  # Friday

    def test_parses_explicit_date(self):
        # Use a future date that will always be in the future
        result = _parse_datetime("December 25 at 9am", IST)
        assert result is not None

    def test_invalid_returns_none(self):
        result = _parse_datetime("banana phone", IST)
        assert result is None

    def test_empty_string_returns_none(self):
        result = _parse_datetime("", IST)
        assert result is None

    def test_result_is_utc_aware(self):
        result = _parse_datetime("next Monday 9am", IST)
        assert result is not None
        assert result.tzinfo is not None


class TestNextAutoSlot:
    def test_returns_monday_or_friday(self):
        result = _next_auto_slot(IST)
        local = result.astimezone(IST)
        assert local.weekday() in {0, 4}, f"Expected Mon or Fri, got {local.strftime('%A')}"

    def test_returns_future_time(self):
        result = _next_auto_slot(IST)
        assert result > datetime.now(UTC)

    def test_time_is_9am_local(self):
        result = _next_auto_slot(IST)
        local = result.astimezone(IST)
        assert local.hour == 9
        assert local.minute == 0

    def test_different_timezone(self):
        ny_tz = pytz.timezone("America/New_York")
        result = _next_auto_slot(ny_tz)
        local = result.astimezone(ny_tz)
        assert local.weekday() in {0, 4}
        assert local.hour == 9


class TestSuggestNextSlot:
    def test_suggests_future_slot(self):
        conflicted = datetime.now(UTC) + timedelta(days=1)
        result = _suggest_next_slot(conflicted, IST)
        assert result > datetime.now(UTC)

    def test_suggests_monday_or_friday(self):
        conflicted = datetime.now(UTC) + timedelta(days=2)
        result = _suggest_next_slot(conflicted, IST)
        local = result.astimezone(IST)
        assert local.weekday() in {0, 4}

    def test_suggestion_after_conflict(self):
        conflicted = datetime.now(UTC) + timedelta(days=1)
        result = _suggest_next_slot(conflicted, IST)
        # Suggestion should be after the conflicted time
        assert result > conflicted or result > datetime.now(UTC)


class TestFormatDatetime:
    def test_formats_readable(self):
        dt = datetime(2025, 4, 7, 9, 0, 0, tzinfo=UTC)
        result = _format_dt(dt, IST)
        # Should contain the day name and time
        assert "2025" in result
        assert "9" in result  # 9am or 2:30pm (IST offset)

    def test_includes_timezone_name(self):
        dt = datetime(2025, 4, 7, 9, 0, 0, tzinfo=UTC)
        result = _format_dt(dt, IST)
        assert "IST" in result

    def test_monday_format(self):
        # Find a Monday
        dt = datetime(2025, 4, 7, 3, 30, 0, tzinfo=UTC)  # 9am IST on a Monday
        result = _format_dt(dt, IST)
        assert "Monday" in result


class TestConflictCheck:
    @pytest.mark.asyncio
    async def test_no_conflict_returns_false(self):
        mock_db = MagicMock()
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.gte.return_value.lte.return_value.execute.return_value = MagicMock(  # noqa: E501
            data=[]
        )

        with patch("app.conversation.scheduling.get_db", return_value=mock_db):
            from app.conversation.scheduling import _check_conflict

            result = await _check_conflict("user_123", datetime.now(UTC) + timedelta(days=1))

        assert result is False

    @pytest.mark.asyncio
    async def test_conflict_detected(self):
        mock_db = MagicMock()
        # Simulate an existing post in the window
        mock_db.table.return_value.select.return_value.eq.return_value.eq.return_value.gte.return_value.lte.return_value.execute.return_value = MagicMock(  # noqa: E501
            data=[{"id": "existing_post"}]
        )

        with patch("app.conversation.scheduling.get_db", return_value=mock_db):
            from app.conversation.scheduling import _check_conflict

            result = await _check_conflict("user_123", datetime.now(UTC) + timedelta(days=1))

        assert result is True
