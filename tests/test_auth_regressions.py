"""Security regressions with real transactions and deterministic race scheduling."""
import asyncio
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.exceptions.auth import InvalidTokenError
from app.models.audit_log import AuditAction, AuditLog
from app.models.refresh_token import RefreshToken
from app.repositories.user import UserRepository
from app.services.auth import AuthService
from tests.conftest import TestSessionLocal


async def test_concurrent_refresh_has_one_descendant(customer_user, monkeypatch):
    async with TestSessionLocal() as db:
        tokens = await AuthService(db).login(customer_user.email, "CustomerPassword123!")
    barrier = asyncio.Barrier(2)
    original = UserRepository.get_by_id

    async def synchronized_lookup(self, user_id, **kwargs):
        # Before the fix, both requests have already read the active token here.
        # After the fix, this is the user lock acquired BEFORE reading tokens.
        await asyncio.wait_for(barrier.wait(), 10)
        return await original(self, user_id, **kwargs)

    monkeypatch.setattr(UserRepository, "get_by_id", synchronized_lookup)

    async def rotate():
        async with TestSessionLocal() as db:
            try:
                return await AuthService(db).refresh(tokens.refresh_token)
            except InvalidTokenError:
                return None

    results = await asyncio.wait_for(asyncio.gather(rotate(), rotate()), 15)
    assert sum(result is not None for result in results) == 1
    async with TestSessionLocal() as db:
        persisted = (await db.scalars(select(RefreshToken))).all()
        assert len(persisted) == 2
        assert all(token.is_revoked for token in persisted)
        actions = list(await db.scalars(select(AuditLog.action)))
        assert actions.count(AuditAction.TOKEN_REFRESHED.value) == 1
        assert actions.count(AuditAction.TOKEN_REUSE_DETECTED.value) == 1


@pytest.mark.parametrize("token_type,path", [("access", "me"), ("refresh", "refresh")])
async def test_expired_tokens_are_rejected(client, customer_user, token_type, path):
    login = await client.post("/api/v1/auth/login", json={
        "email": customer_user.email, "password": "CustomerPassword123!"})
    assert login.status_code == 200
    payload = jwt.decode(login.json()[f"{token_type}_token"], settings.jwt_secret_key,
                         algorithms=[settings.jwt_algorithm])
    payload["exp"] = datetime.now(UTC) - timedelta(seconds=60)
    expired = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    if path == "me":
        response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"})
    else:
        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": expired})
    assert response.status_code == 401
    async with TestSessionLocal() as db:
        tokens = list(await db.scalars(select(RefreshToken)))
        assert len(tokens) == 1 and tokens[0].is_revoked is False
        assert list(await db.scalars(select(AuditLog).where(
            AuditLog.action == AuditAction.TOKEN_REFRESHED.value))) == []


async def test_disabled_user_cannot_use_issued_tokens(client, customer_user, db_session):
    login = await client.post("/api/v1/auth/login", json={
        "email": customer_user.email, "password": "CustomerPassword123!"})
    assert login.status_code == 200
    tokens = login.json()
    customer_user.is_active = False
    await db_session.commit()
    assert (await client.get("/api/v1/auth/me", headers={
        "Authorization": f"Bearer {tokens['access_token']}"})).status_code == 401
    assert (await client.post("/api/v1/auth/refresh", json={
        "refresh_token": tokens["refresh_token"]})).status_code == 401
    assert (await client.post("/api/v1/auth/login", json={
        "email": customer_user.email, "password": "CustomerPassword123!"})).status_code == 403
    async with TestSessionLocal() as db:
        assert len(list(await db.scalars(select(RefreshToken)))) == 1
        assert list(await db.scalars(select(AuditLog).where(
            AuditLog.action == AuditAction.TOKEN_REFRESHED.value))) == []


@pytest.mark.parametrize("subject", ["not-an-integer", [], None])
async def test_signed_invalid_subject_cannot_authorize(client, subject):
    token = jwt.encode({"sub": subject, "type": "access"}, settings.jwt_secret_key,
                       algorithm=settings.jwt_algorithm)
    response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_replay_racing_another_session_revokes_every_descendant(customer_user, monkeypatch):
    async with TestSessionLocal() as db:
        auth = AuthService(db)
        first = await auth.login(customer_user.email, "CustomerPassword123!")
        second = await auth.login(customer_user.email, "CustomerPassword123!")
        await auth.refresh(first.refresh_token)
    barrier = asyncio.Barrier(2)
    original = UserRepository.get_by_id

    async def synchronized_lookup(self, user_id, **kwargs):
        await asyncio.wait_for(barrier.wait(), 10)
        return await original(self, user_id, **kwargs)

    monkeypatch.setattr(UserRepository, "get_by_id", synchronized_lookup)

    async def rotate(token):
        async with TestSessionLocal() as db:
            try:
                return await AuthService(db).refresh(token)
            except InvalidTokenError:
                return None

    results = await asyncio.wait_for(asyncio.gather(
        rotate(first.refresh_token), rotate(second.refresh_token)), 15)
    assert results[0] is None
    # Either the second session rotated just before revocation, or revocation
    # won and its refresh was rejected. Neither ordering may leave active tokens.
    async with TestSessionLocal() as db:
        tokens = list(await db.scalars(select(RefreshToken)))
        assert len(tokens) == 3 + (results[1] is not None)
        assert all(token.is_revoked for token in tokens)
