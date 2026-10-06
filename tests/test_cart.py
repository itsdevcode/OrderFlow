from typing import cast
from httpx import AsyncClient

from app.models.user import User
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.inventory import Inventory


async def login_and_get_access_token(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return cast(str, response.json()["access_token"])


async def test_cart_operations(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Set up another customer to test isolation
    response = await client.post("/api/v1/users", headers={"Authorization": f"Bearer {admin_token}"}, json={
        "email": "customer2@example.com",
        "name": "Cust Two",
        "password": "Password123!",
        "role_id": customer_user.role_id
    })
    customer2_token = await login_and_get_access_token(client, "customer2@example.com", "Password123!")

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}
    customer2_headers = {"Authorization": f"Bearer {customer2_token}"}

    # Create category, product, and inventory
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "Cat1", "slug": "cat1"})
    cat_id = cast(int, response.json()["id"])

    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Prod1", "slug": "prod1", "sku": "SKU1", "price": 100.0, "category_id": cat_id, "is_active": True
    })
    prod_id = cast(int, response.json()["id"])

    # Create inactive product
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "Prod2", "slug": "prod2", "sku": "SKU2", "price": 50.0, "category_id": cat_id, "is_active": False
    })
    prod2_id = cast(int, response.json()["id"])

    # Setup inventory for Prod1
    response = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": "WH1", "name": "Loc1"})
    wh_id = cast(int, response.json()["id"])

    response = await client.post(
        f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust",
        headers=admin_headers,
        json={"available_quantity_change": 10}
    )
    assert response.status_code == 200

    # 1. Customer can get/create their cart
    response = await client.get("/api/v1/cart", headers=customer_headers)
    assert response.status_code == 200
    cart_data = response.json()
    assert cart_data["items"] == []

    # 5. Nonexistent product rejected
    response = await client.post("/api/v1/cart/items", headers=customer_headers, json={
        "product_id": 9999,
        "quantity": 1
    })
    assert response.status_code == 404

    # 6. Inactive product cannot be added
    response = await client.post("/api/v1/cart/items", headers=customer_headers, json={
        "product_id": prod2_id,
        "quantity": 1
    })
    assert response.status_code == 400

    # 4. Quantity <= 0 rejected
    response = await client.post("/api/v1/cart/items", headers=customer_headers, json={
        "product_id": prod_id,
        "quantity": 0
    })
    assert response.status_code == 422

    response = await client.post("/api/v1/cart/items", headers=customer_headers, json={
        "product_id": prod_id,
        "quantity": -5
    })
    assert response.status_code == 422

    # 2. Customer can add an active product
    response = await client.post("/api/v1/cart/items", headers=customer_headers, json={
        "product_id": prod_id,
        "quantity": 2
    })
    assert response.status_code == 200
    cart_data = response.json()
    assert len(cart_data["items"]) == 1
    assert cart_data["items"][0]["product_id"] == prod_id
    assert cart_data["items"][0]["quantity"] == 2
    item_id = cart_data["items"][0]["id"]

    # 3. Adding/updating quantity works correctly (adding existing increments it)
    # 7. Duplicate product does not create duplicate rows
    response = await client.post("/api/v1/cart/items", headers=customer_headers, json={
        "product_id": prod_id,
        "quantity": 3
    })
    assert response.status_code == 200
    cart_data = response.json()
    assert len(cart_data["items"]) == 1
    assert cart_data["items"][0]["quantity"] == 5

    # 8. Customer can update quantity
    response = await client.patch(f"/api/v1/cart/items/{item_id}", headers=customer_headers, json={
        "quantity": 10
    })
    assert response.status_code == 200
    cart_data = response.json()
    assert cart_data["items"][0]["quantity"] == 10

    # Invalid update quantity
    response = await client.patch(f"/api/v1/cart/items/{item_id}", headers=customer_headers, json={
        "quantity": 0
    })
    assert response.status_code == 422

    # 10. Isolation check
    response = await client.get("/api/v1/cart", headers=customer2_headers)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 0

    response = await client.patch(f"/api/v1/cart/items/{item_id}", headers=customer2_headers, json={"quantity": 5})
    assert response.status_code == 404

    response = await client.delete(f"/api/v1/cart/items/{item_id}", headers=customer2_headers)
    assert response.status_code == 404

    # 9. Customer can remove an item
    response = await client.delete(f"/api/v1/cart/items/{item_id}", headers=customer_headers)
    assert response.status_code == 200
    cart_data = response.json()
    assert len(cart_data["items"]) == 0

    # Add back items to test clear cart
    response = await client.post("/api/v1/cart/items", headers=customer_headers, json={"product_id": prod_id, "quantity": 1})
    assert response.status_code == 200

    response = await client.delete("/api/v1/cart", headers=customer_headers)
    assert response.status_code == 204
    
    response = await client.get("/api/v1/cart", headers=customer_headers)
    assert len(response.json()["items"]) == 0

    # 11. Verify inventory untouched
    stmt = select(Inventory).where(Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id)
    result = await db_session.execute(stmt)
    inv = result.scalar_one()
    assert inv.available_quantity == 10
    assert inv.reserved_quantity == 0
