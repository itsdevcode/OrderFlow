from typing import cast
from httpx import AsyncClient

from app.models.user import User
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.inventory import Inventory
from app.models.cart import Cart
from app.models.cart_item import CartItem
from app.models.order import Order
from app.models.order_item import OrderItem
import pytest


async def login_and_get_access_token(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return cast(str, response.json()["access_token"])


async def test_order_lifecycle(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Set up another customer to test isolation
    response = await client.post("/api/v1/users", headers={"Authorization": f"Bearer {admin_token}"}, json={
        "email": "order_cust2@example.com",
        "name": "Cust Two",
        "password": "Password123!",
        "role_id": customer_user.role_id
    })
    customer2_token = await login_and_get_access_token(client, "order_cust2@example.com", "Password123!")

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}
    customer2_headers = {"Authorization": f"Bearer {customer2_token}"}

    # Setup Product
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "CatOrder", "slug": "catorder"})
    cat_id = cast(int, response.json()["id"])

    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "OrderProd1", "slug": "orderprod1", "sku": "OSKU1", "price": 100.0, "category_id": cat_id, "is_active": True
    })
    prod_id = cast(int, response.json()["id"])

    # Setup inventory for Prod1
    response = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": "OWH1", "name": "Loc1"})
    wh_id = cast(int, response.json()["id"])
    response = await client.post(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 50, "reason": "Restock"
    })
    assert response.status_code == 200

    # Unauthenticated requests are rejected
    response = await client.post("/api/v1/orders")
    assert response.status_code == 401

    # Empty cart cannot create order
    response = await client.post("/api/v1/orders", headers=customer_headers)
    assert response.status_code == 400
    assert "empty cart" in response.json()["detail"].lower()

    # Add product to cart
    response = await client.post("/api/v1/cart/items", headers=customer_headers, json={"product_id": prod_id, "quantity": 2})
    assert response.status_code == 200

    # Create a product, add to cart, then make it inactive
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "OrderProd2", "slug": "orderprod2", "sku": "OSKU2", "price": 50.0, "category_id": cat_id, "is_active": True
    })
    prod2_id = cast(int, response.json()["id"])
    
    response = await client.post("/api/v1/cart/items", headers=customer_headers, json={"product_id": prod2_id, "quantity": 1})
    assert response.status_code == 200
    
    # Make product inactive
    response = await client.patch(f"/api/v1/products/{prod2_id}", headers=admin_headers, json={
        "is_active": False
    })
    assert response.status_code == 200
    
    # Try order with inactive product
    response = await client.post("/api/v1/orders", headers=customer_headers)
    assert response.status_code == 400
    assert "not active" in response.json()["detail"].lower()
    
    # Remove inactive product
    response = await client.delete(f"/api/v1/cart/items/{prod2_id}", headers=customer_headers)
    assert response.status_code == 200

    # 1. Check inventory before order
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.status_code == 200
    assert inv_response.json()["available_quantity"] == 50
    assert inv_response.json()["reserved_quantity"] == 0

    # 2. CREATE ORDER
    response = await client.post("/api/v1/orders", headers=customer_headers)
    assert response.status_code == 201
    order_data = response.json()
    assert order_data["status"] == "PENDING"
    assert "ORD-" in order_data["order_number"]
    assert len(order_data["items"]) == 1
    
    # Check total uses snapshot price * quantity
    assert order_data["total_amount"] == "200.00"
    
    item = order_data["items"][0]
    assert item["product_id"] == prod_id
    assert item["product_name"] == "OrderProd1"
    assert item["product_sku"] == "OSKU1"
    assert item["unit_price"] == "100.00"
    assert item["quantity"] == 2
    
    order_id = order_data["id"]
    order_number = order_data["order_number"]

    # 3. Successful order creation clears cart items but cart itself remains
    response = await client.get("/api/v1/cart", headers=customer_headers)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 0
    assert response.json()["id"] is not None

    # 4. Inventory is UNCHANGED (since we defer to next task)
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.status_code == 200
    assert inv_response.json()["available_quantity"] == 50
    assert inv_response.json()["reserved_quantity"] == 0

    # 5. Critical snapshot regression test
    # Change Product.price
    response = await client.patch(f"/api/v1/products/{prod_id}", headers=admin_headers, json={
        "price": 150.0
    })
    assert response.status_code == 200

    # GET historical order
    response = await client.get(f"/api/v1/orders/{order_id}", headers=customer_headers)
    assert response.status_code == 200
    order_data = response.json()
    item = order_data["items"][0]
    # Unit price must still be 100
    assert item["unit_price"] == "100.00"
    assert item["product_name"] == "OrderProd1"

    # 6. Isolation tests
    # User B cannot access User A's order
    response = await client.get(f"/api/v1/orders/{order_id}", headers=customer2_headers)
    assert response.status_code == 404

    # Invalid/nonexistent order returns 404
    response = await client.get("/api/v1/orders/99999", headers=customer_headers)
    assert response.status_code == 404

    # GET /orders returns only CurrentUser's orders
    response = await client.get("/api/v1/orders", headers=customer_headers)
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["id"] == order_id

    response = await client.get("/api/v1/orders", headers=customer2_headers)
    assert response.status_code == 200
    assert len(response.json()) == 0

    # Pagination works
    response = await client.get("/api/v1/orders?offset=1&limit=10", headers=customer_headers)
    assert response.status_code == 200
    assert len(response.json()) == 0


async def test_order_rollback_on_failure(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, monkeypatch: pytest.MonkeyPatch) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    # Setup Product
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "CatRollback", "slug": "catrollback"})
    cat_id = cast(int, response.json()["id"])

    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "RollbackProd1", "slug": "rollbackprod1", "sku": "RB_SKU1", "price": 100.0, "category_id": cat_id, "is_active": True
    })
    prod_id = cast(int, response.json()["id"])

    # Add product to cart
    response = await client.post("/api/v1/cart/items", headers=customer_headers, json={"product_id": prod_id, "quantity": 2})
    assert response.status_code == 200

    # Force a failure during order creation
    from app.repositories.order import OrderRepository
    original_create_order = OrderRepository.create_order
    
    async def mock_create_order(*args, **kwargs):
        raise ValueError("Simulated DB failure")
    
    monkeypatch.setattr(OrderRepository, "create_order", mock_create_order)

    # Attempt to create order
    with pytest.raises(ValueError):
        await client.post("/api/v1/orders", headers=customer_headers)

    # Verify no partial order remains
    result = await db_session.execute(select(Order).where(Order.user_id == customer_user.id))
    orders = result.scalars().all()
    assert len(orders) == 0

    # Verify no partial order items remain
    result = await db_session.execute(select(OrderItem))
    order_items = result.scalars().all()
    # Check specifically for our sku
    assert not any(i.product_sku == "RB_SKU1" for i in order_items)

    # Verify cart items are still present
    response = await client.get("/api/v1/cart", headers=customer_headers)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["product_id"] == prod_id
