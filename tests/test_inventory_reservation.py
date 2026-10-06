import asyncio
from typing import cast
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.inventory import Inventory
from app.models.inventory_reservation import InventoryReservation, ReservationStatus
from app.models.order import Order
from app.models.cart_item import CartItem
from app.services.inventory_reservation import InventoryReservationService

async def login_and_get_access_token(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return cast(str, response.json()["access_token"])

async def setup_data(client: AsyncClient, admin_token: str) -> tuple[int, int, int]:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "CatRes", "slug": "catres"})
    cat_id = cast(int, response.json()["id"])
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "ResProd1", "slug": "resprod1", "sku": "RSKU1", "price": 100.0, "category_id": cat_id, "is_active": True
    })
    prod_id = cast(int, response.json()["id"])
    response = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": "RWH1", "name": "RLoc1"})
    wh_id = cast(int, response.json()["id"])
    return cat_id, prod_id, wh_id

@pytest.mark.asyncio
async def test_concurrent_overselling(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Setup Customer 2
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post("/api/v1/users", headers=admin_headers, json={
        "email": "cust2_concurrent@example.com", "name": "Cust Two", "password": "Password123!", "role_id": customer_user.role_id
    })
    customer2_token = await login_and_get_access_token(client, "cust2_concurrent@example.com", "Password123!")

    cat_id, prod_id, wh_id = await setup_data(client, admin_token)

    # Set inventory exactly to 1
    await client.post(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 1, "reason": "Restock"
    })

    # Both users add product to cart (qty 1)
    c1_headers = {"Authorization": f"Bearer {customer_token}"}
    c2_headers = {"Authorization": f"Bearer {customer2_token}"}

    await client.post("/api/v1/cart/items", headers=c1_headers, json={"product_id": prod_id, "quantity": 1})
    await client.post("/api/v1/cart/items", headers=c2_headers, json={"product_id": prod_id, "quantity": 1})

    # Fire both orders concurrently
    req1 = client.post("/api/v1/orders", headers=c1_headers)
    req2 = client.post("/api/v1/orders", headers=c2_headers)

    res1, res2 = await asyncio.gather(req1, req2)
    
    statuses = [res1.status_code, res2.status_code]
    assert statuses.count(201) == 1, "Exactly one request should succeed"
    assert statuses.count(409) == 1, "Exactly one request should fail with conflict"

    # Verify inventory state
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["available_quantity"] == 0
    assert inv_response.json()["reserved_quantity"] == 1

    # Verify reservations and orders
    result = await db_session.execute(select(InventoryReservation).where(InventoryReservation.product_id == prod_id))
    reservations = result.scalars().all()
    assert len(reservations) == 1
    assert reservations[0].quantity == 1

    result = await db_session.execute(select(Order).join(Order.items).where(Order.items.property.mapper.class_.product_id == prod_id))
    orders = result.unique().scalars().all()
    assert len(orders) == 1

    # Check losing customer cart
    loser_headers = c1_headers if res1.status_code == 409 else c2_headers
    winner_headers = c1_headers if res1.status_code == 201 else c2_headers

    loser_cart = await client.get("/api/v1/cart", headers=loser_headers)
    assert len(loser_cart.json()["items"]) == 1, "Losing customer cart should retain item"

    winner_cart = await client.get("/api/v1/cart", headers=winner_headers)
    assert len(winner_cart.json()["items"]) == 0, "Winning customer cart should be cleared"


@pytest.mark.asyncio
async def test_multi_item_rollback(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cat_id, prod1_id, wh_id = await setup_data(client, admin_token)

    # Product A: available 5
    await client.post(f"/api/v1/inventory/products/{prod1_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 5, "reason": "Restock"
    })

    # Product B: available 0
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "ResProd2", "slug": "resprod2", "sku": "RSKU2", "price": 50.0, "category_id": cat_id, "is_active": True
    })
    prod2_id = cast(int, response.json()["id"])
    # Do not adjust inventory for prod2

    # Add both to cart
    await client.post("/api/v1/cart/items", headers=customer_headers, json={"product_id": prod1_id, "quantity": 2})
    await client.post("/api/v1/cart/items", headers=customer_headers, json={"product_id": prod2_id, "quantity": 1})

    # Try order
    res = await client.post("/api/v1/orders", headers=customer_headers)
    assert res.status_code == 409
    assert "Insufficient stock" in res.json()["detail"]

    # Verify rollback
    inv1_response = await client.get(f"/api/v1/inventory/products/{prod1_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv1_response.json()["available_quantity"] == 5
    assert inv1_response.json()["reserved_quantity"] == 0

    cart_response = await client.get("/api/v1/cart", headers=customer_headers)
    assert len(cart_response.json()["items"]) == 2

    result = await db_session.execute(select(InventoryReservation))
    reservations = result.scalars().all()
    assert len(reservations) == 0


@pytest.mark.asyncio
async def test_reservation_service_methods(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    cat_id, prod_id, wh_id = await setup_data(client, admin_token)
    await client.post(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 10, "reason": "Restock"
    })

    # Clear cart, just in case
    await client.delete("/api/v1/cart/items", headers=customer_headers)

    await client.post("/api/v1/cart/items", headers=customer_headers, json={"product_id": prod_id, "quantity": 3})
    res = await client.post("/api/v1/orders", headers=customer_headers)
    assert res.status_code == 201

    # Check inventory
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["available_quantity"] == 7
    assert inv_response.json()["reserved_quantity"] == 3

    result = await db_session.execute(select(InventoryReservation).where(InventoryReservation.product_id == prod_id))
    reservations = result.scalars().all()
    reservation_id = reservations[0].id

    service = InventoryReservationService(db_session)
    
    # 1. Release Test
    await service.release_reservation(reservation_id)
    # Check inventory
    await db_session.refresh(reservations[0])
    assert reservations[0].status == ReservationStatus.RELEASED

    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["available_quantity"] == 10
    assert inv_response.json()["reserved_quantity"] == 0

    # Idempotent release
    await service.release_reservation(reservation_id)
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["available_quantity"] == 10

    # Test Confirm (needs a new order)
    await client.post("/api/v1/cart/items", headers=customer_headers, json={"product_id": prod_id, "quantity": 2})
    res = await client.post("/api/v1/orders", headers=customer_headers)
    
    result = await db_session.execute(select(InventoryReservation).where(InventoryReservation.status == ReservationStatus.ACTIVE))
    res2 = result.scalars().first()
    assert res2 is not None

    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["available_quantity"] == 8
    assert inv_response.json()["reserved_quantity"] == 2

    # Confirm
    await service.confirm_reservation(res2.id)
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["available_quantity"] == 8
    assert inv_response.json()["reserved_quantity"] == 0

    # Idempotent confirm
    await service.confirm_reservation(res2.id)
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["reserved_quantity"] == 0

    # Test Expire (needs new order)
    await client.post("/api/v1/cart/items", headers=customer_headers, json={"product_id": prod_id, "quantity": 1})
    res = await client.post("/api/v1/orders", headers=customer_headers)

    result = await db_session.execute(select(InventoryReservation).where(InventoryReservation.status == ReservationStatus.ACTIVE))
    res3 = result.scalars().first()
    assert res3 is not None

    # Expire
    await service.expire_reservation(res3.id)
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    # Available was 8, ordered 1 (so 7), then expired (so 8 again)
    assert inv_response.json()["available_quantity"] == 8
    assert inv_response.json()["reserved_quantity"] == 0

    # Idempotent expire
    await service.expire_reservation(res3.id)
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["available_quantity"] == 8
