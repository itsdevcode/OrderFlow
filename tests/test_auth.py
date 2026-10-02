from httpx import AsyncClient

from app.models.user import User


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_login_success(
    client: AsyncClient,
    customer_user: User,
) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": customer_user.email,
            "password": "CustomerPassword123!",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "bearer"


async def test_login_wrong_password(
    client: AsyncClient,
    customer_user: User,
) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": customer_user.email,
            "password": "WrongPassword",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


async def test_me_with_access_token(
    client: AsyncClient,
    customer_user: User,
) -> None:
    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": customer_user.email,
            "password": "CustomerPassword123!",
        },
    )

    assert login_response.status_code == 200

    access_token = login_response.json()["access_token"]

    response = await client.get(
        "/api/v1/auth/me",
        headers={
            "Authorization": f"Bearer {access_token}",
        },
    )

    assert response.status_code == 200
    assert response.json()["email"] == customer_user.email


async def test_me_rejects_refresh_token(
    client: AsyncClient,
    customer_user: User,
) -> None:
    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": customer_user.email,
            "password": "CustomerPassword123!",
        },
    )

    assert login_response.status_code == 200

    refresh_token = login_response.json()["refresh_token"]

    response = await client.get(
        "/api/v1/auth/me",
        headers={
            "Authorization": f"Bearer {refresh_token}",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid token type"

async def test_refresh_token_success(
    client: AsyncClient,
    customer_user: User,
) -> None:
    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": customer_user.email,
            "password": "CustomerPassword123!",
        },
    )

    refresh_token = login_response.json()["refresh_token"]

    response = await client.post(
        "/api/v1/auth/refresh",
        json={
            "refresh_token": refresh_token,
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["access_token"]
    assert data["token_type"] == "bearer"


async def test_refresh_rejects_access_token(
    client: AsyncClient,
    customer_user: User,
) -> None:
    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": customer_user.email,
            "password": "CustomerPassword123!",
        },
    )

    access_token = login_response.json()["access_token"]

    response = await client.post(
        "/api/v1/auth/refresh",
        json={
            "refresh_token": access_token,
        },
    )

    assert response.status_code == 401


async def test_refresh_rejects_invalid_token(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/auth/refresh",
        json={
            "refresh_token": "not-a-valid-jwt",
        },
    )
    assert response.status_code == 401


async def test_refresh_token_revoked_reuse_detection(
    client: AsyncClient,
    customer_user: User,
) -> None:
    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": customer_user.email,
            "password": "CustomerPassword123!",
        },
    )
    refresh_token = login_response.json()["refresh_token"]

    # First refresh should succeed
    refresh_response_1 = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_response_1.status_code == 200

    # The first refresh token is now revoked.
    # A second attempt to use the revoked token should trigger reuse detection.
    # It should fail and revoke ALL tokens.
    refresh_response_2 = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_response_2.status_code == 401

    # The new refresh token from the first refresh should also now be revoked!
    new_refresh_token = refresh_response_1.json()["refresh_token"]
    refresh_response_3 = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": new_refresh_token},
    )
    assert refresh_response_3.status_code == 401


async def test_logout_invalidates_refresh_token(
    client: AsyncClient,
    customer_user: User,
) -> None:
    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": customer_user.email,
            "password": "CustomerPassword123!",
        },
    )
    refresh_token = login_response.json()["refresh_token"]

    # Logout
    logout_response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )
    assert logout_response.status_code == 204

    # Refresh should fail after logout
    refresh_response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_response.status_code == 401


async def test_logout_persists_in_database(
    client: AsyncClient,
    customer_user: User,
) -> None:
    from app.core.jwt import decode_token
    from tests.conftest import TestSessionLocal
    from app.models.refresh_token import RefreshToken
    from sqlalchemy import select

    # 1. Login to get a token
    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": customer_user.email,
            "password": "CustomerPassword123!",
        },
    )
    refresh_token = login_response.json()["refresh_token"]
    
    payload = decode_token(refresh_token)
    jti = payload["jti"]

    # Verify it exists and is NOT revoked
    async with TestSessionLocal() as session:
        result = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        db_token = result.scalar_one_or_none()
        assert db_token is not None
        assert db_token.is_revoked is False

    # 2. Logout (this happens in a separate request/transaction)
    await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )

    # 3. Open a completely new raw DB session and verify it was actually committed!
    async with TestSessionLocal() as new_session:
        new_result = await new_session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        updated_token = new_result.scalar_one_or_none()
        assert updated_token is not None
        assert updated_token.is_revoked is True