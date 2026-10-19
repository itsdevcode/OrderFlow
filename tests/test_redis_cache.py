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


