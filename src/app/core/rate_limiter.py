"""
app/core/rate_limiter.py
─────────────────────────
Per-user rate limiting using Redis sliding window counters.

Limits enforced:
  - Max 10 messages/minute per user (prevents spam, runaway bots)
  - Max 20 LLM generation calls/hour per user (cost protection)
  - Max 5 posts/day scheduled per user (LinkedIn spam prevention)

All limits are configurable via constants below.
If Redis is unavailable, the limiter fails OPEN (passes the request)
so a Redis outage doesn't block users from using the system.
"""

from enum import StrEnum
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Limits ─────────────────────────────────────────────────────────────────
MESSAGE_LIMIT_PER_MINUTE = 10
GENERATION_LIMIT_PER_HOUR = 20
SCHEDULE_LIMIT_PER_DAY = 5


class LimitType(StrEnum):
    MESSAGE = "msg"
    GENERATION = "gen"
    SCHEDULE = "sched"


class RateLimitExceeded(Exception):
    """Raised when a user exceeds a rate limit."""

    def __init__(self, limit_type: LimitType, retry_after_seconds: int) -> None:
        self.limit_type = limit_type
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit exceeded: {limit_type}")

    def user_message(self) -> str:
        if self.limit_type == LimitType.MESSAGE:
            return (
                f"You're sending messages too fast. "
                f"Please wait {self.retry_after_seconds} seconds."
            )
        elif self.limit_type == LimitType.GENERATION:
            minutes = self.retry_after_seconds // 60
            return (
                f"You've reached the generation limit (20/hour). "
                f"Please wait about {minutes} minute(s) before generating another post."
            )
        elif self.limit_type == LimitType.SCHEDULE:
            return (
                "You've scheduled 5 posts today — that's the daily limit. "
                "Your remaining posts will carry over to tomorrow."
            )
        return "Rate limit exceeded. Please wait before trying again."


async def check_rate_limit(user_id: str, limit_type: LimitType) -> None:
    """
    Check if the user has exceeded a rate limit.
    Raises RateLimitExceeded if over the limit.
    Fails open (no exception) if Redis is unavailable.
    """
    try:
        await _check(user_id, limit_type)
    except RateLimitExceeded:
        raise
    except Exception as exc:
        # Redis down or other error — fail open, log warning
        logger.warning("rate_limiter.redis_error", error=str(exc), user_id=user_id)


async def _check(user_id: str, limit_type: LimitType) -> None:
    """Inner check — may raise Redis errors (caller handles)."""
    import redis.asyncio as aioredis

    from app.config import get_settings

    settings = get_settings()
    r = aioredis.from_url(settings.redis_url, decode_responses=True)

    if limit_type == LimitType.MESSAGE:
        await _sliding_window(r, user_id, "msg", 60, MESSAGE_LIMIT_PER_MINUTE, 60)

    elif limit_type == LimitType.GENERATION:
        await _sliding_window(r, user_id, "gen", 3600, GENERATION_LIMIT_PER_HOUR, 3600)

    elif limit_type == LimitType.SCHEDULE:
        await _sliding_window(r, user_id, "sched", 86400, SCHEDULE_LIMIT_PER_DAY, 86400)

    await r.aclose()


async def _sliding_window(
    r: Any,
    user_id: str,
    prefix: str,
    window_seconds: int,
    limit: int,
    ttl: int,
) -> None:
    """
    Sliding window rate limiter using Redis INCR + EXPIRE.
    Simple implementation: fixed window per period.
    """
    import time

    window_start = int(time.time()) // window_seconds
    key = f"rl:{prefix}:{user_id}:{window_start}"

    count = await r.incr(key)
    if count == 1:
        await r.expire(key, ttl + 10)  # +10s buffer

    if count > limit:
        # Calculate retry-after: seconds until next window
        next_window = (window_start + 1) * window_seconds
        retry_after = max(1, next_window - int(time.time()))
        raise RateLimitExceeded(LimitType(prefix), retry_after)

# TODO: Exception name `RateLimitExceeded` should be named with an Error suffix
