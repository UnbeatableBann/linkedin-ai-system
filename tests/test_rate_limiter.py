"""
tests/test_rate_limiter.py
───────────────────────────
Tests for rate limiting logic.
Redis calls are mocked — testing the logic, not Redis.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.rate_limiter import (
    LimitType,
    MESSAGE_LIMIT_PER_MINUTE,
    GENERATION_LIMIT_PER_HOUR,
    SCHEDULE_LIMIT_PER_DAY,
    RateLimitExceeded,
)


class TestRateLimitExceeded:
    def test_message_user_message(self):
        exc = RateLimitExceeded(LimitType.MESSAGE, retry_after_seconds=30)
        msg = exc.user_message()
        assert "30" in msg
        assert "fast" in msg.lower() or "wait" in msg.lower()

    def test_generation_user_message(self):
        exc = RateLimitExceeded(LimitType.GENERATION, retry_after_seconds=1800)
        msg = exc.user_message()
        assert "30" in msg  # 1800 seconds = 30 minutes
        assert "hour" in msg.lower() or "generation" in msg.lower()

    def test_schedule_user_message(self):
        exc = RateLimitExceeded(LimitType.SCHEDULE, retry_after_seconds=43200)
        msg = exc.user_message()
        assert "5" in msg  # 5 posts/day limit
        assert "daily" in msg.lower() or "today" in msg.lower()

    def test_retry_after_stored(self):
        exc = RateLimitExceeded(LimitType.MESSAGE, retry_after_seconds=45)
        assert exc.retry_after_seconds == 45

    def test_limit_type_stored(self):
        exc = RateLimitExceeded(LimitType.GENERATION, retry_after_seconds=100)
        assert exc.limit_type == LimitType.GENERATION


class TestCheckRateLimit:
    @pytest.mark.asyncio
    async def test_passes_when_redis_unavailable(self):
        """Rate limiter must fail OPEN — Redis down should not block users."""
        with patch("app.core.rate_limiter._check", side_effect=ConnectionError("Redis down")):
            # Should not raise
            from app.core.rate_limiter import check_rate_limit
            await check_rate_limit("user_123", LimitType.MESSAGE)

    @pytest.mark.asyncio
    async def test_raises_when_limit_exceeded(self):
        """Should propagate RateLimitExceeded through check_rate_limit."""
        exc = RateLimitExceeded(LimitType.MESSAGE, 30)
        with patch("app.core.rate_limiter._check", side_effect=exc):
            from app.core.rate_limiter import check_rate_limit
            with pytest.raises(RateLimitExceeded):
                await check_rate_limit("user_123", LimitType.MESSAGE)


class TestLimitConstants:
    def test_message_limit_reasonable(self):
        """Sanity check: message limit should be between 5 and 30 per minute."""
        assert 5 <= MESSAGE_LIMIT_PER_MINUTE <= 30

    def test_generation_limit_reasonable(self):
        """Generation limit should protect against runaway LLM costs."""
        assert 5 <= GENERATION_LIMIT_PER_HOUR <= 50

    def test_schedule_limit_reasonable(self):
        """Schedule limit should prevent LinkedIn spam."""
        assert 2 <= SCHEDULE_LIMIT_PER_DAY <= 10

    def test_limit_types_are_strings(self):
        """LimitType values must be valid Redis key prefixes."""
        for lt in LimitType:
            assert " " not in lt.value
            assert len(lt.value) > 0
