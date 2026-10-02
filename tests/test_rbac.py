from httpx import AsyncClient

from app.models.user import User


async def login_and_get_access_token(
    client: AsyncClient,
    email: str,
    password: str,
) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": email,
            "password": password,
        },
    )

    assert response.status_code == 200

    return response.json()["access_token"]


async def test_create_user_without_token(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/users",
        json={
            "name": "New User",
            "email": "newuser@test.com",
            "password": "Password123!",
            "role_id": 1,
        },
    )

    assert response.status_code == 401


async def test_customer_cannot_create_user(
    client: AsyncClient,
    customer_user: User,
) -> None:
    access_token = await login_and_get_access_token(
        client,
        customer_user.email,
        "CustomerPassword123!",
    )

    response = await client.post(
        "/api/v1/users",
        headers={
            "Authorization": f"Bearer {access_token}",
        },
        json={
            "name": "New User",
            "email": "newuser@test.com",
            "password": "Password123!",
            "role_id": customer_user.role_id,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Insufficient permissions"


async def test_admin_can_create_user(
    client: AsyncClient,
    admin_user: User,
) -> None:
    access_token = await login_and_get_access_token(
        client,
        admin_user.email,
        "AdminPassword123!",
    )

    response = await client.post(
        "/api/v1/users",
        headers={
            "Authorization": f"Bearer {access_token}",
        },
        json={
            "name": "Created Customer",
            "email": "created@test.com",
            "password": "Password123!",
            "role_id": admin_user.role_id,
        },
    )

    assert response.status_code == 201