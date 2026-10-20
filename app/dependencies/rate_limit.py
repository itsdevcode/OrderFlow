"""Endpoint policies and identity resolution; no database access or writes."""
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

from fastapi import HTTPException, Request

from app.core.config import settings
from app.services.rate_limit import RateLimitUnavailable, RateScope, rate_limiter


def _address(value: str) -> IPv4Address | IPv6Address:
    address = ip_address(value)
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def client_identity(request: Request) -> str:
    """Walk XFF right-to-left only when the socket peer is explicitly trusted.

    Missing/invalid/oversized forwarded metadata falls back to the socket peer.
    The ASGI server must preserve that peer (see deployment instructions).
    """
    try:
        peer = _address(request.client.host if request.client else "")
    except ValueError:
        return "ip:unknown"  # One conservative bucket, never a random bypass.
    networks = [ip_network(value) for value in settings.rate_limit_trusted_proxies]

    def trusted(address: IPv4Address | IPv6Address) -> bool:
        return any(address in network for network in networks)

    if not trusted(peer):
        return f"ip:{peer}"
    headers = request.headers.getlist("x-forwarded-for")
    if len(headers) != 1 or len(headers[0]) > 4096:
        return f"ip:{peer}"
    hops = headers[0].split(",")
    if len(hops) > 32:
        return f"ip:{peer}"
    try:
        chain = [_address(hop.strip()) for hop in hops]
    except ValueError:
        return f"ip:{peer}"
    current = peer
    for address in reversed(chain):
        if not trusted(current):
            break
        current = address
    return f"ip:{current}"


async def _enforce(request: Request, scope: RateScope) -> None:
    if not settings.rate_limit_enabled:
        return
    if scope == "login":
        limit, window = settings.rate_limit_login_limit, settings.rate_limit_login_window_seconds
    elif scope == "refresh":
        limit, window = settings.rate_limit_refresh_limit, settings.rate_limit_refresh_window_seconds
    else:
        limit, window = settings.rate_limit_webhook_limit, settings.rate_limit_webhook_window_seconds
    try:
        decision = await rate_limiter.check(scope, client_identity(request), limit, window)
    except RateLimitUnavailable:
        if scope == "payment_webhook":
            # Existing HMAC authentication, body bounds, replay checks, and DB
            # idempotency remain mandatory. Providers can continue delivering.
            return
        raise HTTPException(
            status_code=503, detail="Authentication temporarily unavailable",
            headers={"Retry-After": "5"},
        ) from None
    if not decision.allowed:
        raise HTTPException(
            status_code=429, detail="Rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after)},
        )


async def limit_login(request: Request) -> None:
    await _enforce(request, "login")


async def limit_refresh(request: Request) -> None:
    await _enforce(request, "refresh")


async def limit_payment_webhook(request: Request) -> None:
    await _enforce(request, "payment_webhook")
