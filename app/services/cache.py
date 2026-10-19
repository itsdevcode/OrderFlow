import asyncio
import json
import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis_client, sanitize_log_message
from app.models.cache_invalidation import CachePendingInvalidation

logger = logging.getLogger(__name__)

QueryParamValue = str | int | float | bool | None


class CacheService:
    """
    Production-grade, typed cache abstraction layer backed by Redis.
    Provides cache-aside helpers, deterministic key generation,
    versioned namespace invalidation, malformed JSON recovery,
    circuit-breaker cooldown resilience, and multi-worker outage recovery.
    """

    def __init__(
        self,
        redis_client: Redis | None = None,
        default_ttl: int | None = None,
        cooldown_seconds: float = 5.0,
        reconnect_cooldown: float = 5.0,
    ) -> None:
        self._custom_client: Redis | None = redis_client
        self.default_ttl: int = default_ttl or settings.cache_default_ttl_seconds
        self.cooldown_seconds: float = cooldown_seconds
        self.reconnect_cooldown: float = reconnect_cooldown
        self._last_failure_time: float = 0.0
        self._last_reconnect_time: float = 0.0

    @property
    def client(self) -> Redis | None:
        """Returns the configured or singleton Redis client."""
        if self._custom_client is not None:
            return self._custom_client
        return get_redis_client()

    async def ensure_connected(self) -> Redis | None:
        """
        Attempts a bounded reconnection to Redis if not currently connected
        and the reconnect cooldown period has elapsed.
        Ensures Redis is never mandatory at startup while recovering automatically
        when Redis becomes reachable again.
        """
        if not settings.cache_enabled:
            return None
        client = self.client
        if client is not None:
            return client
        now = time.monotonic()
        if now - self._last_reconnect_time < self.reconnect_cooldown:
            return None
        self._last_reconnect_time = now
        from app.core.redis import init_redis_client
        return await init_redis_client()

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
        _ = await self.ensure_connected()
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
                from typing import cast
                data: object = cast(object, json.loads(raw_data))
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
        _ = await self.ensure_connected()
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
        """Deletes a single cache key. Returns True if successfully deleted or absent."""
        _ = await self.ensure_connected()
        if not self.is_available:
            return False

        client = self.client
        if client is None:
            return False

        try:
            _ = await client.delete(key)
            self._record_success()
            return True
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._record_failure(exc)
            return False

    async def delete_many(self, *keys: str) -> int:
        """Deletes multiple cache keys in a single operation without using Redis KEYS. Returns -1 on failure."""
        valid_keys = [k for k in keys if k]
        if not valid_keys:
            return 0
        _ = await self.ensure_connected()
        if not self.is_available:
            return -1

        client = self.client
        if client is None:
            return -1

        try:
            res = await client.delete(*valid_keys)
            self._record_success()
            return int(res)
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._record_failure(exc)
            return -1

    async def get_version(self, namespace: str) -> int:
        """
        Retrieves the current version counter for a namespace (defaults to 1).
        Used for versioned namespace invalidation of paginated listings.
        """
        _ = await self.ensure_connected()
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
        If the version key is absent in Redis (defaulting implicitly to 1),
        it transitions directly to 2, ensuring that the first mutation strictly
        invalidates any listing previously cached under default version 1.
        Instantly invalidates all cached listings under this namespace in O(1).
        """
        _ = await self.ensure_connected()
        if not self.is_available:
            return 1

        client = self.client
        if client is None:
            return 1

        version_key = self.build_key(namespace, "version")
        try:
            lua_script = """
            local current = redis.call('GET', KEYS[1])
            if not current then
                redis.call('SET', KEYS[1], 2)
                return 2
            else
                return redis.call('INCR', KEYS[1])
            end
            """
            val: object = await client.eval(lua_script, 1, version_key)
            self._record_success()
            return int(str(val))
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._record_failure(exc)
            return 1

    def stage_product_invalidation(
        self,
        db: AsyncSession,
        product_id: int,
    ) -> list[CachePendingInvalidation]:
        """
        Stages durable cache-invalidation intent in PostgreSQL in the SAME transaction
        as the product mutation. Must be called before db.commit().
        """
        if not settings.cache_enabled:
            return []
        detail_key = self.build_key("products", "detail", str(product_id))
        event = CachePendingInvalidation(key=detail_key, namespace="products")
        db.add(event)
        return [event]

    def stage_category_invalidation(
        self,
        db: AsyncSession,
        category_id: int,
    ) -> list[CachePendingInvalidation]:
        """
        Stages durable cache-invalidation intent in PostgreSQL in the SAME transaction
        as the category mutation. Must be called before db.commit().
        Stages both category detail/listing and products listing (for relational consistency).
        """
        if not settings.cache_enabled:
            return []
        detail_key = self.build_key("categories", "detail", str(category_id))
        event1 = CachePendingInvalidation(key=detail_key, namespace="categories")
        event2 = CachePendingInvalidation(namespace="products")
        db.add(event1)
        db.add(event2)
        return [event1, event2]

    async def execute_staged_invalidations(
        self,
        db: AsyncSession,
        events: list[CachePendingInvalidation],
    ) -> bool:
        """
        Executes staged cache invalidations against Redis after DB commit.
        Only deletes/acknowledges the pending events from PostgreSQL if all
        Redis operations succeed.
        If Redis is down or operations fail, the events are retained in PostgreSQL
        for later multi-worker reconciliation via sync_pending_invalidations.
        """
        if not settings.cache_enabled or not events:
            return True

        _ = await self.ensure_connected()

        if not self.is_available or self.client is None:
            return False

        keys_to_del: list[str] = [e.key for e in events if e.key]
        namespaces_to_bump: set[str] = {e.namespace for e in events if e.namespace}
        event_ids: list[int] = [e.id for e in events]

        try:
            if keys_to_del:
                del_res = await self.delete_many(*keys_to_del)
                if del_res < 0:
                    return False

            for ns in namespaces_to_bump:
                val = await self.bump_version(ns)
                if val <= 1 and not self.is_available:
                    return False

            if event_ids:
                _ = await db.execute(
                    delete(CachePendingInvalidation).where(CachePendingInvalidation.id.in_(event_ids))
                )
                await db.commit()
            return True
        except Exception as exc:
            try:
                await db.rollback()
            except Exception:
                pass
            logger.warning(
                "Failed to acknowledge staged invalidations: %s",
                sanitize_log_message(str(exc)),
            )
            return False

    async def sync_pending_invalidations(self, db: AsyncSession) -> bool:
        """
        Reconciles pending invalidations stored in PostgreSQL into Redis.
        Returns:
            True: if there are no pending invalidations, or all pending invalidations
                  were successfully reconciled into Redis.
            False: if pending invalidations exist but could not be reconciled into Redis
                   (or DB reconciliation query failed), signaling that Redis may contain
                   stale data and must be bypassed for this request.
        """
        if not settings.cache_enabled:
            return True

        _ = await self.ensure_connected()

        try:
            while True:
                stmt = (
                    select(CachePendingInvalidation)
                    .order_by(CachePendingInvalidation.id)
                    .limit(100)
                    .with_for_update()
                )
                result = await db.execute(stmt)
                rows = list(result.scalars().all())
                if not rows:
                    return True

                if not self.is_available or self.client is None:
                    await db.rollback()
                    return False

                keys_to_del: list[str] = [r.key for r in rows if r.key]
                namespaces_to_bump: set[str] = {r.namespace for r in rows if r.namespace}
                row_ids: list[int] = [r.id for r in rows]

                if keys_to_del:
                    del_res = await self.delete_many(*keys_to_del)
                    if del_res < 0:
                        await db.rollback()
                        return False

                for ns in namespaces_to_bump:
                    val = await self.bump_version(ns)
                    if val <= 1 and not self.is_available:
                        await db.rollback()
                        return False

                _ = await db.execute(
                    delete(CachePendingInvalidation).where(CachePendingInvalidation.id.in_(row_ids))
                )
                await db.commit()
        except Exception as exc:
            try:
                await db.rollback()
            except Exception:
                pass
            logger.warning(
                "Failed to sync pending cache invalidations: %s",
                sanitize_log_message(str(exc)),
            )
            return False

    async def invalidate_product(
        self,
        product_id: int,
        _db: AsyncSession | None = None,
    ) -> None:
        """
        Invalidates product detail cache and bumps product listings version.
        Maintained for backwards-compatibility or explicit post-commit invalidation calls.
        """
        detail_key = self.build_key("products", "detail", str(product_id))
        _ = await self.delete(detail_key)
        _ = await self.bump_version("products")

    async def invalidate_category(
        self,
        category_id: int,
        _db: AsyncSession | None = None,
    ) -> None:
        """
        Invalidates category detail cache, category listings version,
        and product listings version.
        Maintained for backwards-compatibility or explicit post-commit invalidation calls.
        """
        detail_key = self.build_key("categories", "detail", str(category_id))
        _ = await self.delete(detail_key)
        _ = await self.bump_version("categories")
        _ = await self.bump_version("products")


_default_cache_service: CacheService | None = None


def get_cache_service() -> CacheService:
    """Returns the shared CacheService instance."""
    global _default_cache_service
    if _default_cache_service is None:
        _default_cache_service = CacheService()
    return _default_cache_service
