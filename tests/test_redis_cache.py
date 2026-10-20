import asyncio
from decimal import Decimal
from typing import Any, cast

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import (
    close_redis_client,
    get_redis_client,
    init_redis_client,
    sanitize_log_message,
    sanitize_redis_url,
    set_redis_client,
)
from app.main import app as fastapi_app, lifespan
from app.models.category import Category
from app.models.product import Product
from app.models.user import User
from app.models.cache_invalidation import CachePendingInvalidation
from app.schemas.product import ProductRead
from app.services.cache import CacheService, get_cache_service


async def login_and_get_access_token(
    client: AsyncClient,
    email: str,
    password: str,
) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return cast(str, response.json()["access_token"])


@pytest.fixture
def cache_service() -> CacheService:
    return get_cache_service()


# ==============================================================================
# 1. Redis Lifecycle & Credential Sanitization
# ==============================================================================


async def test_redis_client_lifespan() -> None:
    """Verifies that FastAPI lifespan initializes and closes the pooled Redis client."""
    async with lifespan(fastapi_app):
        client = get_redis_client()
        assert client is not None
        assert await client.ping() is True

    # After lifespan shutdown, client is closed
    assert get_redis_client() is None


def test_credential_and_url_sanitization() -> None:
    assert "secret" not in sanitize_log_message("redis://:secret@localhost/0")
    """Verifies Redis URLs and log messages mask credentials to prevent secret leakage."""
    assert (
        sanitize_redis_url("redis://:super_secret_password@localhost:6379/0")
        == "redis://:***@localhost:6379/0"
    )
    assert (
        sanitize_redis_url("redis://app_user:prod_pass_123@redis-cluster.internal:6380/2?ssl=true")
        == "redis://app_user:***@redis-cluster.internal:6380/2?ssl=true"
    )
    assert sanitize_redis_url("redis://localhost:6379/0") == "redis://localhost:6379/0"
    assert sanitize_redis_url("") == ""

    err_msg = "Connection error to redis://user:hidden_pass@localhost:6379: timeout"
    sanitized = sanitize_log_message(err_msg)
    assert "hidden_pass" not in sanitized
    assert "redis://user:***@localhost:6379" in sanitized


# ==============================================================================
# 2. Cache Hit, Miss & Detail Caching
# ==============================================================================


async def test_product_detail_initial_miss_then_hit(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    db_session: AsyncSession,
) -> None:
    """
    1. Initial GET -> cache miss (fetched from PostgreSQL, populated into Redis with TTL).
    2. Subsequent GET -> cache hit (served directly from Redis).
    3. Direct DB update behind cache proves the cached value is returned.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    # Setup category and product
    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Electronics", "slug": "elec"})
    assert cat_resp.status_code == 201
    cat_id: int = cast(int, cat_resp.json()["id"])

    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Smartphone",
        "slug": "phone-1",
        "sku": "SKU-PH1",
        "price": 499.99,
        "category_id": cat_id,
    })
    assert prod_resp.status_code == 201
    prod_id: int = cast(int, prod_resp.json()["id"])

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    detail_key = cache.build_key("products", "detail", str(prod_id))
    # Ensure cache is cold
    await cache.delete(detail_key)
    assert await redis.get(detail_key) is None

    # Step 1: Initial GET -> Cache Miss
    get_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "Smartphone"
    assert get_resp.json()["price"] == "499.99"

    # Verify key was created in Redis
    raw_cached = await redis.get(detail_key)
    assert raw_cached is not None

    # Step 2: Directly mutate DB behind cache without invalidating
    await db_session.execute(
        update(Product).where(Product.id == prod_id).values(name="StealthDBChange")
    )
    await db_session.commit()

    # Step 3: Subsequent GET -> Cache Hit (returns "Smartphone", NOT "StealthDBChange")
    hit_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert hit_resp.status_code == 200
    assert hit_resp.json()["name"] == "Smartphone"

    # Step 4: After explicit cache deletion, next GET reads fresh DB value
    await cache.delete(detail_key)
    miss_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert miss_resp.status_code == 200
    assert miss_resp.json()["name"] == "StealthDBChange"


async def test_category_detail_cache_miss_and_hit(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    db_session: AsyncSession,
) -> None:
    """Verifies Category detail cache-aside flow and DB fallback."""
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Books", "slug": "books"})
    assert cat_resp.status_code == 201
    cat_id: int = cast(int, cat_resp.json()["id"])

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None
    cat_key = cache.build_key("categories", "detail", str(cat_id))

    # Read -> Miss -> Sets cache
    resp1 = await client.get(f"/api/v1/categories/{cat_id}", headers=customer_headers)
    assert resp1.status_code == 200
    assert resp1.json()["name"] == "Books"
    assert await redis.get(cat_key) is not None

    # Mutate DB directly
    await db_session.execute(
        update(Category).where(Category.id == cat_id).values(name="StealthBooks")
    )
    await db_session.commit()

    # Read -> Hit -> Serves "Books"
    resp2 = await client.get(f"/api/v1/categories/{cat_id}", headers=customer_headers)
    assert resp2.status_code == 200
    assert resp2.json()["name"] == "Books"


# ==============================================================================
# 3. Paginated Listing Keys & Filter Determinism
# ==============================================================================


async def test_paginated_listing_keys_and_filter_determinism(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Verifies:
    1. Pagination offsets and limits generate distinct cache keys.
    2. Filter combinations generate distinct cache keys.
    3. Query parameter order is normalized deterministically.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Fashion", "slug": "fashion"})
    cat_id: int = cast(int, cat_resp.json()["id"])

    for i in range(1, 5):
        await client.post("/api/v1/products", headers=admin_headers, json={
            "name": f"Shirt {i}",
            "slug": f"shirt-{i}",
            "sku": f"SKU-SH{i}",
            "price": 25.0 * i,
            "category_id": cat_id,
        })

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    # Page 1 vs Page 2
    r_page1 = await client.get("/api/v1/products?offset=0&limit=2", headers=customer_headers)
    assert r_page1.status_code == 200
    r_page2 = await client.get("/api/v1/products?offset=2&limit=2", headers=customer_headers)
    assert r_page2.status_code == 200

    version = await cache.get_version("products")
    key_page1 = cache.build_key("products", "list", f"v{version}", offset=0, limit=2)
    key_page2 = cache.build_key("products", "list", f"v{version}", offset=2, limit=2)

    assert key_page1 != key_page2
    assert await redis.get(key_page1) is not None
    assert await redis.get(key_page2) is not None

    # Filters: is_active=true vs is_active=false
    key_active_true = cache.build_key("products", "list", f"v{version}", is_active=True, limit=20, offset=0)
    key_active_false = cache.build_key("products", "list", f"v{version}", is_active=False, limit=20, offset=0)
    assert key_active_true != key_active_false

    # Query param order determinism test
    key_order_a = cache.build_key("products", "list", "v1", category_id=1, is_active=True, limit=20, offset=0)
    key_order_b = cache.build_key("products", "list", "v1", offset=0, is_active=True, category_id=1, limit=20)
    assert key_order_a == key_order_b
    assert key_order_a == "orderflow:v1:products:list:v1:category_id=1&is_active=true&limit=20&offset=0"


# ==============================================================================
# 4. Bounded TTL & Expiry
# ==============================================================================


async def test_cache_ttl_and_expiry(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """Verifies cached entries have bounded TTL and expire cleanly without arbitrary sleeps."""
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Garden", "slug": "garden"})
    cat_id: int = cast(int, cat_resp.json()["id"])
    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Hose", "slug": "hose", "sku": "SKU-HOSE", "price": 19.99, "category_id": cat_id
    })
    prod_id: int = cast(int, prod_resp.json()["id"])

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None
    detail_key = cache.build_key("products", "detail", str(prod_id))

    # Trigger caching
    resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert resp.status_code == 200

    # Verify TTL is bounded (<= settings.cache_default_ttl_seconds)
    ttl = await redis.ttl(detail_key)
    assert 0 < ttl <= settings.cache_default_ttl_seconds

    # Simulate deterministic expiry using Redis expire command
    _ = await redis.expire(detail_key, 0)
    assert await redis.get(detail_key) is None

    # Subsequent GET re-populates the cache
    resp2 = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert resp2.status_code == 200
    assert await redis.get(detail_key) is not None


# ==============================================================================
# 5. Cache Invalidation: Create, Update, Delete
# ==============================================================================


async def test_product_create_update_delete_invalidation(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Verifies that:
    1. Product creation invalidates listing version in O(1).
    2. Product update invalidates detail key and listing version.
    3. Product deletion invalidates detail key and listing version.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Appliances", "slug": "appliances"})
    cat_id: int = cast(int, cat_resp.json()["id"])

    # 1. Warm list cache
    v_start = await cache.get_version("products")
    list_resp = await client.get("/api/v1/products", headers=customer_headers)
    assert list_resp.status_code == 200

    # 2. Create product -> Invalidation increments version
    p_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Toaster", "slug": "toaster", "sku": "SKU-TOAST", "price": 29.99, "category_id": cat_id
    })
    assert p_resp.status_code == 201
    prod_id: int = cast(int, p_resp.json()["id"])

    v_after_create = await cache.get_version("products")
    assert v_after_create > v_start

    # New listing request contains the created product
    list_after_create = await client.get("/api/v1/products", headers=customer_headers)
    assert any(p["id"] == prod_id for p in list_after_create.json())

    # 3. Warm detail cache
    detail_key = cache.build_key("products", "detail", str(prod_id))
    det_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert det_resp.status_code == 200
    assert await redis.get(detail_key) is not None

    # 4. Update product -> Detail key deleted, version incremented
    upd_resp = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={
        "name": "Toaster Pro", "price": 39.99
    })
    assert upd_resp.status_code == 200
    assert await redis.get(detail_key) is None
    v_after_update = await cache.get_version("products")
    assert v_after_update > v_after_create

    # Fresh detail GET returns updated data
    det_resp2 = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert det_resp2.json()["name"] == "Toaster Pro"
    assert det_resp2.json()["price"] == "39.99"

    # 5. Delete product -> Detail key deleted, version incremented
    del_resp = await client.delete(f"/api/v1/products/{prod_id}", headers=admin_headers)
    assert del_resp.status_code == 204
    assert await redis.get(detail_key) is None
    v_after_delete = await cache.get_version("products")
    assert v_after_delete > v_after_update

    # Detail GET returns 404
    missing_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert missing_resp.status_code == 404


async def test_category_update_and_delete_invalidation(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Verifies Category mutation invalidation:
    1. Category detail key is cleared.
    2. Category listing version is bumped.
    3. Product listing version is also bumped to avoid stale category-product listings.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Furniture", "slug": "furniture"})
    cat_id: int = cast(int, cat_resp.json()["id"])

    # Warm category detail and product version
    cat_key = cache.build_key("categories", "detail", str(cat_id))
    _ = await client.get(f"/api/v1/categories/{cat_id}", headers=customer_headers)
    assert await redis.get(cat_key) is not None

    prod_ver_before = await cache.get_version("products")
    cat_ver_before = await cache.get_version("categories")

    # Update category
    upd_resp = await client.patch(f"/api/v1/categories/{cat_id}", headers=admin_headers, json={"name": "Luxury Furniture"})
    assert upd_resp.status_code == 200

    # Detail key deleted, both versions incremented
    assert await redis.get(cat_key) is None
    assert await cache.get_version("categories") > cat_ver_before
    assert await cache.get_version("products") > prod_ver_before

    # Delete category (create a temporary one with no products)
    cat2_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "TempCat", "slug": "tempcat"})
    cat2_id: int = cast(int, cat2_resp.json()["id"])
    cat2_key = cache.build_key("categories", "detail", str(cat2_id))
    _ = await client.get(f"/api/v1/categories/{cat2_id}", headers=customer_headers)
    assert await redis.get(cat2_key) is not None

    del_resp = await client.delete(f"/api/v1/categories/{cat2_id}", headers=admin_headers)
    assert del_resp.status_code == 204
    assert await redis.get(cat2_key) is None


# ==============================================================================
# 6. Database Rollback Atomicity (No Premature Cache Invalidation)
# ==============================================================================


async def test_db_rollback_does_not_mutate_or_invalidate_cache(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Verifies that if a database transaction fails (e.g. duplicate constraint violation)
    and rolls back, the cache is NEVER invalidated or mutated.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Sports", "slug": "sports"})
    cat_id: int = cast(int, cat_resp.json()["id"])

    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Ball", "slug": "ball", "sku": "SKU-BALL", "price": 15.0, "category_id": cat_id
    })
    prod_id: int = cast(int, prod_resp.json()["id"])

    detail_key = cache.build_key("products", "detail", str(prod_id))
    _ = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    cached_before = await redis.get(detail_key)
    assert cached_before is not None

    v_before = await cache.get_version("products")

    # Trigger a DB rollback via duplicate SKU conflict
    dup_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Another Ball", "slug": "another-ball", "sku": "SKU-BALL", "price": 20.0, "category_id": cat_id
    })
    assert dup_resp.status_code == 409

    # Cache state MUST NOT be altered
    v_after = await cache.get_version("products")
    assert v_after == v_before
    cached_after = await redis.get(detail_key)
    assert cached_after == cached_before


# ==============================================================================
# 7. Resilience & Degradation: Redis Outages & Malformed JSON
# ==============================================================================


async def test_redis_unavailable_get_fallback_to_postgres(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Verifies that if Redis becomes completely unavailable:
    1. Ordinary catalog reads continue reading from PostgreSQL.
    2. No HTTP 500 errors are returned to clients.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Tools", "slug": "tools"})
    cat_id: int = cast(int, cat_resp.json()["id"])
    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Hammer", "slug": "hammer", "sku": "SKU-HAMMER", "price": 12.5, "category_id": cat_id
    })
    prod_id: int = cast(int, prod_resp.json()["id"])

    # Simulate complete Redis unavailability by setting client to None
    orig_client = get_redis_client()
    try:
        set_redis_client(None)

        # GET detail must succeed via PostgreSQL without 500
        get_prod = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
        assert get_prod.status_code == 200
        assert get_prod.json()["name"] == "Hammer"

        # GET listing must succeed via PostgreSQL without 500
        list_prod = await client.get("/api/v1/products", headers=customer_headers)
        assert list_prod.status_code == 200
        assert any(p["id"] == prod_id for p in list_prod.json())

        # GET category must succeed via PostgreSQL without 500
        get_cat = await client.get(f"/api/v1/categories/{cat_id}", headers=customer_headers)
        assert get_cat.status_code == 200
        assert get_cat.json()["name"] == "Tools"
    finally:
        set_redis_client(orig_client)


async def test_redis_unavailable_mutations_persist_safely(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
) -> None:
    """
    Verifies that if Redis fails during a mutation:
    1. POST, PATCH, and DELETE operations still persist to PostgreSQL.
    2. Redis invalidation failure does NOT roll back the committed DB transaction.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    orig_client = get_redis_client()
    try:
        set_redis_client(None)

        # 1. Create category during Redis outage
        cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "OutageCat", "slug": "outage-cat"})
        assert cat_resp.status_code == 201
        cat_id: int = cast(int, cat_resp.json()["id"])

        # 2. Create product during Redis outage
        prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
            "name": "OutageProd", "slug": "outage-prod", "sku": "SKU-OUTAGE", "price": 10.0, "category_id": cat_id
        })
        assert prod_resp.status_code == 201
        prod_id: int = cast(int, prod_resp.json()["id"])

        # 3. Update product during Redis outage
        upd_resp = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={"price": 15.0})
        assert upd_resp.status_code == 200

        # Verify DB directly: data is committed and authoritative!
        db_prod = (
            await db_session.execute(select(Product).where(Product.id == prod_id))
        ).scalar_one_or_none()
        assert db_prod is not None
        assert db_prod.price == Decimal("15.00")
    finally:
        set_redis_client(orig_client)


async def test_malformed_cached_json_handled_safely(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Verifies that if corrupted / invalid JSON exists in Redis:
    1. The cache service logs a warning and treats it as a cache miss.
    2. The endpoint returns 200 OK with fresh data from PostgreSQL.
    3. The corrupted key is replaced or deleted.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Audio", "slug": "audio"})
    cat_id: int = cast(int, cat_resp.json()["id"])
    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Headphones", "slug": "headphones", "sku": "SKU-HP", "price": 89.99, "category_id": cat_id
    })
    prod_id: int = cast(int, prod_resp.json()["id"])

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None
    detail_key = cache.build_key("products", "detail", str(prod_id))

    # Inject corrupt non-JSON string into Redis
    await redis.set(detail_key, "{malformed: [json! NOT_VALID")

    # Call GET endpoint: must handle gracefully and return 200 from PostgreSQL
    resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Headphones"

    # Verify key was refreshed with valid JSON
    new_cached = await redis.get(detail_key)
    assert new_cached is not None
    parsed = ProductRead.model_validate_json(new_cached)
    assert parsed.name == "Headphones"


# ==============================================================================
# 8. Concurrent Read/Write Race & TTL Consistency Bound
# ==============================================================================


async def test_concurrent_read_write_invalidation_race(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    db_session: AsyncSession,
) -> None:
    """
    Demonstrates and tests the classic cache-aside race condition:
    1. Reader task misses cache and fetches old state from PostgreSQL.
    2. Writer task concurrently updates PostgreSQL, commits, and deletes cache key.
    3. Reader task resumes and writes its old state to cache.
    4. Bounded TTL acts as the fallback consistency bound; key expires and recovers.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "RaceCat", "slug": "race-cat"})
    cat_id: int = cast(int, cat_resp.json()["id"])
    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "RaceProd", "slug": "race-prod", "sku": "SKU-RACE", "price": 10.0, "category_id": cat_id
    })
    prod_id: int = cast(int, prod_resp.json()["id"])

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None
    detail_key = cache.build_key("products", "detail", str(prod_id))
    await cache.delete(detail_key)

    reader_read_db_event = asyncio.Event()
    writer_finished_event = asyncio.Event()

    stale_product_data: dict[str, Any] = {}

    async def simulated_reader() -> None:
        # Reader reads old row from DB
        db_row = (await db_session.execute(select(Product).where(Product.id == prod_id))).scalar_one()
        stale_product_data.update(ProductRead.model_validate(db_row).model_dump(mode="json"))

        reader_read_db_event.set()
        await writer_finished_event.wait()

        # Reader writes stale data into Redis with short TTL
        _ = await cache.set(detail_key, stale_product_data, ttl=60)

    async def simulated_writer() -> None:
        await reader_read_db_event.wait()

        # Writer updates product to 20.0, commits DB and invalidates cache
        resp = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={"price": 20.0})
        assert resp.status_code == 200
        writer_finished_event.set()

    # Run concurrently
    await asyncio.gather(simulated_reader(), simulated_writer())

    # Cache now contains the stale value from the delayed reader
    cached_val = await redis.get(detail_key)
    assert cached_val is not None
    assert '"price": "10.00"' in str(cached_val)

    # Documented fallback: TTL bounds the staleness. Simulating TTL expiry restores consistency.
    _ = await redis.expire(detail_key, 0)
    recovered_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert recovered_resp.status_code == 200
    assert recovered_resp.json()["price"] == "20.00"


# ==============================================================================
# 9. Authorization Boundaries (No Cache Leakage)
# ==============================================================================


async def test_no_cache_leakage_across_authorization_boundaries(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Verifies that cached catalog data is never accessible across authorization boundaries:
    1. Unauthenticated requests are rejected with 401 without returning cached data.
    2. Invalid or expired tokens receive 401.
    3. Customers cannot execute mutations even if cached data is present.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "SecureCat", "slug": "sec-cat"})
    cat_id: int = cast(int, cat_resp.json()["id"])
    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "SecureProd", "slug": "sec-prod", "sku": "SKU-SEC", "price": 99.0, "category_id": cat_id
    })
    prod_id: int = cast(int, prod_resp.json()["id"])

    # Warm cache using authorized customer
    auth_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert auth_resp.status_code == 200

    # Unauthenticated request: MUST receive 401
    unauth_resp = await client.get(f"/api/v1/products/{prod_id}")
    assert unauth_resp.status_code == 401

    # Invalid token: MUST receive 401
    bad_token_resp = await client.get(f"/api/v1/products/{prod_id}", headers={"Authorization": "Bearer invalid_token"})
    assert bad_token_resp.status_code == 401

    # Customer mutation: MUST receive 403 Forbidden
    cust_mut_resp = await client.delete(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert cust_mut_resp.status_code == 403


async def test_circuit_breaker_cooldown_bypasses_network_calls() -> None:
    """
    Verifies that when a Redis failure occurs, the cache service enters
    a circuit breaker cooldown period, immediately bypassing Redis without
    incurring repeated connection timeout latency on subsequent requests.
    """
    class FailingRedis:
        def __init__(self) -> None:
            self.call_count: int = 0

        async def get(self, _key: str) -> None:
            self.call_count += 1
            raise TimeoutError("Connection timed out")

    stub = FailingRedis()
    service = CacheService(redis_client=cast(Redis, cast(object, stub)), cooldown_seconds=2.0)

    # Initial get triggers failure and enters cooldown
    assert service.is_available is True
    res1 = await service.get("test:key")
    assert res1 is None
    assert stub.call_count == 1

    # Immediate second call: is_available is False, Redis network call bypassed
    assert service.is_available is False
    res2 = await service.get("test:key")
    assert res2 is None
    # Call count MUST remain 1 (no network attempt during cooldown!)
    assert stub.call_count == 1


# ==============================================================================
# Finding 1 Regression: Version Initialization Bug
# ==============================================================================


async def test_version_initialization_on_absent_key_invalidates_cleanly(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Finding 1 Regression Test:
    Ensures that when the Redis namespace version key is absent (defaults to 1),
    the first mutation transitions it to 2 (instead of 1), so that
    the next GET strictly invalidates the v1 cached listing and fetches fresh PostgreSQL data.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    # Step 1: Explicitly delete namespace version key and verify it is absent
    version_key = cache.build_key("products", "version")
    _ = await redis.delete(version_key)
    assert await redis.get(version_key) is None
    # get_version returns default 1 when key is absent
    assert await cache.get_version("products") == 1

    # Create category for products
    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Gaming", "slug": "gaming"})
    assert cat_resp.status_code == 201
    cat_id: int = cast(int, cat_resp.json()["id"])

    # Create initial product
    prod1_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Gamepad Pro", "slug": "gamepad-pro", "sku": "SKU-GP1", "price": 59.99, "category_id": cat_id
    })
    assert prod1_resp.status_code == 201

    # Reset version key again to be 100% sure we start test with absent key
    _ = await redis.delete(version_key)
    assert await redis.get(version_key) is None
    assert await cache.get_version("products") == 1

    # Step 2: Warm product listing cache under absent version (evaluates to v1)
    list_resp1 = await client.get("/api/v1/products", headers=customer_headers)
    assert list_resp1.status_code == 200
    items1 = cast(list[dict[str, object]], list_resp1.json())
    assert len(items1) == 1
    assert items1[0]["name"] == "Gamepad Pro"

    # Verify listing is cached in Redis under v1 key
    v1_list_key = cache.build_key("products", "list", "v1", limit=20, offset=0)
    cached_v1 = await redis.get(v1_list_key)
    assert cached_v1 is not None

    # Step 3: Perform committed product mutation (create a second product)
    prod2_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Gaming Mouse", "slug": "gaming-mouse", "sku": "SKU-GM1", "price": 39.99, "category_id": cat_id
    })
    assert prod2_resp.status_code == 201

    # Verify version key in Redis transitioned to 2 (NOT 1!)
    current_version = await cache.get_version("products")
    assert current_version == 2
    raw_v = await redis.get(version_key)
    assert raw_v is not None
    assert int(raw_v.decode() if isinstance(raw_v, bytes) else str(raw_v)) == 2

    # Step 4: Next GET must return fresh PostgreSQL data (both products)
    list_resp2 = await client.get("/api/v1/products", headers=customer_headers)
    assert list_resp2.status_code == 200
    items2 = cast(list[dict[str, object]], list_resp2.json())
    assert len(items2) == 2
    names = [str(p["name"]) for p in items2]
    assert "Gamepad Pro" in names
    assert "Gaming Mouse" in names

    # Verify new listing is cached under v2 key
    v2_list_key = cache.build_key("products", "list", "v2", limit=20, offset=0)
    cached_v2 = await redis.get(v2_list_key)
    assert cached_v2 is not None


async def test_category_version_initialization_on_absent_key_invalidates_cleanly(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Finding 1 Regression Test for Categories:
    Ensures that starting with absent category version key, a mutation transitions
    version from default 1 to 2, causing next listing GET to return fresh DB data.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    # Step 1: Ensure category version key is absent
    cat_version_key = cache.build_key("categories", "version")
    _ = await redis.delete(cat_version_key)
    assert await redis.get(cat_version_key) is None
    assert await cache.get_version("categories") == 1

    # Create initial category
    c1 = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Furniture", "slug": "furniture"})
    assert c1.status_code == 201

    # Reset version key to absent to test warm cache under absent key
    _ = await redis.delete(cat_version_key)
    assert await redis.get(cat_version_key) is None

    # Step 2: Warm category listing cache
    resp1 = await client.get("/api/v1/categories", headers=customer_headers)
    assert resp1.status_code == 200
    c_items1 = cast(list[dict[str, object]], resp1.json())
    assert len(c_items1) == 1
    assert c_items1[0]["name"] == "Furniture"

    # Step 3: Perform committed mutation (create second category)
    c2 = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Lighting", "slug": "lighting"})
    assert c2.status_code == 201

    # Verify version key transitioned to 2
    assert await cache.get_version("categories") == 2

    # Step 4: Next GET returns fresh DB data with both categories
    resp2 = await client.get("/api/v1/categories", headers=customer_headers)
    assert resp2.status_code == 200
    c_items2 = cast(list[dict[str, object]], resp2.json())
    cat_names = [str(c["name"]) for c in c_items2]
    assert "Furniture" in cat_names
    assert "Lighting" in cat_names


# ==============================================================================
# Finding 2 Regression: Cache Consistency After Redis Outage (Multi-Worker Safe)
# ==============================================================================


async def test_redis_outage_invalidation_failure_and_multi_worker_recovery(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    db_session: AsyncSession,
) -> None:
    """
    Finding 2 Regression Test:
    1. Warm cache for product detail.
    2. Simulate Redis outage during a committed DB mutation (update price).
    3. Verify PostgreSQL write succeeds (preserves DB write).
    4. Verify pending invalidation is persisted in PostgreSQL (for multi-worker sync).
    5. While Redis is down, assert GET falls back to DB and returns fresh updated price.
    6. Simulate Redis recovery (Redis still contains stale pre-outage cache entry).
    7. Simulate an independent worker process performing GET.
    8. Assert recovered Redis CANNOT serve the known-stale entry.
    9. Assert response returns fresh data and updates Redis with the new value.
    10. Assert pending invalidation table in PostgreSQL is cleanly reconciled.
    """
    from unittest.mock import patch
    from redis.exceptions import ConnectionError as RedisConnError

    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cache = get_cache_service()
    real_redis = cache.client
    assert real_redis is not None

    # Step 1: Create product and warm cache
    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Audio", "slug": "audio"})
    cat_id: int = cast(int, cat_resp.json()["id"])
    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Headphones", "slug": "headphones", "sku": "SKU-HP1", "price": 100.0, "category_id": cat_id
    })
    prod_id: int = cast(int, prod_resp.json()["id"])

    detail_key = cache.build_key("products", "detail", str(prod_id))

    # Read to warm cache
    warm_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert warm_resp.status_code == 200
    assert warm_resp.json()["price"] == "100.00"
    assert await real_redis.get(detail_key) is not None

    # Step 2 & 3: Simulate Redis outage during committed DB mutation.
    # We patch CacheService.delete and bump_version to raise a RedisError during update
    with patch.object(real_redis, "eval", side_effect=RedisConnError("Redis connection lost")):
        update_resp = await client.patch(
            f"/api/v1/products/{prod_id}",
            headers=admin_headers,
            json={"price": 150.0},
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["price"] == "150.00"

    # Step 4: Verify PostgreSQL write succeeded and pending invalidation was recorded in DB
    check_stmt = select(CachePendingInvalidation).where(CachePendingInvalidation.key == detail_key)
    pending_records = (await db_session.execute(check_stmt)).scalars().all()
    assert len(pending_records) >= 1
    assert pending_records[0].key == detail_key

    # Notice: Redis still holds the old pre-outage cache entry ("100.00")!
    raw_stale = await real_redis.get(detail_key)
    assert raw_stale is not None
    raw_stale_str = raw_stale.decode() if isinstance(raw_stale, bytes) else str(raw_stale)
    assert "100.0" in raw_stale_str

    # Step 5: While Redis is unavailable, read fallback to PostgreSQL works
    with patch.object(real_redis, "delete", side_effect=RedisConnError("Redis still down")), \
         patch.object(real_redis, "get", side_effect=RedisConnError("Redis still down")):
        down_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
        assert down_resp.status_code == 200
        assert down_resp.json()["price"] == "150.00"

    # Step 6 & 7: Redis recovers! Subsequent GET executed by any worker process
    # Clear circuit breaker cooldown to simulate Redis connectivity restoration
    cache._record_success()
    # Old stale entry is still physically in Redis until synced by the recovery mechanism.
    # Now simulate a subsequent GET request:
    recov_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert recov_resp.status_code == 200

    # Step 8: Assert recovered Redis CANNOT serve the known-stale entry ("100.00")
    assert recov_resp.json()["price"] == "150.00"

    # Step 9: Assert Redis now holds fresh data ($150.00)
    raw_updated = await real_redis.get(detail_key)
    assert raw_updated is not None
    raw_updated_str = raw_updated.decode() if isinstance(raw_updated, bytes) else str(raw_updated)
    assert "150.0" in raw_updated_str

    # Step 10: Assert pending invalidations table in PostgreSQL is now empty
    pending_after = (await db_session.execute(check_stmt)).scalars().all()
    assert len(pending_after) == 0


# ==============================================================================
# Finding 3 Regression: Corrupt List Payload Handling
# ==============================================================================


async def test_corrupt_product_list_payload_rejects_and_falls_back_to_db(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Finding 3 Regression Test for Products:
    If a cached product list in Redis contains any malformed item (e.g. non-dict
    or schema validation failure), the entire cached payload must be rejected,
    purged from Redis, and fresh authoritative data fetched from PostgreSQL.
    A partial list must NEVER be returned.
    """
    import json

    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Monitors", "slug": "monitors"})
    cat_id: int = cast(int, cat_resp.json()["id"])

    # Create 2 products
    p1 = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "4K Monitor", "slug": "4k-mon", "sku": "SKU-M4K", "price": 399.99, "category_id": cat_id
    })
    p2 = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "1440p Monitor", "slug": "2k-mon", "sku": "SKU-M2K", "price": 249.99, "category_id": cat_id
    })
    assert p1.status_code == 201 and p2.status_code == 201

    # Case A: One item in cached list is a non-dict string corrupting the list
    version = await cache.get_version("products")
    list_key = cache.build_key("products", "list", f"v{version}", limit=20, offset=0)

    p1_data = cast(dict[str, object], p1.json())
    # Corrupted payload: 1 valid dict, 1 non-dict corrupt item
    corrupted_payload: list[object] = [p1_data, "CORRUPT_NON_DICT_STRING"]
    _ = await redis.set(list_key, json.dumps(corrupted_payload))

    # GET request: must NOT return a partial list [p1]!
    # Must reject entire list, purge key, query DB, and return full 2 items!
    resp_a = await client.get("/api/v1/products", headers=customer_headers)
    assert resp_a.status_code == 200
    items_a = cast(list[dict[str, object]], resp_a.json())
    assert len(items_a) == 2  # NOT 1!
    names_a = [str(p["name"]) for p in items_a]
    assert "4K Monitor" in names_a
    assert "1440p Monitor" in names_a

    # Case B: One item in cached list is a dict that fails Pydantic schema validation
    # (e.g. missing required field 'sku' or invalid 'price')
    corrupted_pydantic_payload: list[object] = [
        p1_data,
        {"id": 999, "name": "Broken Schema", "price": "invalid_non_numeric"}
    ]
    _ = await redis.set(list_key, json.dumps(corrupted_pydantic_payload))

    resp_b = await client.get("/api/v1/products", headers=customer_headers)
    assert resp_b.status_code == 200
    items_b = cast(list[dict[str, object]], resp_b.json())
    assert len(items_b) == 2  # NOT 1!
    names_b = [str(p["name"]) for p in items_b]
    assert "4K Monitor" in names_b
    assert "1440p Monitor" in names_b


async def test_corrupt_category_list_payload_rejects_and_falls_back_to_db(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """
    Finding 3 Regression Test for Categories:
    Ensures that a category listing cache containing non-dict or invalid schema items
    is completely rejected and purged, returning fresh full data from PostgreSQL.
    """
    import json

    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    c1 = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Kitchen", "slug": "kitchen"})
    c2 = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Garden", "slug": "garden"})
    assert c1.status_code == 201 and c2.status_code == 201

    version = await cache.get_version("categories")
    cat_list_key = cache.build_key("categories", "list", f"v{version}", limit=20, offset=0)

    # Invalidate cache with corrupted list containing non-dict item
    corrupted_data: list[object] = [cast(dict[str, object], c1.json()), 12345]  # 12345 is non-dict
    _ = await redis.set(cat_list_key, json.dumps(corrupted_data))

    resp = await client.get("/api/v1/categories", headers=customer_headers)
    assert resp.status_code == 200
    items = cast(list[dict[str, object]], resp.json())
    assert len(items) == 2  # NOT 1!
    names = [str(c["name"]) for c in items]
    assert "Kitchen" in names
    assert "Garden" in names


# ==============================================================================
# Additional Verification: In-Flight Race Condition Guard
# ==============================================================================


async def test_read_version_db_query_cache_set_race_guard(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
) -> None:
    """
    Verifies that if a concurrent mutation bumps the namespace version
    while a DB query is in-flight, the stale in-flight query results are NOT
    written into the cache (optimistic version guard).
    """
    from unittest.mock import patch
    from app.services.product import ProductService

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Hardware", "slug": "hw"})
    cat_id: int = cast(int, cat_resp.json()["id"])
    _ = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Screwdriver", "slug": "screw-1", "sku": "SKU-SC1", "price": 9.99, "category_id": cat_id
    })

    version_before = await cache.get_version("products")
    list_key = cache.build_key("products", "list", f"v{version_before}", limit=20, offset=0)
    _ = await cache.delete(list_key)

    service = ProductService(db_session, cache=cache)
    prod_repo = service.product_repo

    # Intercept list call: right after DB query, simulate concurrent bump_version
    orig_list = prod_repo.list

    async def mock_list(
        offset: int = 0,
        limit: int = 100,
        category_id: int | None = None,
        is_active: bool | None = None,
    ) -> list[Product]:
        res = await orig_list(offset=offset, limit=limit, category_id=category_id, is_active=is_active)
        _ = await cache.bump_version("products")
        return res

    with patch.object(prod_repo, "list", side_effect=mock_list):
        result = await service.list_products(offset=0, limit=20)
        assert len(result) >= 1

    # Assert that list_key under the old version was NOT written to cache
    cached = await redis.get(list_key)
    assert cached is None


# ==============================================================================
# Fix 1 & 2 & 3: Crash Recovery, Fail-Safe Reconciliation & Startup Resilience
# ==============================================================================


async def test_simulated_crash_between_commit_and_redis_invalidation(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    db_session: AsyncSession,
) -> None:
    """
    Fix 1 Regression Test:
    Simulates a process crash immediately following PostgreSQL commit
    (before execute_staged_invalidations is called or can acknowledge).
    1. Product and invalidation intent are committed atomically in PostgreSQL.
    2. Process crashes before Redis invalidation can succeed.
    3. Pending event is durably retained in PostgreSQL.
    4. Subsequent request by an independent worker reconciles the event,
       purges the stale Redis entry, serves fresh DB data, and clears the pending event.
    """
    from unittest.mock import patch

    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    # Step 1: Create category and product
    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "CrashCat", "slug": "crash-cat"})
    cat_id: int = cast(int, cat_resp.json()["id"])
    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "CrashProd", "slug": "crash-prod", "sku": "SKU-CRASH", "price": 50.0, "category_id": cat_id
    })
    prod_id: int = cast(int, prod_resp.json()["id"])
    detail_key = cache.build_key("products", "detail", str(prod_id))

    # Warm cache
    warm_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert warm_resp.status_code == 200
    assert warm_resp.json()["price"] == "50.00"
    assert await redis.get(detail_key) is not None

    # Step 2: Simulate process crash between commit and Redis invalidation
    with patch.object(cache, "execute_staged_invalidations", side_effect=RuntimeError("Simulated SIGKILL / process crash")):
        with pytest.raises(RuntimeError, match="Simulated SIGKILL"):
            _ = await client.patch(
                f"/api/v1/products/{prod_id}",
                headers=admin_headers,
                json={"price": 75.0},
            )

    # Step 3: Verify PostgreSQL state:
    # Mutation was committed atomically with the durable pending invalidation intent!
    db_prod = (await db_session.execute(select(Product).where(Product.id == prod_id))).scalar_one()
    assert db_prod.price == Decimal("75.00")

    check_stmt = select(CachePendingInvalidation).where(CachePendingInvalidation.key == detail_key)
    pending_records = (await db_session.execute(check_stmt)).scalars().all()
    assert len(pending_records) >= 1
    assert pending_records[0].key == detail_key

    # Redis still contains stale pre-crash value because invalidation was interrupted by crash
    raw_stale = await redis.get(detail_key)
    assert raw_stale is not None
    assert "50.0" in str(raw_stale)

    # Step 4: Recovery on subsequent request (simulating independent worker)
    # The subsequent read invokes sync_pending_invalidations, purging stale Redis data
    recov_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert recov_resp.status_code == 200
    assert recov_resp.json()["price"] == "75.00"

    # Verify Redis is updated with the fresh value
    raw_updated = await redis.get(detail_key)
    assert raw_updated is not None
    assert "75.0" in str(raw_updated)

    # Verify pending invalidation table in PostgreSQL was acknowledged and cleared
    pending_after = (await db_session.execute(check_stmt)).scalars().all()
    assert len(pending_after) == 0


async def test_fail_safe_reconciliation_bypasses_redis_on_failure(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    db_session: AsyncSession,
) -> None:
    """
    Fix 2 Regression Test:
    1. Warm cache with product detail.
    2. Simulate an unresolved invalidation intent persisted in PostgreSQL.
    3. Simulate PostgreSQL reconciliation failure in sync_pending_invalidations (returning False).
    4. Assert that Redis is completely bypassed for the request and PostgreSQL is read directly.
    5. Assert stale Redis entries are NOT served while unresolved invalidations exist.
    6. Verify that once reconciliation succeeds, Redis is updated and pending events are cleared.
    """
    from unittest.mock import patch

    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cache = get_cache_service()
    redis = cache.client
    assert redis is not None

    # Step 1: Create category and product, warm cache
    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "ReconcileCat", "slug": "reconcile-cat"})
    cat_id: int = cast(int, cat_resp.json()["id"])
    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "ReconcileProd", "slug": "reconcile-prod", "sku": "SKU-REC", "price": 100.0, "category_id": cat_id
    })
    prod_id: int = cast(int, prod_resp.json()["id"])
    detail_key = cache.build_key("products", "detail", str(prod_id))

    warm_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert warm_resp.status_code == 200
    assert warm_resp.json()["price"] == "100.00"

    # Step 2: Directly update product in DB to 125.0 and record pending invalidation
    await db_session.execute(
        update(Product).where(Product.id == prod_id).values(price=Decimal("125.00"))
    )
    pending_event = CachePendingInvalidation(key=detail_key, namespace="products")
    db_session.add(pending_event)
    await db_session.commit()

    # Redis still has stale "100.00"
    stale_in_redis = await redis.get(detail_key)
    assert stale_in_redis is not None
    assert "100.0" in str(stale_in_redis)

    # Step 3: Simulate failure in sync_pending_invalidations (e.g. PostgreSQL lock or query failure)
    with patch.object(cache, "sync_pending_invalidations", return_value=False):
        # Step 4 & 5: Request must bypass Redis and read fresh PostgreSQL state (125.00)
        # NEVER serving the stale 100.00 entry in Redis!
        read_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
        assert read_resp.status_code == 200
        assert read_resp.json()["price"] == "125.00"

        # Also verify listing bypasses Redis when sync fails
        list_resp = await client.get("/api/v1/products", headers=customer_headers)
        assert list_resp.status_code == 200
        matched = [p for p in list_resp.json() if p["id"] == prod_id]
        assert len(matched) == 1
        assert matched[0]["price"] == "125.00"

    # Step 6: When reconciliation succeeds, sync executes and updates Redis
    recov_resp = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert recov_resp.status_code == 200
    assert recov_resp.json()["price"] == "125.00"

    # Assert pending event in PostgreSQL is now acknowledged and deleted
    check_stmt = select(CachePendingInvalidation).where(CachePendingInvalidation.key == detail_key)
    pending_after = (await db_session.execute(check_stmt)).scalars().all()
    assert len(pending_after) == 0


async def test_startup_recovery_with_redis_unavailable_and_bounded_reconnect(
    client: AsyncClient,
    customer_user: User,
) -> None:
    """
    Fix 3 Regression Test:
    1. Simulates Redis being completely offline during FastAPI lifespan startup.
    2. Verifies application starts successfully in degraded mode with zero crashes.
    3. Verifies normal API availability (/health, /api/v1/products with auth).
    4. Verifies bounded reconnect strategy does not repeatedly block requests during cooldown.
    5. Verifies automatic reconnection when Redis becomes reachable after cooldown.
    """
    import time
    import redis.asyncio as aioredis
    from unittest.mock import patch
    from redis.exceptions import ConnectionError as RedisConnError

    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    # 1. Simulate Redis completely offline during startup lifespan
    orig_client = get_redis_client()
    try:
        set_redis_client(None)
        with patch.object(aioredis, "from_url", side_effect=RedisConnError("Cannot connect to Redis host")):
            async with lifespan(fastapi_app):
                # Startup succeeded without exception
                assert get_redis_client() is None

                # 2. Normal API availability without Redis
                health_resp = await client.get("/health")
                assert health_resp.status_code == 200
                assert health_resp.json() == {"status": "ok"}

                prods_resp = await client.get("/api/v1/products", headers=customer_headers)
                assert prods_resp.status_code == 200

                # 3. Test bounded reconnect strategy:
                # During cooldown, ensure_connected returns None without making network attempts
                cache_instance = CacheService(reconnect_cooldown=5.0)
                cache_instance._last_reconnect_time = time.monotonic()
                assert await cache_instance.ensure_connected() is None

        # 4. Once cooldown elapses and Redis is available, ensure_connected reconnects
        recovered_cache = CacheService(reconnect_cooldown=0.0)
        recovered_client = await recovered_cache.ensure_connected()
        assert recovered_client is not None
        assert await recovered_client.ping() is True
    finally:
        set_redis_client(orig_client)







async def test_outbox_rollback_and_commit_are_atomic(db_session: AsyncSession) -> None:
    from tests.conftest import TestSessionLocal
    from app.repositories.cache_invalidation import CacheInvalidationRepository

    category = Category(name="Atomic", slug="atomic")
    db_session.add(category)
    await db_session.flush()
    await CacheInvalidationRepository(db_session).stage(namespace="categories")
    async with TestSessionLocal() as observer:
        assert (await observer.execute(select(Category))).scalars().all() == []
        assert (await observer.execute(select(CachePendingInvalidation))).scalars().all() == []
    await db_session.rollback()
    assert (await db_session.execute(select(Category))).scalars().all() == []
    assert (await db_session.execute(select(CachePendingInvalidation))).scalars().all() == []
    db_session.add(Category(name="Committed", slug="committed"))
    await CacheInvalidationRepository(db_session).stage(namespace="categories")
    await db_session.commit()
    async with TestSessionLocal() as observer:
        assert len((await observer.execute(select(Category))).scalars().all()) == 1
        assert len((await observer.execute(select(CachePendingInvalidation))).scalars().all()) == 1


async def test_reconciliation_independent_workers_and_ack_replay(db_session: AsyncSession) -> None:
    from unittest.mock import patch
    from tests.conftest import TestSessionLocal
    from app.repositories.cache_invalidation import CacheInvalidationRepository
    from app.services.cache import ReconciliationOutcome

    cache = get_cache_service()
    cache._record_success()
    await CacheInvalidationRepository(db_session).stage(namespace="products")
    await db_session.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    original = cache._invalidate_batch

    async def pause(batch: list[CachePendingInvalidation]) -> bool:
        entered.set()
        await release.wait()
        return await original(batch)

    async with TestSessionLocal() as worker:
        with patch.object(cache, "_invalidate_batch", side_effect=pause):
            task = asyncio.create_task(cache.sync_pending_invalidations(worker))
            await asyncio.wait_for(entered.wait(), timeout=5)
            try:
                assert await cache.sync_pending_invalidations(db_session) == ReconciliationOutcome.FAILED
            finally:
                release.set()
                assert await task == ReconciliationOutcome.SUCCESS

    await CacheInvalidationRepository(db_session).stage(namespace="products")
    await db_session.commit()
    version = await cache.get_version("products")
    with patch.object(CacheInvalidationRepository, "acknowledge_batch", side_effect=RuntimeError("ack failed")):
        assert await cache.sync_pending_invalidations(db_session) == ReconciliationOutcome.FAILED
    assert await cache.get_version("products") > version
    assert len((await db_session.execute(select(CachePendingInvalidation))).scalars().all()) == 1
    # Expire lease deterministically; replay repeats safe deletion/version advancement.
    assert await cache.sync_pending_invalidations(db_session, lease_seconds=0) == ReconciliationOutcome.SUCCESS
    assert (await db_session.execute(select(CachePendingInvalidation))).scalars().all() == []


@pytest.mark.parametrize("namespace", ["products", "categories"])
async def test_atomic_population_fence(namespace: str) -> None:
    from app.models.cache_invalidation import CachePendingInvalidation
    cache = get_cache_service()
    cache._record_success()
    key = cache.build_key(namespace, "detail", "123")
    version = await cache.get_version(namespace)
    assert await cache.set_if_version(key, {"old": True}, namespace, version)
    assert await cache._invalidate_batch([CachePendingInvalidation(key=key, namespace=namespace)])
    assert not await cache.set_if_version(key, {"old": True}, namespace, version)
    assert await cache.get(key) is None


@pytest.mark.parametrize("service_name", ["product", "category"])
async def test_failed_reconciliation_never_gets_or_sets_cache(db_session: AsyncSession, service_name: str) -> None:
    from unittest.mock import AsyncMock, patch
    from app.services.product import ProductService
    from app.services.category import CategoryService
    from app.services.cache import ReconciliationOutcome
    cache = get_cache_service()
    service = ProductService(db_session, cache) if service_name == "product" else CategoryService(db_session, cache)
    with patch.object(cache, "sync_pending_invalidations", return_value=ReconciliationOutcome.FAILED), \
         patch.object(cache, "get", new_callable=AsyncMock) as get, \
         patch.object(cache, "set_if_version", new_callable=AsyncMock) as put:
        if service_name == "product":
            assert await service.list_products() == []
        else:
            assert await service.list_categories() == []
        get.assert_not_awaited()
        put.assert_not_awaited()


async def test_reconciliation_does_not_commit_caller_work(db_session: AsyncSession) -> None:
    cache = get_cache_service()
    db_session.add(Category(name="Caller", slug="caller"))
    await cache.sync_pending_invalidations(db_session)
    await db_session.rollback()
    assert (await db_session.execute(select(Category))).scalars().all() == []


async def test_service_staging_failure_rolls_back_business_change(db_session: AsyncSession) -> None:
    from unittest.mock import patch
    from tests.conftest import TestSessionLocal
    from app.schemas.category import CategoryCreate
    from app.services.category import CategoryService
    cache = get_cache_service()
    original = cache.stage_category_invalidation

    async def fail_after_staging(db: AsyncSession, category_id: int) -> list[CachePendingInvalidation]:
        await original(db, category_id)
        raise RuntimeError("forced transaction rollback")

    with patch.object(cache, "stage_category_invalidation", side_effect=fail_after_staging):
        with pytest.raises(RuntimeError, match="forced transaction rollback"):
            await CategoryService(db_session, cache).create_category(CategoryCreate(name="Rollback", slug="rollback"))
    async with TestSessionLocal() as observer:
        assert (await observer.execute(select(Category))).scalars().all() == []
        assert (await observer.execute(select(CachePendingInvalidation))).scalars().all() == []


async def test_catalog_write_rejects_caller_transaction(db_session: AsyncSession) -> None:
    from app.schemas.category import CategoryCreate
    from app.services.category import CategoryService
    async with db_session.begin():
        with pytest.raises(RuntimeError, match="service-owned"):
            await CategoryService(db_session).create_category(CategoryCreate(name="Owned", slug="owned"))
    assert (await db_session.execute(select(Category))).scalars().all() == []


async def test_detail_reader_cannot_populate_after_concurrent_invalidation(db_session: AsyncSession) -> None:
    from unittest.mock import patch
    from app.services.category import CategoryService
    from app.schemas.category import CategoryCreate
    cache = get_cache_service()
    cache._record_success()
    service = CategoryService(db_session, cache)
    category = await service.create_category(CategoryCreate(name="Fence", slug="fence"))
    key = cache.build_key("categories", "detail", str(category.id))
    original = cache.set_if_version

    async def invalidate_before_population(key: str, value: object, namespace: str, version: int) -> bool:
        assert await cache._invalidate_batch([CachePendingInvalidation(key=key, namespace=namespace)])
        return await original(key, value, namespace, version)

    with patch.object(cache, "set_if_version", side_effect=invalidate_before_population):
        assert (await service.get_category(category.id)).name == "Fence"
    assert await cache.get(key) is None


async def test_catalog_write_does_not_adopt_flushed_caller_work(db_session: AsyncSession) -> None:
    from app.schemas.category import CategoryCreate
    from app.services.category import CategoryService
    db_session.add(Category(name="Caller flushed", slug="caller-flushed"))
    await db_session.flush()
    with pytest.raises(RuntimeError, match="service-owned"):
        await CategoryService(db_session).create_category(CategoryCreate(name="Service", slug="service"))
    await db_session.rollback()
    assert (await db_session.execute(select(Category))).scalars().all() == []


async def test_disabled_cache_retains_intent_for_future_enablement(db_session: AsyncSession) -> None:
    from unittest.mock import patch
    cache = get_cache_service()
    with patch.object(settings, "cache_enabled", False):
        events = await cache.stage_product_invalidation(db_session, 123)
        await db_session.commit()
        assert len(events) == 1
        await cache.execute_staged_invalidations(db_session, events)
        assert len((await db_session.execute(select(CachePendingInvalidation))).scalars().all()) == 1
    cache._record_success()
    await cache.sync_pending_invalidations(db_session)
    assert (await db_session.execute(select(CachePendingInvalidation))).scalars().all() == []
