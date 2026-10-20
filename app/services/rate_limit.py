"""Atomic, Redis-clock fixed windows shared by every API worker."""
import asyncio
from dataclasses import dataclass
import hashlib
import logging
import time
from typing import Literal

from redis.exceptions import RedisError

from app.core.redis import ensure_redis_connected

logger = logging.getLogger(__name__)
RateScope = Literal["login", "refresh", "payment_webhook"]

# Denied requests do not extend expiry or grow the counter. The window is
# [floor(redis_time/window)*window, next boundary), independent of worker clocks.
FIXED_WINDOW_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local duration = tonumber(ARGV[2]) * 1000
local bucket = math.floor(now / duration)
local reset = (bucket + 1) * duration
local previous = redis.call('HGET', KEYS[1], 'bucket')
local count = 0
if previous and tonumber(previous) == bucket then
    count = tonumber(redis.call('HGET', KEYS[1], 'count')) or 0
end
local allowed = 0
if count < tonumber(ARGV[1]) then
    count = count + 1
    allowed = 1
end
redis.call('HSET', KEYS[1], 'bucket', bucket, 'count', count)
redis.call('PEXPIREAT', KEYS[1], reset)
return {allowed, math.max(0, tonumber(ARGV[1]) - count), math.ceil((reset - now) / 1000)}
"""


class RateLimitUnavailable(Exception):
    """The shared counter could not be consulted; endpoint policy decides response."""


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    remaining: int
    retry_after: int


class RateLimiter:
    def __init__(self, cooldown_seconds: float = 5.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._unavailable_until = 0.0

    @staticmethod
    def build_key(scope: RateScope, identity: str, window_seconds: int) -> str:
        # Identity must be a trusted address or a verified authenticated principal.
        # Neither credentials nor supplied token contents are suitable identities.
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return f"orderflow:rate:v1:{scope}:{window_seconds}:{digest}"

    async def check(
        self, scope: RateScope, identity: str, limit: int, window_seconds: int,
    ) -> RateDecision:
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate limit and window must be positive")
        if time.monotonic() < self._unavailable_until:
            raise RateLimitUnavailable
        try:
            client = await ensure_redis_connected()
            if client is None:
                raise RateLimitUnavailable
            result = await client.eval(
                FIXED_WINDOW_SCRIPT, 1, self.build_key(scope, identity, window_seconds),
                limit, window_seconds,
            )
            allowed, remaining, retry = result
            self._unavailable_until = 0.0
            return RateDecision(bool(int(allowed)), int(remaining), int(retry))
        except (RedisError, OSError, asyncio.TimeoutError, RateLimitUnavailable) as exc:
            self._unavailable_until = time.monotonic() + self.cooldown_seconds
            # No connection strings, credentials, identity, or payloads in logs.
            logger.warning("Rate limiter unavailable (%s)", type(exc).__name__)
            raise RateLimitUnavailable from exc


rate_limiter = RateLimiter()
