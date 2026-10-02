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