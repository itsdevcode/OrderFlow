import asyncio
import json
import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.redis import get_redis_client, sanitize_log_message

logger = logging.getLogger(__name__)

QueryParamValue = str | int | float | bool | None


class CacheService:
    """
    Production-grade, typed cache abstraction layer backed by Redis.
    Provides cache-aside helpers, deterministic key generation,
    versioned namespace invalidation, malformed JSON recovery,
    and circuit-breaker cooldown resilience to avoid repeated long timeouts.
    """

    def __init__(
        self,
        redis_client: Redis | None = None,
        default_ttl: int | None = None,
        cooldown_seconds: float = 5.0,
    ) -> None:
        self._custom_client: Redis | None = redis_client
        self.default_ttl: int = default_ttl or settings.cache_default_ttl_seconds
        self.cooldown_seconds: float = cooldown_seconds
        self._last_failure_time: float = 0.0

    @property
    def client(self) -> Redis | None:
        """Returns the configured or singleton Redis client."""
        if self._custom_client is not None:
            return self._custom_client
        return get_redis_client()

    @property
    def is_available(self) -> bool:
        """
        Evaluates whether Redis caching is currently active and healthy.
        Bypasses Redis during the failure cooldown period to avoid repeated
        connection timeout latency on production request paths.
        """
        if not settings.cache_enabled:
            return False
        if self.client is None:
            return False
        if self._last_failure_time > 0:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed < self.cooldown_seconds:
                return False
        return True

    def _record_failure(self, exc: Exception) -> None:
        """Records an operational failure and triggers the circuit-breaker cooldown."""
        self._last_failure_time = time.monotonic()
        sanitized_err = sanitize_log_message(str(exc))
        logger.warning(
            "Redis cache error (%s: %s). Activating %0.1fs cooldown fallback to DB.",
            exc.__class__.__name__,
            sanitized_err,
            self.cooldown_seconds,
        )

    def _record_success(self) -> None:
        """Clears circuit breaker cooldown on successful interaction."""
        self._last_failure_time = 0.0

    def build_key(
        self,
        namespace: str,
        key_type: str,
        *segments: str,
        **query_params: QueryParamValue,
    ) -> str:
        """
        Builds a deterministic, versioned cache key.
        Format: orderflow:v1:{namespace}:{key_type}[:seg1:seg2...][:param1=val1&param2=val2]

        Query parameters are sorted alphabetically and bools are normalized to lowercase.
        """
        parts: list[str] = ["orderflow", "v1", namespace, key_type]
        for seg in segments:
            if seg:
                parts.append(str(seg))

        if query_params:
            filtered: dict[str, str] = {}
            for k, v in query_params.items():
                if v is not None:
                    if isinstance(v, bool):
                        filtered[k] = "true" if v else "false"
                    else:
                        filtered[k] = str(v)

            if filtered:
                sorted_keys: list[str] = sorted(filtered.keys())
                sorted_params: list[str] = [f"{k}={filtered[k]}" for k in sorted_keys]
                parts.append("&".join(sorted_params))

        return ":".join(parts)

    async def get(self, key: str) -> object | None:
        """
        Retrieves and deserializes JSON cached data for the given key.
        Gracefully handles misses, connection errors, and corrupted/malformed JSON.
        """
        if not self.is_available:
            return None

        client = self.client
        if client is None:
            return None

        try:
            raw_data = await client.get(key)
            if raw_data is None:
                return None

            self._record_success()
            try:
                data: object = json.loads(raw_data)
                return data
            except (json.JSONDecodeError, TypeError, ValueError) as json_err:
                logger.warning(
                    "Malformed JSON detected in cache for key %s (%s). Safely falling back to DB miss.",
                    key,
                    json_err,
                )
                try:
                    _ = await client.delete(key)
                except Exception:
                    pass
                return None
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._record_failure(exc)
            return None

    async def set(
        self,
        key: str,
        value: object,
        ttl: int | None = None,
    ) -> bool:
        """
        Serializes and caches validated JSON-compatible response data with bounded TTL.
        Returns True on success, False on degradation/error.
        """
        if not self.is_available:
            return False

        client = self.client
        if client is None:
            return False

        ttl_seconds = ttl if ttl is not None else self.default_ttl
        try:
            serialized = json.dumps(value)
            _ = await client.set(key, serialized, ex=ttl_seconds)
            self._record_success()
            return True
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._record_failure(exc)
            return False

    async def delete(self, key: str) -> bool:
        """Deletes a single cache key."""
        if not self.is_available:
            return False

        client = self.client
        if client is None:
            return False

        try:
            res = await client.delete(key)
            self._record_success()
            return bool(res)
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._record_failure(exc)
            return False

    async def delete_many(self, *keys: str) -> int:
        """Deletes multiple cache keys in a single operation without using Redis KEYS."""
        valid_keys = [k for k in keys if k]
        if not valid_keys or not self.is_available:
            return 0

        client = self.client
        if client is None:
            return 0

        try:
            res = await client.delete(*valid_keys)
            self._record_success()
            return int(res)
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._record_failure(exc)
            return 0

    async def get_version(self, namespace: str) -> int:
        """
        Retrieves the current version counter for a namespace (defaults to 1).
        Used for versioned namespace invalidation of paginated listings.
        """
        if not self.is_available:
            return 1

        client = self.client
        if client is None:
            return 1

        version_key = self.build_key(namespace, "version")
        try:
            val = await client.get(version_key)
            self._record_success()
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    return 1
            return 1
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._record_failure(exc)
            return 1

    async def bump_version(self, namespace: str) -> int:
        """
        Atomically increments the namespace listing version counter.
        Instantly invalidates all cached listings under this namespace in O(1).
        """
        if not self.is_available:
            return 1

        client = self.client
        if client is None:
            return 1

        version_key = self.build_key(namespace, "version")
        try:
            val = await client.incr(version_key)
            self._record_success()
            return int(val)
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._record_failure(exc)
            return 1

    async def invalidate_product(self, product_id: int) -> None:
        """
        Invalidates product detail cache and bumps product listings version.
        Safe to call after DB commit. Never throws.
        """
        try:
            detail_key = self.build_key("products", "detail", str(product_id))
            _ = await self.delete(detail_key)
            _ = await self.bump_version("products")
        except Exception as exc:
            logger.warning("Error during product cache invalidation: %s", sanitize_log_message(str(exc)))

    async def invalidate_category(self, category_id: int) -> None:
        """
        Invalidates category detail cache, category listings version,
        and product listings version (to prevent stale category-product relationships).
        Safe to call after DB commit. Never throws.
        """
        try:
            detail_key = self.build_key("categories", "detail", str(category_id))
            _ = await self.delete(detail_key)
            _ = await self.bump_version("categories")
            # Bumping products version ensures any product list filtered by category is refreshed
            _ = await self.bump_version("products")
        except Exception as exc:
            logger.warning("Error during category cache invalidation: %s", sanitize_log_message(str(exc)))


_default_cache_service: CacheService | None = None


def get_cache_service() -> CacheService:
    """Returns the shared CacheService instance."""
    global _default_cache_service
    if _default_cache_service is None:
        _default_cache_service = CacheService()
    return _default_cache_service
