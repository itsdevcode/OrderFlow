import asyncio
import math
from unittest.mock import AsyncMock, patch

from fastapi import Request
from httpx import AsyncClient
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.core.redis import get_redis_client
from app.dependencies.rate_limit import client_identity
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.rate_limit import (
    FIXED_WINDOW_SCRIPT, RateLimiter, RateLimitUnavailable, rate_limiter,
)


@pytest.fixture(autouse=True)
def reset_limiter():
    rate_limiter._unavailable_until = 0
    yield
    rate_limiter._unavailable_until = 0


def request(peer: str, forwarded: list[str] | None = None) -> Request:
    return Request({
        "type": "http", "client": (peer, 1234),
        "headers": [(b"x-forwarded-for", value.encode()) for value in (forwarded or [])],
    })


async def test_real_redis_threshold_retry_and_expiry() -> None:
    limiter = RateLimiter()
    first = await limiter.check("login", "ip:one", 2, 60)
    assert first.allowed and first.remaining == 1
    assert (await limiter.check("login", "ip:one", 2, 60)).allowed
    denied = await limiter.check("login", "ip:one", 2, 60)
    assert not denied.allowed and denied.remaining == 0
    assert 1 <= denied.retry_after <= 60
    redis = get_redis_client()
    assert redis is not None
    key = limiter.build_key("login", "ip:one", 60)
    assert 0 < await redis.pttl(key) <= 60000
    # Deterministic expiry, no wall-clock sleep.
    await redis.pexpire(key, 0)
    assert (await limiter.check("login", "ip:one", 2, 60)).allowed


async def test_fixed_window_exact_boundary_and_retry() -> None:
    redis = get_redis_client()
    assert redis is not None
    seconds, _ = await redis.time()
    boundary = (int(seconds) // 60 + 3) * 60
    key = RateLimiter.build_key("login", "ip:boundary", 60)

    async def at(milliseconds: int):
        # Run the production Lua against real Redis, substituting only its clock.
        script = FIXED_WINDOW_SCRIPT.replace(
            "redis.call('TIME')", f"{{'{milliseconds // 1000}', '{milliseconds % 1000 * 1000}'}}",
        )
        return await redis.eval(script, 1, key, 1, 60)

    assert await at(boundary * 1000 - 1) == [1, 0, 1]
    assert await at(boundary * 1000 - 1) == [0, 0, 1]
    assert await at(boundary * 1000) == [1, 0, 60]
    assert await at(boundary * 1000 + 1001) == [0, 0, math.ceil(58.999)]


async def test_atomic_concurrency_and_independent_workers() -> None:
    workers = [RateLimiter(), RateLimiter()]
    redis = get_redis_client()
    assert redis is not None
    seconds, _ = await redis.time()
    clock = (int(seconds) // 60 + 3) * 60
    script = FIXED_WINDOW_SCRIPT.replace("redis.call('TIME')", f"{{'{clock}', '0'}}")
    with patch("app.services.rate_limit.FIXED_WINDOW_SCRIPT", script):
        decisions = await asyncio.gather(*[
            workers[index % 2].check("login", "ip:shared", 7, 60)
            for index in range(100)
        ])
    assert sum(result.allowed for result in decisions) == 7
    redis = get_redis_client()
    assert redis is not None
    assert await redis.hget(workers[0].build_key("login", "ip:shared", 60), "count") == "7"


async def test_identity_and_endpoint_buckets_are_independent() -> None:
    limiter = RateLimiter()
    assert (await limiter.check("login", "ip:a", 1, 60)).allowed
    assert not (await limiter.check("login", "ip:a", 1, 60)).allowed
    assert (await limiter.check("login", "ip:b", 1, 60)).allowed
    assert (await limiter.check("refresh", "ip:a", 1, 60)).allowed
    assert (await limiter.check("payment_webhook", "ip:a", 1, 60)).allowed
    assert (await limiter.check("login", "user:123", 1, 60)).allowed


@pytest.mark.parametrize("path,payload,setting", [
    ("/api/v1/auth/login", {"email": "unknown@example.com", "password": "wrong"}, "rate_limit_login_limit"),
    ("/api/v1/auth/refresh", {"refresh_token": "invalid"}, "rate_limit_refresh_limit"),
    ("/api/v1/webhooks/payments", {}, "rate_limit_webhook_limit"),
])
async def test_endpoint_threshold_and_consistent_429(client: AsyncClient, path: str, payload: dict, setting: str) -> None:
    with patch.object(settings, setting, 2):
        for _ in range(2):
            response = await client.post(path, json=payload)
            assert response.status_code == 401
        response = await client.post(path, json=payload)
        assert response.status_code == 429
        assert response.json() == {"detail": "Rate limit exceeded"}
        assert 1 <= int(response.headers["Retry-After"]) <= 60


async def test_valid_auth_threshold_and_no_token_mutation_when_limited(
    client: AsyncClient, customer_user: User, db_session: AsyncSession,
) -> None:
    credentials = {"email": customer_user.email, "password": "CustomerPassword123!"}
    with patch.object(settings, "rate_limit_login_limit", 1), patch.object(settings, "rate_limit_refresh_limit", 1):
        login = await client.post("/api/v1/auth/login", json=credentials)
        assert login.status_code == 200
        token = login.json()["refresh_token"]
        assert (await client.post("/api/v1/auth/login", json=credentials)).status_code == 429
        refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert refreshed.status_code == 200
        token = refreshed.json()["refresh_token"]
        count = (await db_session.execute(select(func.count()).select_from(RefreshToken))).scalar_one()
        assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": token})).status_code == 429
        assert (await db_session.execute(select(func.count()).select_from(RefreshToken))).scalar_one() == count
        with patch.object(settings, "rate_limit_enabled", False):
            assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": token})).status_code == 200
        # GET/authentication cannot be bypassed by an unlimited catalog identity.
        assert (await client.get("/api/v1/auth/me")).status_code in (401, 403)
        assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"})).status_code == 200


@pytest.mark.parametrize("peer,forwarded,expected", [
    ("198.51.100.2", ["1.1.1.1"], "198.51.100.2"),
    ("10.0.0.1", ["198.51.100.2"], "198.51.100.2"),
    ("10.0.0.1", ["1.1.1.1, 198.51.100.2, 10.0.0.2"], "198.51.100.2"),
    ("10.0.0.1", ["invalid"], "10.0.0.1"),
    ("10.0.0.1", ["1.1.1.1", "2.2.2.2"], "10.0.0.1"),
    ("10.0.0.1", ["x" * 4097], "10.0.0.1"),
    ("10.0.0.1", [", ".join(["1.1.1.1"] * 33)], "10.0.0.1"),
    ("::ffff:198.51.100.2", [], "198.51.100.2"),
    ("2001:db8::1", ["1.1.1.1"], "2001:db8::1"),
])
def test_trusted_proxy_chain(peer: str, forwarded: list[str], expected: str) -> None:
    with patch.object(settings, "rate_limit_trusted_proxies", ["10.0.0.0/24"]):
        assert client_identity(request(peer, forwarded)) == f"ip:{expected}"


async def test_spoofing_headers_or_tokens_cannot_reset_bucket(client: AsyncClient) -> None:
    with patch.object(settings, "rate_limit_login_limit", 1):
        payload = {"email": "unknown@example.com", "password": "wrong"}
        assert (await client.post("/api/v1/auth/login", json=payload, headers={"X-Forwarded-For": "1.1.1.1"})).status_code == 401
        assert (await client.post("/api/v1/auth/login", json=payload, headers={"X-Forwarded-For": "2.2.2.2", "Authorization": "Bearer arbitrary"})).status_code == 429
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/api/v1/products")).status_code in (401, 403)


async def test_trusted_proxy_independent_http_clients(client: AsyncClient) -> None:
    with patch.object(settings, "rate_limit_trusted_proxies", ["127.0.0.1/32"]), patch.object(settings, "rate_limit_login_limit", 1):
        payload = {"email": "unknown@example.com", "password": "wrong"}
        for ip in ("198.51.100.1", "198.51.100.2"):
            assert (await client.post("/api/v1/auth/login", json=payload, headers={"X-Forwarded-For": ip})).status_code == 401
            assert (await client.post("/api/v1/auth/login", json=payload, headers={"X-Forwarded-For": ip})).status_code == 429


async def test_redis_outage_auth_closed_webhook_still_authenticates_and_recovers(client: AsyncClient) -> None:
    redis = get_redis_client()
    assert redis is not None
    with patch.object(redis, "eval", side_effect=RedisConnectionError("offline")):
        response = await client.post("/api/v1/auth/login", json={"email": "unknown@example.com", "password": "wrong"})
        assert response.status_code == 503 and response.headers["Retry-After"] == "5"
        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": "invalid"})
        assert response.status_code == 503
        assert (await client.post("/api/v1/webhooks/payments", json={})).status_code == 401
        assert (await client.get("/health")).status_code == 200
    rate_limiter._unavailable_until = 0  # Advance cooldown deterministically.
    assert (await client.post("/api/v1/auth/login", json={"email": "unknown@example.com", "password": "wrong"})).status_code == 401


async def test_missing_startup_pool_and_later_recovery() -> None:
    limiter = RateLimiter(cooldown_seconds=0)
    with patch("app.services.rate_limit.ensure_redis_connected", new_callable=AsyncMock, return_value=None):
        with pytest.raises(RateLimitUnavailable):
            await limiter.check("login", "ip:startup", 1, 60)
    assert (await limiter.check("login", "ip:startup", 1, 60)).allowed


async def test_cache_disabled_does_not_disable_security_pool() -> None:
    from app.core.redis import ensure_redis_connected
    with patch.object(settings, "cache_enabled", False):
        assert await ensure_redis_connected() is not None
        assert (await RateLimiter().check("login", "ip:cache-disabled", 1, 60)).allowed


def test_keys_are_bounded_and_do_not_expose_identity() -> None:
    identity = "user:" + "private-identity" * 100
    key = RateLimiter.build_key("refresh", identity, 60)
    assert len(key) < 128 and identity not in key and "private-identity" not in key


def test_configuration_rejects_invalid_limits_and_proxy_networks() -> None:
    from pydantic import ValidationError
    for overrides in ({"rate_limit_login_limit": 0}, {"rate_limit_refresh_window_seconds": -1}, {"rate_limit_trusted_proxies": ["not-a-network"]}):
        with pytest.raises(ValidationError):
            Settings(**overrides)


async def test_signed_webhook_outage_preserves_idempotency(
    client: AsyncClient, admin_user: User, customer_user: User,
) -> None:
    from tests.test_payment_webhooks import make_signed_webhook_payload
    from tests.test_payments import login_and_get_access_token, setup_test_order
    from tests.conftest import TestSessionLocal
    from app.models.webhook_event import WebhookEvent
    from app.models.payment import Payment, PaymentStatus
    admin = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    order_id, _, _ = await setup_test_order(client, admin, customer, unit_price=100, quantity=1)
    payment = await client.post(f"/api/v1/orders/{order_id}/payments", headers={"Authorization": f"Bearer {customer}"}, json={"idempotency_key": "rate-outage"})
    assert payment.status_code == 201
    raw, headers = make_signed_webhook_payload("rate-outage-event", "payment.succeeded", payment.json()["provider_payment_id"], "100.00")
    redis = get_redis_client()
    assert redis is not None
    with patch.object(redis, "eval", side_effect=RedisConnectionError("offline")):
        for _ in range(2):
            response = await client.post("/api/v1/webhooks/payments", content=raw, headers=headers)
            assert response.status_code == 200
    async with TestSessionLocal() as session:
        assert (await session.execute(select(func.count()).select_from(WebhookEvent))).scalar_one() == 1
        assert (await session.execute(select(Payment.status).where(Payment.id == payment.json()["id"]))).scalar_one() == PaymentStatus.SUCCEEDED


async def test_outage_does_not_create_refresh_tokens(client: AsyncClient, customer_user: User, db_session: AsyncSession) -> None:
    with patch("app.services.rate_limit.ensure_redis_connected", new_callable=AsyncMock, return_value=None):
        response = await client.post("/api/v1/auth/login", json={"email": customer_user.email, "password": "CustomerPassword123!"})
        assert response.status_code == 503
    assert (await db_session.execute(select(func.count()).select_from(RefreshToken))).scalar_one() == 0


async def test_limiter_disabled_explicitly(client: AsyncClient) -> None:
    with patch.object(settings, "rate_limit_enabled", False), patch.object(rate_limiter, "check", new_callable=AsyncMock) as check:
        assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": "invalid"})).status_code == 401
        check.assert_not_awaited()
