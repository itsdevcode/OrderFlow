import asyncio
from decimal import Decimal
from typing import cast
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import Inventory
from app.models.inventory_reservation import InventoryReservation, ReservationStatus
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.models.payment import Payment, PaymentStatus
from app.models.user import User
from app.services.payment import PaymentService
from tests.conftest import TestSessionLocal


async def login_and_get_access_token(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return cast(str, response.json()["access_token"])


async def setup_test_order(
    client: AsyncClient,
    admin_token: str,
    customer_token: str,
    unit_price: float = 150.50,
    quantity: int = 2,
    sku_prefix: str = "PAY",
) -> tuple[int, int, int]:
    """Helper to set up catalog, inventory, cart, and return (order_id, product_id, warehouse_id)"""
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    uid = uuid.uuid4().hex[:6]
    cat_res = await client.post(
        "/api/v1/categories",
        headers=admin_headers,
        json={"name": f"PayCat_{uid}", "slug": f"paycat-{uid}"},
    )
    cat_id = cast(int, cat_res.json()["id"])

    prod_res = await client.post(
        "/api/v1/products",
        headers=admin_headers,
        json={
            "name": f"PayProd_{uid}",
            "slug": f"payprod-{uid}",
            "sku": f"{sku_prefix}_{uid}",
            "price": unit_price,
            "category_id": cat_id,
            "is_active": True,
        },
    )
    prod_id = cast(int, prod_res.json()["id"])

    wh_res = await client.post(
        "/api/v1/warehouses",
        headers=admin_headers,
        json={"code": f"PWH_{uid}", "name": f"Pay WH {uid}"},
    )
    wh_id = cast(int, wh_res.json()["id"])

    adjust_res = await client.post(
        f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust",
        headers=admin_headers,
        json={"available_quantity_change": 100, "reason": "Initial stock"},
    )
    assert adjust_res.status_code == 200

    add_res = await client.post(
        "/api/v1/cart/items",
        headers=customer_headers,
        json={"product_id": prod_id, "quantity": quantity},
    )
    assert add_res.status_code == 200

    order_res = await client.post("/api/v1/orders", headers=customer_headers)
    assert order_res.status_code == 201
    order_id = cast(int, order_res.json()["id"])

    return order_id, prod_id, wh_id


async def test_unauthenticated_payment_request(client: AsyncClient) -> None:
    # POST unauthenticated
    response = await client.post(
        "/api/v1/orders/1/payments",
        json={"idempotency_key": "unauth-key-1"},
    )
    assert response.status_code == 401

    # GET unauthenticated
    response = await client.get("/api/v1/orders/1/payments")
    assert response.status_code == 401


async def test_order_ownership_isolation(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer1_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Register Customer 2
    await client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "email": "pay_cust2@example.com",
            "name": "Customer 2",
            "password": "Password123!",
            "role_id": customer_user.role_id,
        },
    )
    customer2_token = await login_and_get_access_token(client, "pay_cust2@example.com", "Password123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer1_token)

    # Customer 2 attempts to initiate payment on Customer 1's order -> 404
    cust2_headers = {"Authorization": f"Bearer {customer2_token}"}
    res_post = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=cust2_headers,
        json={"idempotency_key": "cust2-steals-pay"},
    )
    assert res_post.status_code == 404
    assert res_post.json()["detail"] == "Order not found"

    # Customer 2 attempts to list payments on Customer 1's order -> 404
    res_get = await client.get(
        f"/api/v1/orders/{order_id}/payments",
        headers=cust2_headers,
    )
    assert res_get.status_code == 404
    assert res_get.json()["detail"] == "Order not found"


async def test_payment_amount_derived_from_order_items_and_precision(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # unit price 99.95, quantity 3 => 299.85
    order_id, prod_id, _ = await setup_test_order(
        client, admin_token, customer_token, unit_price=99.95, quantity=3
    )

    # Change product price after order snapshot
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await client.patch(
        f"/api/v1/products/{prod_id}",
        headers=admin_headers,
        json={"price": 1000.00},
    )

    customer_headers = {"Authorization": f"Bearer {customer_token}"}
    key = f"key-derive-{uuid.uuid4().hex}"
    # Even if client tries to pass extra fields or amount, schema doesn't accept client amount
    response = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key, "amount": 1.00},
    )
    assert response.status_code == 201
    pay_data = response.json()

    # Amount derived strictly from OrderItems snapshot: 99.95 * 3 = 299.85
    assert pay_data["amount"] == "299.85"
    assert pay_data["currency"] == "INR"
    assert pay_data["status"] == "PENDING"
    assert pay_data["idempotency_key"] == key
    assert pay_data["provider_payment_id"] is not None


async def test_successful_pending_payment_initiation_and_list(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token, unit_price=50.0, quantity=2)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    key = f"init-key-{uuid.uuid4().hex}"
    res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key},
    )
    assert res.status_code == 201
    created_payment = res.json()
    assert created_payment["order_id"] == order_id
    assert created_payment["status"] == "PENDING"
    assert created_payment["amount"] == "100.00"

    # List payments
    res_list = await client.get(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
    )
    assert res_list.status_code == 200
    payments = res_list.json()
    assert len(payments) == 1
    assert payments[0]["id"] == created_payment["id"]
    assert payments[0]["idempotency_key"] == key


async def test_repeated_idempotency_key_returns_existing_payment(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    key = f"repeat-key-{uuid.uuid4().hex}"
    res1 = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key},
    )
    assert res1.status_code == 201
    pay1 = res1.json()

    # Second call with identical key
    res2 = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key},
    )
    # Returns existing payment
    assert res2.status_code in (200, 201)
    pay2 = res2.json()
    assert pay1["id"] == pay2["id"]
    assert pay1["idempotency_key"] == pay2["idempotency_key"]
    assert pay1["created_at"] == pay2["created_at"]


async def test_idempotency_key_conflict_different_order(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id_1, _, _ = await setup_test_order(client, admin_token, customer_token, unit_price=20.0, quantity=1)
    order_id_2, _, _ = await setup_test_order(client, admin_token, customer_token, unit_price=30.0, quantity=1)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    shared_key = f"conflict-key-{uuid.uuid4().hex}"
    res1 = await client.post(
        f"/api/v1/orders/{order_id_1}/payments",
        headers=customer_headers,
        json={"idempotency_key": shared_key},
    )
    assert res1.status_code == 201

    # Attempt to reuse the same key for order_id_2 -> 409 Conflict
    res2 = await client.post(
        f"/api/v1/orders/{order_id_2}/payments",
        headers=customer_headers,
        json={"idempotency_key": shared_key},
    )
    assert res2.status_code == 409
    assert "already used for another order" in res2.json()["detail"].lower()


async def test_concurrent_duplicate_payment_initiation(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    same_key = f"concurrent-same-key-{uuid.uuid4().hex}"

    async def send_payment_attempt() -> int:
        res = await client.post(
            f"/api/v1/orders/{order_id}/payments",
            headers=customer_headers,
            json={"idempotency_key": same_key},
        )
        return res.status_code

    # Launch 5 concurrent requests with the exact same key
    tasks = [send_payment_attempt() for _ in range(5)]
    results = await asyncio.gather(*tasks)

    # All should succeed (200 or 201) returning the payment
    assert all(code in (200, 201) for code in results)

    # Verify committed state with a fresh session
    async with TestSessionLocal() as session:
        stmt = select(Payment).where(Payment.order_id == order_id)
        result = await session.execute(stmt)
        payments = result.scalars().all()
        # Exactly 1 payment record created
        assert len(payments) == 1
        assert payments[0].idempotency_key == same_key


async def test_concurrent_different_key_initiation_on_same_order(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    keys = [f"concurrent-diff-key-{i}-{uuid.uuid4().hex}" for i in range(5)]

    async def send_payment_attempt(key: str) -> int:
        res = await client.post(
            f"/api/v1/orders/{order_id}/payments",
            headers=customer_headers,
            json={"idempotency_key": key},
        )
        return res.status_code

    tasks = [send_payment_attempt(k) for k in keys]
    results = await asyncio.gather(*tasks)

    # Exactly 1 request succeeds (201), the rest conflict (409) because a PENDING payment already exists
    assert results.count(201) == 1
    assert results.count(409) == 4

    # Verify committed state with fresh session
    async with TestSessionLocal() as session:
        stmt = select(Payment).where(Payment.order_id == order_id)
        result = await session.execute(stmt)
        payments = result.scalars().all()
        # Exactly 1 active PENDING payment
        assert len(payments) == 1
        assert payments[0].status == PaymentStatus.PENDING


async def test_payment_already_succeeded_blocks_further_charges(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    key1 = f"succ-key-{uuid.uuid4().hex}"
    res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key1},
    )
    assert res.status_code == 201
    pay_id = res.json()["id"]

    # Internal service transitions payment to SUCCEEDED
    async with TestSessionLocal() as session:
        service = PaymentService(session)
        updated_pay = await service.transition_payment_status(pay_id, PaymentStatus.SUCCEEDED)
        assert updated_pay.status == PaymentStatus.SUCCEEDED

    # New charge attempt with new key on already paid order -> 409 Conflict
    key2 = f"new-key-on-paid-{uuid.uuid4().hex}"
    res2 = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key2},
    )
    assert res2.status_code == 409
    assert "already been paid" in res2.json()["detail"].lower()


async def test_payment_lifecycle_state_transitions(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    key = f"trans-key-{uuid.uuid4().hex}"
    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key},
    )
    assert init_res.status_code == 201
    payment_id = cast(int, init_res.json()["id"])

    # Test PENDING -> SUCCEEDED
    async with TestSessionLocal() as session:
        service = PaymentService(session)
        p = await service.transition_payment_status(payment_id, PaymentStatus.SUCCEEDED)
        assert p.status == PaymentStatus.SUCCEEDED

    # Test repeated valid transition is idempotent
    async with TestSessionLocal() as session:
        service = PaymentService(session)
        p = await service.transition_payment_status(payment_id, PaymentStatus.SUCCEEDED)
        assert p.status == PaymentStatus.SUCCEEDED

    # Test invalid transition: SUCCEEDED -> FAILED -> raises InvalidPaymentStateTransitionError
    from app.exceptions.payment import InvalidPaymentStateTransitionError
    async with TestSessionLocal() as session:
        service = PaymentService(session)
        with pytest.raises(InvalidPaymentStateTransitionError):
            await service.transition_payment_status(payment_id, PaymentStatus.FAILED)

    # Test valid transition: SUCCEEDED -> REFUNDED
    async with TestSessionLocal() as session:
        service = PaymentService(session)
        p = await service.transition_payment_status(payment_id, PaymentStatus.REFUNDED)
        assert p.status == PaymentStatus.REFUNDED

    # Test invalid transition: REFUNDED -> SUCCEEDED -> raises
    async with TestSessionLocal() as session:
        service = PaymentService(session)
        with pytest.raises(InvalidPaymentStateTransitionError):
            await service.transition_payment_status(payment_id, PaymentStatus.SUCCEEDED)


async def test_cancelled_order_rejects_payment(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)

    # Cancel order in database
    async with TestSessionLocal() as session:
        stmt = select(Order).where(Order.id == order_id)
        res = await session.execute(stmt)
        order = res.scalar_one()
        order.status = OrderStatus.CANCELLED
        await session.commit()

    customer_headers = {"Authorization": f"Bearer {customer_token}"}
    key = f"cancel-order-pay-{uuid.uuid4().hex}"
    res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key},
    )
    assert res.status_code == 400
    assert "cancelled order" in res.json()["detail"].lower()


async def test_payment_initiation_does_not_change_inventory_reservation(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, prod_id, wh_id = await setup_test_order(
        client, admin_token, customer_token, unit_price=75.0, quantity=3
    )

    # Check reservation and inventory before payment initiation
    async with TestSessionLocal() as session:
        # InventoryReservation
        stmt_res = select(InventoryReservation).where(InventoryReservation.order_id == order_id)
        res_res = await session.execute(stmt_res)
        reservations_before = res_res.scalars().all()
        assert len(reservations_before) == 1
        resv = reservations_before[0]
        assert resv.status == ReservationStatus.ACTIVE
        assert resv.quantity == 3

        # Inventory
        stmt_inv = select(Inventory).where(
            Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id
        )
        res_inv = await session.execute(stmt_inv)
        inv = res_inv.scalar_one()
        assert inv.available_quantity == 97
        assert inv.reserved_quantity == 3

    # Initiate payment
    customer_headers = {"Authorization": f"Bearer {customer_token}"}
    key = f"inv-unchanged-{uuid.uuid4().hex}"
    res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key},
    )
    assert res.status_code == 201

    # Verify inventory and reservation remain completely unchanged
    async with TestSessionLocal() as session:
        stmt_res = select(InventoryReservation).where(InventoryReservation.order_id == order_id)
        res_res = await session.execute(stmt_res)
        reservations_after = res_res.scalars().all()
        assert len(reservations_after) == 1
        resv_after = reservations_after[0]
        assert resv_after.status == ReservationStatus.ACTIVE
        assert resv_after.quantity == 3

        stmt_inv = select(Inventory).where(
            Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id
        )
        res_inv = await session.execute(stmt_inv)
        inv_after = res_inv.scalar_one()
        assert inv_after.available_quantity == 97
        assert inv_after.reserved_quantity == 3


async def test_rollback_on_provider_failure(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    # Our FakePaymentProvider raises RuntimeError when key contains 'error'
    error_key = f"key-with-error-{uuid.uuid4().hex}"
    res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": error_key},
    )
    assert res.status_code == 502
    assert "payment provider processing failed" in res.json()["detail"].lower()
    # Internal error detail should not be exposed to client
    assert "outage" not in res.json()["detail"].lower()

    # Verify with fresh session that no partial payment record was committed
    async with TestSessionLocal() as session:
        stmt = select(Payment).where(Payment.order_id == order_id)
        result = await session.execute(stmt)
        payments = result.scalars().all()
        assert len(payments) == 0


async def test_provider_explicit_failure_persists_failed_status(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    # Our FakePaymentProvider returns success=False when key contains 'fail'
    fail_key = f"key-with-fail-{uuid.uuid4().hex}"
    res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": fail_key},
    )
    # Payment creation succeeds with status FAILED (not PENDING)
    assert res.status_code == 201
    pay_data = res.json()
    assert pay_data["status"] == "FAILED"
    assert pay_data["idempotency_key"] == fail_key

    # Verify committed state with a fresh session
    async with TestSessionLocal() as session:
        stmt = select(Payment).where(Payment.order_id == order_id)
        result = await session.execute(stmt)
        payments = result.scalars().all()
        assert len(payments) == 1
        assert payments[0].status == PaymentStatus.FAILED

    # Because the payment is FAILED (and not PENDING or SUCCEEDED),
    # the customer may retry with a new key on the same order!
    retry_key = f"retry-key-after-{uuid.uuid4().hex}"
    res_retry = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": retry_key},
    )
    assert res_retry.status_code == 201
    assert res_retry.json()["status"] == "PENDING"


async def test_unsupported_provider_rejected_and_no_record_created(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    key = f"unsupported-prov-{uuid.uuid4().hex}"
    # Request unsupported provider 'stripe' when configured provider is 'fake_provider'
    res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key, "provider": "stripe"},
    )
    # Must reject with HTTP 400
    assert res.status_code == 400
    assert "unsupported payment provider" in res.json()["detail"].lower()

    # Verify no Payment record is inserted in the database
    async with TestSessionLocal() as session:
        stmt = select(Payment).where(Payment.order_id == order_id)
        result = await session.execute(stmt)
        payments = result.scalars().all()
        assert len(payments) == 0


async def test_idempotency_key_conflict_incompatible_provider(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    key = f"provider-mismatch-{uuid.uuid4().hex}"
    # 1. fake_provider succeeds
    res1 = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key, "provider": "fake_provider"},
    )
    assert res1.status_code == 201
    assert res1.json()["provider"] == "fake_provider"
    pay_id = res1.json()["id"]

    # 2. Replay with same key and same order, but unsupported/incompatible provider -> 409 Conflict
    # When validating against existing payment with same key, provider incompatibility takes precedence or rejects
    res2 = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key, "provider": "other_provider"},
    )
    # Rejected as unsupported provider (400) or incompatible replay (409)
    assert res2.status_code in (400, 409)

    # 3. Direct service-level test with multi-provider configured to assert 409 incompatibility
    from app.services.fake_payment_provider import FakePaymentProvider
    async with TestSessionLocal() as session:
        # Service configured with provider 'custom_gateway'
        prov_service = PaymentService(session, provider=FakePaymentProvider("custom_gateway"))
        # Attempt to reuse existing payment key (which was 'fake_provider') -> raises IdempotencyKeyConflictError (409)
        from app.exceptions.payment import IdempotencyKeyConflictError
        with pytest.raises(IdempotencyKeyConflictError):
            await prov_service.initiate_payment(
                order_id=order_id,
                user=customer_user,
                idempotency_key=key,
                provider_name="custom_gateway",
            )

    # 4. Complete the payment to SUCCEEDED
    async with TestSessionLocal() as session:
        service = PaymentService(session)
        _ = await service.transition_payment_status(pay_id, PaymentStatus.SUCCEEDED)

    # 5. Replay with identical key, same order, same provider after completion -> returns existing payment
    res3 = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=customer_headers,
        json={"idempotency_key": key, "provider": "fake_provider"},
    )
    assert res3.status_code in (200, 201)
    assert res3.json()["id"] == pay_id
    assert res3.json()["status"] == "SUCCEEDED"

    # 6. Replay with incompatible provider after completion -> rejected
    async with TestSessionLocal() as session:
        prov_service = PaymentService(session, provider=FakePaymentProvider("custom_gateway"))
        with pytest.raises(IdempotencyKeyConflictError):
            await prov_service.initiate_payment(
                order_id=order_id,
                user=customer_user,
                idempotency_key=key,
                provider_name="custom_gateway",
            )



async def test_postgresql_partial_unique_indexes_enforce_rules(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """Direct PostgreSQL integration test to verify partial unique index constraints at DB level."""
    from sqlalchemy.exc import IntegrityError

    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")
    order_id, _, _ = await setup_test_order(client, admin_token, customer_token)

    # 1. Test PENDING partial unique index: cannot insert two PENDING payments for the same order_id
    async with TestSessionLocal() as session:
        p1 = Payment(
            order_id=order_id,
            provider="prov1",
            idempotency_key=f"db-uq-pending-1-{uuid.uuid4().hex}",
            amount=Decimal("100.00"),
            currency="INR",
            status=PaymentStatus.PENDING,
        )
        session.add(p1)
        await session.commit()

    async with TestSessionLocal() as session:
        p2 = Payment(
            order_id=order_id,
            provider="prov2",
            idempotency_key=f"db-uq-pending-2-{uuid.uuid4().hex}",
            amount=Decimal("100.00"),
            currency="INR",
            status=PaymentStatus.PENDING,
        )
        session.add(p2)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()
        assert "uq_payments_order_id_pending" in str(exc_info.value).lower()

    # 2. Test that multiple FAILED payments for the same order ARE allowed
    async with TestSessionLocal() as session:
        # First transition existing PENDING payment to FAILED
        stmt = select(Payment).where(Payment.id == p1.id)
        res = await session.execute(stmt)
        existing = res.scalar_one()
        existing.status = PaymentStatus.FAILED
        await session.commit()

    async with TestSessionLocal() as session:
        p_failed_2 = Payment(
            order_id=order_id,
            provider="prov2",
            idempotency_key=f"db-uq-failed-2-{uuid.uuid4().hex}",
            amount=Decimal("100.00"),
            currency="INR",
            status=PaymentStatus.FAILED,
        )
        session.add(p_failed_2)
        # Should succeed without violating unique index
        await session.commit()

    # 3. Test SUCCEEDED partial unique index: cannot have two SUCCEEDED payments for the same order_id
    async with TestSessionLocal() as session:
        p_succ_1 = Payment(
            order_id=order_id,
            provider="prov1",
            idempotency_key=f"db-uq-succ-1-{uuid.uuid4().hex}",
            amount=Decimal("100.00"),
            currency="INR",
            status=PaymentStatus.SUCCEEDED,
        )
        session.add(p_succ_1)
        await session.commit()

    async with TestSessionLocal() as session:
        p_succ_2 = Payment(
            order_id=order_id,
            provider="prov2",
            idempotency_key=f"db-uq-succ-2-{uuid.uuid4().hex}",
            amount=Decimal("100.00"),
            currency="INR",
            status=PaymentStatus.SUCCEEDED,
        )
        session.add(p_succ_2)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()
        assert "uq_payments_order_id_succeeded" in str(exc_info.value).lower()


