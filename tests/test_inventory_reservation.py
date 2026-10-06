import asyncio
import uuid
from typing import cast
import pytest
from httpx import AsyncClient
from tests.conftest import TestSessionLocal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timezone
from app.exceptions.inventory import InventoryNotFoundError
from app.exceptions.inventory_reservation import InvalidReservationStateError

from app.models.user import User
from app.models.inventory import Inventory
from app.models.inventory_reservation import InventoryReservation, ReservationStatus
from app.models.order import Order
from app.models.cart_item import CartItem
from app.services.inventory_reservation import InventoryReservationService

async def login_and_get_access_token(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return cast(str, response.json()["access_token"])


async def setup_data_unique(client: AsyncClient, admin_token: str) -> tuple[int, int, int]:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    suffix = uuid.uuid4().hex[:8]
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": f"CatRes_{suffix}", "slug": f"catres_{suffix}"})
    cat_id = cast(int, response.json()["id"])
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": f"ResProd1_{suffix}", "slug": f"resprod1_{suffix}", "sku": f"RSKU1_{suffix}", "price": 100.0, "category_id": cat_id, "is_active": True
    })
    prod_id = cast(int, response.json()["id"])
    response = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": f"RWH1_{suffix}", "name": f"RLoc1_{suffix}"})
    wh_id = cast(int, response.json()["id"])
    return cat_id, prod_id, wh_id

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

@pytest.mark.parametrize("run", range(5))
@pytest.mark.asyncio
async def test_concurrent_overselling(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, run: int) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Setup Customer 2
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post("/api/v1/users", headers=admin_headers, json={
        "email": f"cust2_concurrent_{run}@example.com", "name": "Cust Two", "password": "Password123!", "role_id": customer_user.role_id
    })
    customer2_token = await login_and_get_access_token(client, f"cust2_concurrent_{run}@example.com", "Password123!")

    cat_id, prod_id, wh_id = await setup_data_unique(client, admin_token)

    # Set inventory exactly to 1
    await client.post(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 1, "reason": "Restock"
    })

    # Both users add product to cart (qty 1)
    c1_headers = {"Authorization": f"Bearer {customer_token}"}
    c2_headers = {"Authorization": f"Bearer {customer2_token}"}

    await client.post("/api/v1/cart/items", headers=c1_headers, json={"product_id": prod_id, "quantity": 1})
    await client.post("/api/v1/cart/items", headers=c2_headers, json={"product_id": prod_id, "quantity": 1})

    # Independent transactions occur because test client uses override_get_db which creates a new AsyncSession per request
    # Fire both orders concurrently
    req1 = client.post("/api/v1/orders", headers=c1_headers)
    req2 = client.post("/api/v1/orders", headers=c2_headers)

    res1, res2 = await asyncio.gather(req1, req2)
    
    statuses = [res1.status_code, res2.status_code]
    assert statuses.count(201) == 1, "Exactly one request should succeed"
    assert statuses.count(409) == 1, "Exactly one request should fail with conflict"

    loser_headers = c1_headers if res1.status_code == 409 else c2_headers
    winner_headers = c1_headers if res1.status_code == 201 else c2_headers

    loser_cart = await client.get("/api/v1/cart", headers=loser_headers)
    assert len(loser_cart.json()["items"]) == 1, "Losing customer cart should retain item"

    winner_cart = await client.get("/api/v1/cart", headers=winner_headers)
    assert len(winner_cart.json()["items"]) == 0, "Winning customer cart should be cleared"

    async with TestSessionLocal() as verify_session:
        result = await verify_session.execute(select(Inventory).where(Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id))
        inv = result.scalars().first()
        assert inv.available_quantity == 0
        assert inv.reserved_quantity == 1
        assert inv.available_quantity >= 0
        assert inv.reserved_quantity >= 0

        result = await verify_session.execute(select(InventoryReservation).where(InventoryReservation.product_id == prod_id))
        reservations = result.scalars().all()
        assert len(reservations) == 1
        assert reservations[0].quantity == 1
        assert reservations[0].status == ReservationStatus.ACTIVE

        result = await verify_session.execute(select(Order).join(Order.items).where(Order.items.property.mapper.class_.product_id == prod_id))
        orders = result.unique().scalars().all()
        assert len(orders) == 1


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

    cat_id, prod_id, wh_id = await setup_data_unique(client, admin_token)
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
    await db_session.commit()
    # Check inventory
    await db_session.refresh(reservations[0])
    assert reservations[0].status == ReservationStatus.RELEASED

    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["available_quantity"] == 10
    assert inv_response.json()["reserved_quantity"] == 0

    # Idempotent release
    await service.release_reservation(reservation_id)
    await db_session.commit()
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
    await db_session.commit()
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["available_quantity"] == 8
    assert inv_response.json()["reserved_quantity"] == 0

    # Idempotent confirm
    await service.confirm_reservation(res2.id)
    await db_session.commit()
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
    await db_session.commit()
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    # Available was 8, ordered 1 (so 7), then expired (so 8 again)
    assert inv_response.json()["available_quantity"] == 8
    assert inv_response.json()["reserved_quantity"] == 0

    # Idempotent expire
    await service.expire_reservation(res3.id)
    await db_session.commit()
    inv_response = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert inv_response.json()["available_quantity"] == 8

@pytest.mark.asyncio
async def test_duplicate_logical_reservation_blocked(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cat_id, prod_id, wh_id = await setup_data_unique(client, admin_token)
    
    order = Order(user_id=customer_user.id)
    db_session.add(order)
    await db_session.commit()
    await db_session.refresh(order)
    
    res1 = InventoryReservation(order_id=order.id, product_id=prod_id, warehouse_id=wh_id, quantity=1, status=ReservationStatus.ACTIVE, expires_at=datetime.now(timezone.utc))
    db_session.add(res1)
    await db_session.commit()
    
    res2 = InventoryReservation(order_id=order.id, product_id=prod_id, warehouse_id=wh_id, quantity=1, status=ReservationStatus.ACTIVE, expires_at=datetime.now(timezone.utc))
    db_session.add(res2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

@pytest.mark.asyncio
async def test_transition_does_not_commit_unexpectedly(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cat_id, prod_id, wh_id = await setup_data_unique(client, admin_token)
    
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 10, "reason": "Restock"
    })
    
    order = Order(user_id=customer_user.id)
    db_session.add(order)
    await db_session.commit()
    
    res = InventoryReservation(order_id=order.id, product_id=prod_id, warehouse_id=wh_id, quantity=1, status=ReservationStatus.ACTIVE, expires_at=datetime.now(timezone.utc))
    db_session.add(res)
    
    inventory = await db_session.execute(select(Inventory).where(Inventory.product_id == prod_id))
    inv_obj = inventory.scalars().first()
    if inv_obj:
        inv_obj.reserved_quantity = 1
        inv_obj.available_quantity = 9
    await db_session.commit()
    
    service = InventoryReservationService(db_session)
    await service.release_reservation(res.id)
    
    await db_session.refresh(res)
    assert res.status == ReservationStatus.RELEASED
    
    res_id = res.id
    await db_session.rollback()
    
    res_again = await db_session.get(InventoryReservation, res_id)
    assert res_again is not None
    assert res_again.status == ReservationStatus.ACTIVE

@pytest.mark.asyncio
async def test_invalid_reserved_quantity_does_not_go_negative(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cat_id, prod_id, wh_id = await setup_data_unique(client, admin_token)
    
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 10, "reason": "Restock"
    })
    
    order = Order(user_id=customer_user.id)
    db_session.add(order)
    await db_session.commit()
    
    res = InventoryReservation(order_id=order.id, product_id=prod_id, warehouse_id=wh_id, quantity=5, status=ReservationStatus.ACTIVE, expires_at=datetime.now(timezone.utc))
    db_session.add(res)
    
    inventory = await db_session.execute(select(Inventory).where(Inventory.product_id == prod_id))
    inv_obj = inventory.scalars().first()
    if inv_obj:
        inv_obj.reserved_quantity = 2
    await db_session.commit()
    
    service = InventoryReservationService(db_session)
    with pytest.raises(InvalidReservationStateError) as exc:
        await service.release_reservation(res.id)
    assert "reserved_quantity" in str(exc.value)

@pytest.mark.asyncio
async def test_missing_inventory_raises_domain_exception(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cat_id, prod_id, wh_id = await setup_data_unique(client, admin_token)
    
    inventory = await db_session.execute(select(Inventory).where(Inventory.product_id == prod_id))
    inv_obj = inventory.scalars().first()
    if inv_obj:
        await db_session.delete(inv_obj)
        await db_session.commit()
        
    order = Order(user_id=customer_user.id)
    db_session.add(order)
    await db_session.commit()
    
    res = InventoryReservation(order_id=order.id, product_id=prod_id, warehouse_id=wh_id, quantity=1, status=ReservationStatus.ACTIVE, expires_at=datetime.now(timezone.utc))
    db_session.add(res)
    await db_session.commit()
    
    service = InventoryReservationService(db_session)
    with pytest.raises(InventoryNotFoundError) as exc:
        await service.release_reservation(res.id)
    assert "Inventory record missing" in str(exc.value)


@pytest.mark.parametrize("run", range(5))
@pytest.mark.asyncio
async def test_concurrent_multi_quantity_overselling(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, run: int) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Setup Customer 2
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post("/api/v1/users", headers=admin_headers, json={
        "email": f"cust2_multi_{run}@example.com", "name": "Cust Two", "password": "Password123!", "role_id": customer_user.role_id
    })
    customer2_token = await login_and_get_access_token(client, f"cust2_multi_{run}@example.com", "Password123!")

    cat_id, prod_id, wh_id = await setup_data_unique(client, admin_token)

    # Set inventory exactly to 5
    await client.post(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 5, "reason": "Restock"
    })

    # Both users add product to cart (qty 3)
    c1_headers = {"Authorization": f"Bearer {customer_token}"}
    c2_headers = {"Authorization": f"Bearer {customer2_token}"}

    await client.post("/api/v1/cart/items", headers=c1_headers, json={"product_id": prod_id, "quantity": 3})
    await client.post("/api/v1/cart/items", headers=c2_headers, json={"product_id": prod_id, "quantity": 3})

    # Fire both orders concurrently
    req1 = client.post("/api/v1/orders", headers=c1_headers)
    req2 = client.post("/api/v1/orders", headers=c2_headers)

    res1, res2 = await asyncio.gather(req1, req2)
    
    statuses = [res1.status_code, res2.status_code]
    assert statuses.count(201) == 1, "Exactly one request should succeed"
    assert statuses.count(409) == 1, "Exactly one request should fail with conflict"

    loser_headers = c1_headers if res1.status_code == 409 else c2_headers
    winner_headers = c1_headers if res1.status_code == 201 else c2_headers

    loser_cart = await client.get("/api/v1/cart", headers=loser_headers)
    assert len(loser_cart.json()["items"]) == 1
    assert loser_cart.json()["items"][0]["quantity"] == 3

    winner_cart = await client.get("/api/v1/cart", headers=winner_headers)
    assert len(winner_cart.json()["items"]) == 0

    async with TestSessionLocal() as verify_session:
        result = await verify_session.execute(select(Inventory).where(Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id))
        inv = result.scalars().first()
        assert inv.available_quantity == 2
        assert inv.reserved_quantity == 3
        assert inv.available_quantity >= 0
        assert inv.reserved_quantity >= 0

        result = await verify_session.execute(select(InventoryReservation).where(InventoryReservation.product_id == prod_id))
        reservations = result.scalars().all()
        assert len(reservations) == 1
        assert reservations[0].quantity == 3
        assert reservations[0].status == ReservationStatus.ACTIVE

        result = await verify_session.execute(select(Order).join(Order.items).where(Order.items.property.mapper.class_.product_id == prod_id))
        orders = result.unique().scalars().all()
        assert len(orders) == 1



@pytest.mark.parametrize("run", range(5))
@pytest.mark.asyncio
async def test_concurrent_sufficient_stock_success(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, run: int) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Setup Customer 2
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post("/api/v1/users", headers=admin_headers, json={
        "email": f"cust2_suff_{run}@example.com", "name": "Cust Two", "password": "Password123!", "role_id": customer_user.role_id
    })
    customer2_token = await login_and_get_access_token(client, f"cust2_suff_{run}@example.com", "Password123!")

    cat_id, prod_id, wh_id = await setup_data_unique(client, admin_token)

    # Set inventory exactly to 10
    await client.post(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 10, "reason": "Restock"
    })

    # Users add product to cart
    c1_headers = {"Authorization": f"Bearer {customer_token}"}
    c2_headers = {"Authorization": f"Bearer {customer2_token}"}

    await client.post("/api/v1/cart/items", headers=c1_headers, json={"product_id": prod_id, "quantity": 3})
    await client.post("/api/v1/cart/items", headers=c2_headers, json={"product_id": prod_id, "quantity": 4})

    # Fire both orders concurrently
    req1 = client.post("/api/v1/orders", headers=c1_headers)
    req2 = client.post("/api/v1/orders", headers=c2_headers)

    res1, res2 = await asyncio.gather(req1, req2)
    
    assert res1.status_code == 201
    assert res2.status_code == 201

    c1_cart = await client.get("/api/v1/cart", headers=c1_headers)
    assert len(c1_cart.json()["items"]) == 0

    c2_cart = await client.get("/api/v1/cart", headers=c2_headers)
    assert len(c2_cart.json()["items"]) == 0

    async with TestSessionLocal() as verify_session:
        result = await verify_session.execute(select(Inventory).where(Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id))
        inv = result.scalars().first()
        assert inv.available_quantity == 3
        assert inv.reserved_quantity == 7
        assert inv.available_quantity >= 0
        assert inv.reserved_quantity >= 0

        result = await verify_session.execute(select(InventoryReservation).where(InventoryReservation.product_id == prod_id))
        reservations = result.scalars().all()
        assert len(reservations) == 2
        res_quantities = sorted([r.quantity for r in reservations])
        assert res_quantities == [3, 4]
        for r in reservations:
            assert r.status == ReservationStatus.ACTIVE

        result = await verify_session.execute(select(Order).join(Order.items).where(Order.items.property.mapper.class_.product_id == prod_id))
        orders = result.unique().scalars().all()
        assert len(orders) == 2



@pytest.mark.parametrize("run", range(5))
@pytest.mark.asyncio
async def test_concurrent_multi_product_deadlock(client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, run: int) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Setup Customer 2
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.post("/api/v1/users", headers=admin_headers, json={
        "email": f"cust2_dl_{run}@example.com", "name": "Cust Two", "password": "Password123!", "role_id": customer_user.role_id
    })
    customer2_token = await login_and_get_access_token(client, f"cust2_dl_{run}@example.com", "Password123!")

    suffix = uuid.uuid4().hex[:8]
    # Create products
    response = await client.post("/api/v1/categories", headers=admin_headers, json={"name": f"CatRes_{suffix}", "slug": f"catres_{suffix}"})
    cat_id = cast(int, response.json()["id"])
    
    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": f"ProdA_{suffix}", "slug": f"proda_{suffix}", "sku": f"SKUA_{suffix}", "price": 10.0, "category_id": cat_id, "is_active": True
    })
    prodA_id = cast(int, response.json()["id"])

    response = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": f"ProdB_{suffix}", "slug": f"prodb_{suffix}", "sku": f"SKUB_{suffix}", "price": 10.0, "category_id": cat_id, "is_active": True
    })
    prodB_id = cast(int, response.json()["id"])

    response = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": f"WHA_{suffix}", "name": f"LocA_{suffix}"})
    wh_id = cast(int, response.json()["id"])

    # Stock both products
    await client.post(f"/api/v1/inventory/products/{prodA_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 100, "reason": "Restock"
    })
    await client.post(f"/api/v1/inventory/products/{prodB_id}/warehouses/{wh_id}/adjust", headers=admin_headers, json={
        "available_quantity_change": 100, "reason": "Restock"
    })

    # C1 cart: A then B
    c1_headers = {"Authorization": f"Bearer {customer_token}"}
    await client.post("/api/v1/cart/items", headers=c1_headers, json={"product_id": prodA_id, "quantity": 1})
    await client.post("/api/v1/cart/items", headers=c1_headers, json={"product_id": prodB_id, "quantity": 1})

    # C2 cart: B then A
    c2_headers = {"Authorization": f"Bearer {customer2_token}"}
    await client.post("/api/v1/cart/items", headers=c2_headers, json={"product_id": prodB_id, "quantity": 1})
    await client.post("/api/v1/cart/items", headers=c2_headers, json={"product_id": prodA_id, "quantity": 1})

    req1 = client.post("/api/v1/orders", headers=c1_headers)
    req2 = client.post("/api/v1/orders", headers=c2_headers)

    # Use timeout to detect deadlock
    try:
        res1, res2 = await asyncio.wait_for(asyncio.gather(req1, req2), timeout=10.0)
    except asyncio.TimeoutError:
        pytest.fail("Deadlock detected during concurrent orders!")

    assert res1.status_code == 201
    assert res2.status_code == 201

    c1_cart = await client.get("/api/v1/cart", headers=c1_headers)
    assert len(c1_cart.json()["items"]) == 0
    c2_cart = await client.get("/api/v1/cart", headers=c2_headers)
    assert len(c2_cart.json()["items"]) == 0

    async with TestSessionLocal() as verify_session:
        result = await verify_session.execute(select(Inventory).where(Inventory.product_id.in_([prodA_id, prodB_id])))
        inventories = result.scalars().all()
        assert len(inventories) == 2
        for inv in inventories:
            assert inv.available_quantity == 98
            assert inv.reserved_quantity == 2
            assert inv.available_quantity >= 0
            assert inv.reserved_quantity >= 0

        result = await verify_session.execute(select(InventoryReservation).where(InventoryReservation.product_id.in_([prodA_id, prodB_id])))
        reservations = result.scalars().all()
        assert len(reservations) == 4
        for r in reservations:
            assert r.status == ReservationStatus.ACTIVE

        result = await verify_session.execute(select(Order).join(Order.items).where(Order.items.property.mapper.class_.product_id.in_([prodA_id, prodB_id])))
        orders = result.unique().scalars().all()
        assert len(orders) == 2

