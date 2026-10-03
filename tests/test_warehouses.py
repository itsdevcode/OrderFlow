from httpx import AsyncClient
from app.models.user import User
from typing import cast

async def login_and_get_access_token(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return cast(str, response.json()["access_token"])

async def test_warehouse_crud_and_rbac(client: AsyncClient, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    # 1. Customer create denied
    response = await client.post("/api/v1/warehouses", headers=customer_headers, json={"code": "W1", "name": "Warehouse 1"})
    assert response.status_code == 403

    # 2. Admin create success
    response = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": "W1", "name": "Warehouse 1"})
    assert response.status_code == 201
    warehouse_id: int = cast(int, response.json()["id"])

    # 3. Duplicate code -> 409
    response = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": "W1", "name": "Warehouse 2"})
    assert response.status_code == 409

    # 4. List / Detail (Authenticated)
    response = await client.get("/api/v1/warehouses", headers=customer_headers)
    assert response.status_code == 200
    warehouses = cast(list[dict[str, object]], response.json())
    assert len(warehouses) >= 1

    response = await client.get(f"/api/v1/warehouses/{warehouse_id}", headers=customer_headers)
    assert response.status_code == 200
    warehouse = cast(dict[str, object], response.json())
    assert warehouse["name"] == "Warehouse 1"

    # 5. Missing -> 404
    response = await client.get("/api/v1/warehouses/99999", headers=customer_headers)
    assert response.status_code == 404

    # 6. Customer update/delete denied
    response = await client.patch(f"/api/v1/warehouses/{warehouse_id}", headers=customer_headers, json={"name": "W1-upd"})
    assert response.status_code == 403
    response = await client.delete(f"/api/v1/warehouses/{warehouse_id}", headers=customer_headers)
    assert response.status_code == 403

    # 7. Admin update
    response = await client.patch(f"/api/v1/warehouses/{warehouse_id}", headers=admin_headers, json={"name": "W1-upd"})
    assert response.status_code == 200
    warehouse_upd = cast(dict[str, object], response.json())
    assert warehouse_upd["name"] == "W1-upd"

    # Explicit null validation tests
    response = await client.patch(f"/api/v1/warehouses/{warehouse_id}", headers=admin_headers, json={"name": None})
    assert response.status_code == 422

    response = await client.patch(f"/api/v1/warehouses/{warehouse_id}", headers=admin_headers, json={"code": None})
    assert response.status_code == 422

    # 8. Admin delete success
    response = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": "W2", "name": "Warehouse 2"})
    w2_id: int = cast(int, response.json()["id"])
    response = await client.delete(f"/api/v1/warehouses/{w2_id}", headers=admin_headers)
    assert response.status_code == 204
