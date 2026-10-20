import asyncio
from enum import Enum
import json
import logging
import time
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import session as session_module

from app.core.config import settings
from app.core.redis import ensure_redis_connected, get_redis_client, sanitize_log_message
from app.models.cache_invalidation import CachePendingInvalidation
from app.repositories.cache_invalidation import CacheInvalidationRepository

logger = logging.getLogger(__name__)

QueryParamValue = str | int | float | bool | None


class ReconciliationOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CACHE_UNAVAILABLE = "CACHE_UNAVAILABLE"


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
        if self._custom_client is not None:
            return self._custom_client
        if self.client is not None:
            return self.client
        now = time.monotonic()
        if now - self._last_reconnect_time < self.reconnect_cooldown:
            return None
        self._last_reconnect_time = now
        return await ensure_redis_connected(self.reconnect_cooldown)

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

    async def set_if_version(
        self, key: str, value: object, namespace: str, version: int,
    ) -> bool:
        """Atomically fence cache population against namespace invalidation."""
        if not self.is_available or self.client is None:
            return False
        try:
            result = await self.client.eval(
                """
                local version = redis.call('GET', KEYS[1]) or '1'
                if version ~= ARGV[1] then return 0 end
                redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
                return 1
                """,
                2, self.build_key(namespace, "version"), key,
                str(version), json.dumps(value), self.default_ttl,
            )
            return result == 1
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

    async def stage_product_invalidation(
        self,
        db: AsyncSession,
        product_id: int,
    ) -> list[CachePendingInvalidation]:
        """
        Stages durable cache-invalidation intent in PostgreSQL in the SAME transaction
        as the product mutation. Must be called before db.commit().
        Flushes entity via repository to ensure primary key is populated.
        """
        repo = CacheInvalidationRepository(db)
        detail_key = self.build_key("products", "detail", str(product_id))
        event = await repo.stage(key=detail_key, namespace="products")
        return [event]

    async def stage_category_invalidation(
        self,
        db: AsyncSession,
        category_id: int,
    ) -> list[CachePendingInvalidation]:
        """
        Stages durable cache-invalidation intent in PostgreSQL in the SAME transaction
        as the category mutation. Must be called before db.commit().
        Stages both category detail/listing and products listing (for relational consistency).
        """
        repo = CacheInvalidationRepository(db)
        detail_key = self.build_key("categories", "detail", str(category_id))
        event1 = await repo.stage(key=detail_key, namespace="categories")
        event2 = await repo.stage(namespace="products")
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

        # Reconciliation owns a separate session, never the caller's transaction.
        outcome = await self.sync_pending_invalidations(db)
        return outcome == ReconciliationOutcome.SUCCESS

    async def sync_pending_invalidations(
        self,
        db: AsyncSession,
        lease_seconds: int = 15,
        limit: int = 100,
    ) -> ReconciliationOutcome:
        """
        Reconciles pending invalidations stored in PostgreSQL into Redis.
        Uses safe multi-worker concurrency with SELECT ... FOR UPDATE SKIP LOCKED
        to claim batches, committing the claim immediately to release PostgreSQL row locks
        prior to executing Redis network operations.
        Returns:
            SUCCESS: if there are no pending invalidations, or all pending invalidations
                     were successfully reconciled and acknowledged into Redis.
            CACHE_UNAVAILABLE: if Redis is unreachable or operations failed.
            FAILED: if database operations failed or in-flight records are locked by another worker.
        """
        if not settings.cache_enabled:
            return ReconciliationOutcome.SUCCESS

        _ = await self.ensure_connected()

        async with session_module.AsyncSessionLocal() as recovery_db:
            return await self._reconcile(recovery_db, lease_seconds, limit)

    async def _reconcile(
        self, db: AsyncSession, lease_seconds: int, limit: int,
    ) -> ReconciliationOutcome:
        repo = CacheInvalidationRepository(db)
        try:
            has_pending = await repo.has_pending()
            if not has_pending:
                if not self.is_available or self.client is None:
                    return ReconciliationOutcome.CACHE_UNAVAILABLE
                return ReconciliationOutcome.SUCCESS

            if not self.is_available or self.client is None:
                return ReconciliationOutcome.CACHE_UNAVAILABLE

            claim_token = str(uuid.uuid4())

            for _batch_number in range(10):
                # 1. Claim batch with SKIP LOCKED and commit immediately so no row locks are held during Redis ops
                batch = await repo.claim_batch(
                    claim_token=claim_token,
                    lease_seconds=lease_seconds,
                    limit=limit,
                )
                await db.commit()

                if not batch:
                    # No unclaimed records found in this batch.
                    # Check if there are still records claimed by other workers
                    if await repo.has_pending():
                        return ReconciliationOutcome.FAILED
                    return ReconciliationOutcome.SUCCESS

                batch_ids = [r.id for r in batch]
                redis_ok = await self._invalidate_batch(batch)

                if not redis_ok:
                    # Release claim so retry can happen after cooldown
                    try:
                        await repo.release_claim(batch_ids, claim_token=claim_token)
                        await db.commit()
                    except Exception:
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                    return ReconciliationOutcome.CACHE_UNAVAILABLE

                # 3. Acknowledge and delete batch from PostgreSQL only after Redis success
                await repo.acknowledge_batch(batch_ids, claim_token=claim_token)
                await db.commit()

            return ReconciliationOutcome.FAILED

        except Exception as exc:
            try:
                await db.rollback()
            except Exception:
                pass
            logger.warning(
                "Failed to sync pending cache invalidations: %s",
                sanitize_log_message(str(exc)),
            )
            return ReconciliationOutcome.FAILED

    async def _invalidate_batch(self, batch: list[CachePendingInvalidation]) -> bool:
        client = self.client
        if not self.is_available or client is None:
            return False
        try:
            for namespace in sorted({r.namespace for r in batch if r.namespace}):
                keys = [r.key for r in batch if r.namespace == namespace and r.key]
                # DEL and version bump must be atomic relative to set_if_version.
                await client.eval(
                    """
                    for i = 2, #KEYS do redis.call('DEL', KEYS[i]) end
                    if not redis.call('GET', KEYS[1]) then
                        redis.call('SET', KEYS[1], 2)
                        return 2
                    end
                    return redis.call('INCR', KEYS[1])
                    """,
                    1 + len(keys), self.build_key(namespace, "version"), *keys,
                )
            orphan_keys = [r.key for r in batch if r.key and not r.namespace]
            if orphan_keys:
                await client.delete(*orphan_keys)
            self._record_success()
            return True
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._record_failure(exc)
            return False

    async def invalidate_product(
        self, product_id: int, db: AsyncSession | None = None,
    ) -> None:
        """Explicit invalidation; supplied sessions only stage intent (caller commits)."""
        if db is not None:
            await self.stage_product_invalidation(db, product_id)
            return
        await self.delete(self.build_key("products", "detail", str(product_id)))
        await self.bump_version("products")

    async def invalidate_category(
        self, category_id: int, db: AsyncSession | None = None,
    ) -> None:
        if db is not None:
            await self.stage_category_invalidation(db, category_id)
            return
        await self.delete(self.build_key("categories", "detail", str(category_id)))
        await self.bump_version("categories")
        await self.bump_version("products")


_default_cache_service: CacheService | None = None


def get_cache_service() -> CacheService:
    """Returns the shared CacheService instance."""
    global _default_cache_service
    if _default_cache_service is None:
        _default_cache_service = CacheService()
    return _default_cache_service
