import asyncio
import logging
import re
from urllib.parse import urlsplit, urlunsplit

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis_client: Redis | None = None
_client_lock = asyncio.Lock()


def sanitize_redis_url(url: str) -> str:
    """Mask credentials in Redis URL for safe logging."""
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.password is not None or parsed.username is not None:
            user = parsed.username or ""
            netloc = f"{user}:***@{parsed.hostname}" if parsed.hostname else "***"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        return url
    except Exception:
        return "[sanitized-redis-url]"


def sanitize_log_message(msg: str) -> str:
    """Mask any embedded passwords in error strings."""
    return re.sub(r"://([^:@]+):([^@]+)@", r"://\1:***@", str(msg))


async def init_redis_client() -> Redis | None:
    """
    Initializes the shared Redis connection pool.
    Gracefully handles unavailability during startup without failing the application.
    """
    global _redis_client

    if not settings.cache_enabled:
        logger.info("Redis cache is disabled via settings (CACHE_ENABLED=False).")
        return None

    sanitized_url = sanitize_redis_url(settings.redis_url)
    async with _client_lock:
        if _redis_client is not None:
            return _redis_client

        try:
            client = aioredis.from_url(
                settings.redis_url,
                socket_connect_timeout=settings.redis_connect_timeout,
                socket_timeout=settings.redis_socket_timeout,
                decode_responses=True,
            )
            # Validate connectivity with ping
            await client.ping()
            _redis_client = client
            logger.info("Connected to Redis at %s", sanitized_url)
            return _redis_client
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            sanitized_err = sanitize_log_message(str(exc))
            logger.warning(
                "Redis connection failed at %s (%s: %s). Application running in degraded mode with cache disabled.",
                sanitized_url,
                exc.__class__.__name__,
                sanitized_err,
            )
            _redis_client = None
            return None


async def close_redis_client() -> None:
    """Closes the Redis connection pool cleanly during application shutdown."""
    global _redis_client
    async with _client_lock:
        if _redis_client is not None:
            try:
                await _redis_client.aclose()
            except Exception as exc:
                logger.warning("Error closing Redis client: %s", sanitize_log_message(str(exc)))
            finally:
                _redis_client = None


def get_redis_client() -> Redis | None:
    """Returns the current singleton Redis client or None if unavailable/disabled."""
    return _redis_client


def set_redis_client(client: Redis | None) -> None:
    """Explicitly set or override the Redis client (useful for tests and dependency overrides)."""
    global _redis_client
    _redis_client = client
