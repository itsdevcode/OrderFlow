from httpx import AsyncClient
from app.models.user import User
from typing import cast

async def login_and_get_access_token(
    client: AsyncClient,
    email: str,
    password: str,
) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return cast(str, response.json()["access_token"])


async def test_category_crud_and_rbac(client: AsyncClient, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    # 1. Customer create denied
    response = await client.post("/api/v1/categories", headers=customer_headers, json={"name": "Cat1", "slug": "cat1"})
    assert response.status_code == 403

    # 2. Admin create success
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Cat1", "slug": "cat1"})
    assert response.status_code == 201
    category_id: int = cast(int, response.json()["id"])

    # 3. Duplicate name/slug -> 409
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Cat1", "slug": "cat2"})
    assert response.status_code == 409
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Cat2", "slug": "cat1"})
    assert response.status_code == 409

    # 4. List / Detail (Authenticated)
    response = await client.get("/api/v1/categories", headers=customer_headers)
    assert response.status_code == 200
    categories = cast(list[dict[str, object]], response.json())
    assert len(categories) >= 1

    response = await client.get(f"/api/v1/categories/{category_id}", headers=customer_headers)
    assert response.status_code == 200
    category = cast(dict[str, object], response.json())
    assert category["name"] == "Cat1"

    # 5. Missing -> 404
    response = await client.get("/api/v1/categories/99999", headers=customer_headers)
    assert response.status_code == 404

    # 6. Customer update/delete denied
    response = await client.patch(f"/api/v1/categories/{category_id}", headers=customer_headers, json={"name": "Cat1-upd"})
    assert response.status_code == 403
    response = await client.delete(f"/api/v1/categories/{category_id}", headers=customer_headers)
    assert response.status_code == 403

    # 7. Admin update
    response = await client.patch(f"/api/v1/categories/{category_id}", headers=admin_headers, json={"name": "Cat1-upd"})
    assert response.status_code == 200
    category_upd = cast(dict[str, object], response.json())
    assert category_upd["name"] == "Cat1-upd"

    # 8. Category-with-products delete -> 409
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Prod1", "slug": "prod1", "sku": "SKU1", "price": 10.0, "category_id": category_id
    })
    assert response.status_code == 201

    response = await client.delete(f"/api/v1/categories/{category_id}", headers=admin_headers)
    assert response.status_code == 409

    # 9. Admin delete success (on a new category)
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Cat2", "slug": "cat2"})
    cat2_id: int = cast(int, response.json()["id"])
    response = await client.delete(f"/api/v1/categories/{cat2_id}", headers=admin_headers)
    assert response.status_code == 204
