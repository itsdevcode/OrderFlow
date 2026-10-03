from httpx import AsyncClient
from app.models.user import User

from typing import cast

async def login_and_get_access_token(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return cast(str, response.json()["access_token"])


async def test_product_crud_and_rbac(client: AsyncClient, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    # Setup category
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "PCat", "slug": "pcat"})
    assert response.status_code == 201
    cat_id: int = cast(int, response.json()["id"])

    # 1. Customer mutations denied
    response = await client.post("/api/v1/products", headers=customer_headers, json={
        "name": "P1", "slug": "p1", "sku": "S1", "price": 10.0, "category_id": cat_id
    })
    assert response.status_code == 403

    response = await client.patch("/api/v1/products/99999", headers=customer_headers, json={
        "price": 20.0
    })
    assert response.status_code == 403

    # 2. Invalid category -> 404
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "P1", "slug": "p1", "sku": "S1", "price": 10.0, "category_id": 99999
    })
    assert response.status_code == 404

    # 3. Invalid price -> 422
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "P1", "slug": "p1", "sku": "S1", "price": -5.0, "category_id": cat_id
    })
    assert response.status_code == 422

    # 4. Create success
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "P1", "slug": "p1", "sku": "S1", "price": 10.0, "category_id": cat_id
    })
    assert response.status_code == 201
    prod_id: int = cast(int, response.json()["id"])

    # 5. Duplicate SKU/slug -> 409
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "P2", "slug": "p1", "sku": "S2", "price": 10.0, "category_id": cat_id
    })
    assert response.status_code == 409
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "P2", "slug": "p2", "sku": "S1", "price": 10.0, "category_id": cat_id
    })
    assert response.status_code == 409

    # 6. List filters (category_id, is_active)
    response = await client.get(f"/api/v1/products?category_id={cat_id}", headers=customer_headers)
    assert response.status_code == 200
    products = cast(list[dict[str, object]], response.json())
    assert len(products) >= 1
    assert all(product["category_id"] == cat_id for product in products)

    # Create and set a product to inactive for filtering test
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "P3", "slug": "p3", "sku": "S3", "price": 10.0, "category_id": cat_id
    })
    p3_id: int = cast(int, response.json()["id"])
    _ = await client.patch(f"/api/v1/products/{p3_id}", headers=admin_headers, json={"is_active": False})

    response = await client.get("/api/v1/products?is_active=false", headers=customer_headers)
    assert response.status_code == 200
    inactive_products = cast(list[dict[str, object]], response.json())
    assert len(inactive_products) >= 1
    assert all(product["is_active"] is False for product in inactive_products)

    # 7. Detail / missing -> 404
    response = await client.get(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert response.status_code == 200

    response = await client.get("/api/v1/products/99999", headers=customer_headers)
    assert response.status_code == 404

    # 8. Update including category change + duplicate protection
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "PCat2", "slug": "pcat2"})
    cat2_id: int = cast(int, response.json()["id"])

    # Invalid category on update -> 404
    response = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={
        "category_id": 99999
    })
    assert response.status_code == 404

    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "P2", "slug": "p2", "sku": "S2", "price": 20.0, "category_id": cat_id
    })
    assert response.status_code == 201

    response = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={
        "category_id": cat2_id,
        "slug": "p1-upd",
        "price": 15.0
    })
    assert response.status_code == 200
    body = cast(dict[str, object], response.json())
    assert body["category_id"] == cat2_id
    assert body["slug"] == "p1-upd"
    assert body["price"] == "15.00"

    # Explicit null validation tests
    response = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={"price": None})
    assert response.status_code == 422

    response = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={"sku": None})
    assert response.status_code == 422

    response = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={"category_id": None})
    assert response.status_code == 422
    # Duplicate protection on update
    response = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={"slug": "p2"})
    assert response.status_code == 409

    response = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={"sku": "S2"})
    assert response.status_code == 409

    # 9. Delete
    response = await client.delete(f"/api/v1/products/{prod_id}", headers=customer_headers)
    assert response.status_code == 403

    response = await client.delete(f"/api/v1/products/{prod_id}", headers=admin_headers)
    assert response.status_code == 204
    
    response = await client.get(f"/api/v1/products/{prod_id}", headers=admin_headers)
    assert response.status_code == 404
