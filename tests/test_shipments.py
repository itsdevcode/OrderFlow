import asyncio
from typing import cast
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.inventory import Inventory
from app.models.inventory_reservation import InventoryReservation, ReservationStatus
from app.models.order import Order, OrderStatus
from app.models.payment import Payment, PaymentStatus
from app.models.shipment import Shipment, ShipmentStatus
from app.models.user import User
from tests.conftest import TestSessionLocal
from tests.test_payment_webhooks import make_signed_webhook_payload
from tests.test_payments import login_and_get_access_token, setup_test_order


async def create_confirmed_paid_order(
    client: AsyncClient,
    admin_token: str,
    customer_token: str,
    sku_prefix: str = "SHIP",
) -> tuple[int, int, int]:
    """
    Create an order and perform real payment initiation + signed webhook verification
    to transition Order -> CONFIRMED and Payment -> SUCCEEDED.
    """
    order_id, prod_id, wh_id = await setup_test_order(
        client,
        admin_token,
        customer_token,
        unit_price=120.00,
        quantity=2,
        sku_prefix=sku_prefix,
    )

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {customer_token}"},
        json={"idempotency_key": f"pay-key-{uuid.uuid4().hex}"},
    )
    assert init_res.status_code == 201
    pay_data = init_res.json()
    provider_payment_id = cast(str, pay_data["provider_payment_id"])

    # Deliver signed webhook for payment.succeeded
    raw_bytes, headers = make_signed_webhook_payload(
        event_id=f"evt_pay_succ_{uuid.uuid4().hex}",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="240.00",
    )
    wh_res = await client.post("/api/v1/webhooks/payments", content=raw_bytes, headers=headers)
    assert wh_res.status_code == 200

    # Verify Order is CONFIRMED and Payment is SUCCEEDED
    async with TestSessionLocal() as session:
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert order.status == OrderStatus.CONFIRMED

        pay = (await session.execute(select(Payment).where(Payment.id == pay_data["id"]))).scalar_one()
        assert pay.status == PaymentStatus.SUCCEEDED

    return order_id, prod_id, wh_id


async def test_unauthenticated_shipment_requests(client: AsyncClient) -> None:
    res = await client.post("/api/v1/orders/1/shipments", json={})
    assert res.status_code == 401

    res = await client.get("/api/v1/orders/1/shipments")
    assert res.status_code == 401

    res = await client.get("/api/v1/shipments/1")
    assert res.status_code == 401

    res = await client.patch("/api/v1/shipments/1/status", json={"status": "SHIPPED"})
    assert res.status_code == 401

    res = await client.get("/api/v1/shipments")
    assert res.status_code == 401


async def test_customer_forbidden_from_shipment_creation(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="CUST_FORBID")

    # Customer tries to create shipment -> 403 Forbidden
    res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"carrier": "DHL", "tracking_number": "DHL123"},
    )
    assert res.status_code == 403
    assert "insufficient permissions" in res.json()["detail"].lower()


async def test_customer_forbidden_from_status_updates(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="CUST_UPD")

    # Admin creates shipment
    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert create_res.status_code == 201
    shipment_id = create_res.json()["id"]

    # Customer tries to update status -> 403 Forbidden
    res = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"status": "SHIPPED", "carrier": "FedEx", "tracking_number": "FX12345"},
    )
    assert res.status_code == 403


async def test_admin_and_manager_shipment_creation(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    db_session: AsyncSession,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Create Manager User
    from app.core.roles import RoleName
    from app.core.security import hash_password
    from app.models.role import Role

    mgr_role_stmt = select(Role).where(Role.name == RoleName.MANAGER.value)
    mgr_role = (await db_session.execute(mgr_role_stmt)).scalar_one_or_none()
    if not mgr_role:
        mgr_role = Role(name=RoleName.MANAGER.value)
        db_session.add(mgr_role)
        await db_session.flush()

    manager = User(
        name="Test Manager",
        email="manager@test.com",
        hashed_password=hash_password("ManagerPassword123!"),
        role_id=mgr_role.id,
    )
    db_session.add(manager)
    await db_session.commit()

    manager_token = await login_and_get_access_token(client, "manager@test.com", "ManagerPassword123!")

    # 1. Admin creates shipment
    order_id_1, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="ADMIN_SHIP")
    res_admin = await client.post(
        f"/api/v1/orders/{order_id_1}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert res_admin.status_code == 201
    data_admin = res_admin.json()
    assert data_admin["order_id"] == order_id_1
    assert data_admin["status"] == "PENDING"
    assert data_admin["shipped_at"] is None
    assert data_admin["delivered_at"] is None
    assert data_admin["cancelled_at"] is None

    # 2. Manager creates shipment
    order_id_2, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="MGR_SHIP")
    res_mgr = await client.post(
        f"/api/v1/orders/{order_id_2}/shipments",
        headers={"Authorization": f"Bearer {manager_token}"},
        json={"carrier": "BlueDart", "tracking_number": "BD999888"},
    )
    assert res_mgr.status_code == 201
    data_mgr = res_mgr.json()
    assert data_mgr["order_id"] == order_id_2
    assert data_mgr["carrier"] == "BlueDart"
    assert data_mgr["tracking_number"] == "BD999888"
    assert data_mgr["status"] == "PENDING"


async def test_shipment_creation_for_pending_order_rejected(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Order created but no payment -> Order is PENDING
    order_id, _, _ = await setup_test_order(client, admin_token, cust_token, sku_prefix="PENDING_ORD")

    res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert res.status_code == 409
    assert "eligible" in res.json()["detail"].lower()


async def test_shipment_creation_for_cancelled_order_rejected(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, cust_token, sku_prefix="CANC_ORD")

    # Manually cancel order in DB
    async with TestSessionLocal() as session:
        ord_obj = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        ord_obj.status = OrderStatus.CANCELLED
        await session.commit()

    res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert res.status_code == 409
    assert "eligible" in res.json()["detail"].lower()


async def test_shipment_creation_for_confirmed_order_without_succeeded_payment_rejected(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, cust_token, sku_prefix="CONF_NOPAY")

    # Order is manually set to CONFIRMED without payment
    async with TestSessionLocal() as session:
        ord_obj = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        ord_obj.status = OrderStatus.CONFIRMED
        await session.commit()

    res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert res.status_code == 409
    assert "payment" in res.json()["detail"].lower()


async def test_valid_shipment_creation_after_verified_payment_success(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="PAY_VERIF")

    res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["order_id"] == order_id
    assert data["status"] == "PENDING"


async def test_duplicate_shipment_rejected(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="DUP_SHIP")

    # First shipment creation
    res1 = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert res1.status_code == 201

    # Second shipment creation for same order
    res2 = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"].lower()


async def test_concurrent_duplicate_shipment_creation(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """Test concurrent attempts to create two shipments for the same order using independent sessions."""
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    for rep in range(3):
        order_id, _, _ = await create_confirmed_paid_order(
            client, admin_token, cust_token, sku_prefix=f"CONC_CREATE_{rep}"
        )

        async def attempt_create() -> int:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    f"/api/v1/orders/{order_id}/shipments",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={},
                )
                return r.status_code

        # Fire 5 concurrent creation attempts
        results = await asyncio.gather(*[attempt_create() for _ in range(5)])

        # Exactly one must be 201, others 409
        assert results.count(201) == 1, f"Expected exactly one 201, got {results}"
        assert all(code in (201, 409) for code in results), f"Unexpected codes: {results}"

        # Verify only one shipment exists in DB
        async with TestSessionLocal() as session:
            shipments = (
                await session.execute(
                    select(Shipment).where(Shipment.order_id == order_id)
                )
            ).scalars().all()
            assert len(shipments) == 1


async def test_pending_to_shipped_successful(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="P_TO_S")

    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    shipment_id = create_res.json()["id"]

    # Transition to SHIPPED
    res = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "BlueDart", "tracking_number": f"BD_{uuid.uuid4().hex[:8]}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SHIPPED"
    assert data["carrier"] == "BlueDart"
    assert data["shipped_at"] is not None
    assert data["delivered_at"] is None
    assert data["cancelled_at"] is None


async def test_shipped_requires_carrier_and_tracking_number(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="REQ_TRACK")

    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    shipment_id = create_res.json()["id"]

    # Missing both carrier and tracking
    res1 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED"},
    )
    assert res1.status_code == 409
    assert "requires" in res1.json()["detail"].lower()

    # Empty carrier (whitespace)
    res2 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "   ", "tracking_number": "TRK123"},
    )
    assert res2.status_code == 409

    # Empty tracking number
    res3 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "FedEx", "tracking_number": "  "},
    )
    assert res3.status_code == 409


async def test_pending_to_delivered_rejected(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="P_TO_D")

    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    shipment_id = create_res.json()["id"]

    res = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "DELIVERED"},
    )
    assert res.status_code == 409
    assert "cannot transition" in res.json()["detail"].lower()


async def test_shipped_to_delivered_successful(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="S_TO_D")

    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    shipment_id = create_res.json()["id"]

    # PENDING -> SHIPPED
    ship_res = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "UPS", "tracking_number": "UPS123456"},
    )
    assert ship_res.status_code == 200
    shipped_at = ship_res.json()["shipped_at"]

    # SHIPPED -> DELIVERED
    deliv_res = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "DELIVERED"},
    )
    assert deliv_res.status_code == 200
    data = deliv_res.json()
    assert data["status"] == "DELIVERED"
    assert data["carrier"] == "UPS"
    assert data["tracking_number"] == "UPS123456"
    assert data["shipped_at"] == shipped_at
    assert data["delivered_at"] is not None


async def test_pending_to_cancelled_successful(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="P_TO_C")

    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    shipment_id = create_res.json()["id"]

    res = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "CANCELLED"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "CANCELLED"
    assert data["cancelled_at"] is not None


async def test_cancelled_is_terminal(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="CANC_TERM")

    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    shipment_id = create_res.json()["id"]

    # PENDING -> CANCELLED
    await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "CANCELLED"},
    )

    # CANCELLED -> SHIPPED rejected
    res1 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "FedEx", "tracking_number": "TRK999"},
    )
    assert res1.status_code == 409

    # CANCELLED -> DELIVERED rejected
    res2 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "DELIVERED"},
    )
    assert res2.status_code == 409


async def test_delivered_is_terminal(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="DELIV_TERM")

    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    shipment_id = create_res.json()["id"]

    # PENDING -> SHIPPED -> DELIVERED
    await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "FedEx", "tracking_number": "TRK_D_TERM"},
    )
    await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "DELIVERED"},
    )

    # DELIVERED -> SHIPPED rejected
    res1 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "FedEx", "tracking_number": "TRK_D_TERM"},
    )
    assert res1.status_code == 409

    # DELIVERED -> CANCELLED rejected
    res2 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "CANCELLED"},
    )
    assert res2.status_code == 409


async def test_shipped_to_cancelled_rejected(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="S_TO_C")

    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    shipment_id = create_res.json()["id"]

    await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "FedEx", "tracking_number": "TRK_STOC"},
    )

    # SHIPPED -> CANCELLED rejected
    res = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "CANCELLED"},
    )
    assert res.status_code == 409


async def test_idempotent_repeated_transitions(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="IDEMP_TR")

    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    shipment_id = create_res.json()["id"]

    # 1. Repeated PENDING -> PENDING
    pending_rep = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "PENDING"},
    )
    assert pending_rep.status_code == 200

    # 2. Transition to SHIPPED
    res1 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "DHL", "tracking_number": "DHL-REPEAT"},
    )
    assert res1.status_code == 200
    shipped_at_1 = res1.json()["shipped_at"]

    # Repeated identical SHIPPED request
    res2 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "DHL", "tracking_number": "DHL-REPEAT"},
    )
    assert res2.status_code == 200
    assert res2.json()["shipped_at"] == shipped_at_1

    # 3. Transition to DELIVERED
    deliv1 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "DELIVERED"},
    )
    assert deliv1.status_code == 200
    deliv_at = deliv1.json()["delivered_at"]

    # Repeated DELIVERED request
    deliv2 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "DELIVERED"},
    )
    assert deliv2.status_code == 200
    assert deliv2.json()["delivered_at"] == deliv_at


async def test_incompatible_repeated_transition_payload_rejected(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="INCOMPAT_TR")

    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    shipment_id = create_res.json()["id"]

    # SHIPPED with DHL
    res1 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "DHL", "tracking_number": "DHL-ORIG"},
    )
    assert res1.status_code == 200

    # Repeat SHIPPED with different carrier -> 409 Conflict
    res2 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "FedEx", "tracking_number": "DHL-ORIG"},
    )
    assert res2.status_code == 409

    # Repeat SHIPPED with different tracking number -> 409 Conflict
    res3 = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "DHL", "tracking_number": "DHL-DIFF"},
    )
    assert res3.status_code == 409


async def test_customer_can_view_own_shipment_and_isolation(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    db_session: AsyncSession,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Set up another customer
    from app.core.security import hash_password
    other_cust = User(
        name="Other Cust",
        email="other_cust_ship@example.com",
        hashed_password=hash_password("Password123!"),
        role_id=customer_user.role_id,
    )
    db_session.add(other_cust)
    await db_session.commit()
    other_token = await login_and_get_access_token(client, "other_cust_ship@example.com", "Password123!")

    # Customer 1 order and shipment
    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="VIEW_ISOL")
    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"carrier": "DHL", "tracking_number": "DHL-VIEW-1"},
    )
    assert create_res.status_code == 201
    shipment_id = create_res.json()["id"]

    # 1. Customer 1 can view via /orders/{order_id}/shipments
    res1 = await client.get(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {cust_token}"},
    )
    assert res1.status_code == 200
    assert res1.json()["id"] == shipment_id

    # 2. Customer 1 can view via /shipments/{shipment_id}
    res2 = await client.get(
        f"/api/v1/shipments/{shipment_id}",
        headers={"Authorization": f"Bearer {cust_token}"},
    )
    assert res2.status_code == 200
    assert res2.json()["id"] == shipment_id

    # 3. Customer 2 viewing Customer 1's order shipment returns 404 (no existence leakage)
    res_other1 = await client.get(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert res_other1.status_code == 404

    # 4. Customer 2 viewing Customer 1's shipment returns 404
    res_other2 = await client.get(
        f"/api/v1/shipments/{shipment_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert res_other2.status_code == 404

    # 5. Customer 2 cannot list all shipments -> 403 Forbidden
    res_list = await client.get(
        "/api/v1/shipments",
        headers={"Authorization": f"Bearer {cust_token}"},
    )
    assert res_list.status_code == 403


async def test_admin_and_manager_can_view_and_list_shipments(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="LIST_SHIP")
    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"carrier": "DHL", "tracking_number": "DHL-LIST-1"},
    )
    shipment_id = create_res.json()["id"]

    # Admin view by shipment_id
    res1 = await client.get(f"/api/v1/shipments/{shipment_id}", headers={"Authorization": f"Bearer {admin_token}"})
    assert res1.status_code == 200

    # Admin view by order_id
    res2 = await client.get(f"/api/v1/orders/{order_id}/shipments", headers={"Authorization": f"Bearer {admin_token}"})
    assert res2.status_code == 200

    # Admin list all shipments
    list_res = await client.get("/api/v1/shipments", headers={"Authorization": f"Bearer {admin_token}"})
    assert list_res.status_code == 200
    assert any(s["id"] == shipment_id for s in list_res.json())


async def test_pagination_and_status_filtering(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Create 3 shipments
    shipment_ids: list[int] = []
    for i in range(3):
        order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix=f"PAG_{i}")
        res = await client.post(
            f"/api/v1/orders/{order_id}/shipments",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )
        shipment_ids.append(res.json()["id"])

    # Update one to SHIPPED
    await client.patch(
        f"/api/v1/shipments/{shipment_ids[0]}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "FedEx", "tracking_number": "FEDEX-PAG-0"},
    )

    # Filter by SHIPPED
    res_shipped = await client.get(
        "/api/v1/shipments?status=SHIPPED",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_shipped.status_code == 200
    assert all(s["status"] == "SHIPPED" for s in res_shipped.json())
    assert any(s["id"] == shipment_ids[0] for s in res_shipped.json())

    # Filter by PENDING
    res_pending = await client.get(
        "/api/v1/shipments?status=PENDING",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_pending.status_code == 200
    assert all(s["status"] == "PENDING" for s in res_pending.json())

    # Pagination: offset and limit
    res_page = await client.get(
        "/api/v1/shipments?offset=0&limit=1",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_page.status_code == 200
    assert len(res_page.json()) == 1


async def test_concurrent_status_transition_race(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """Test concurrent status transitions (e.g. SHIPPED vs CANCELLED) on the same shipment."""
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    for rep in range(3):
        order_id, _, _ = await create_confirmed_paid_order(
            client, admin_token, cust_token, sku_prefix=f"CONC_STAT_{rep}"
        )
        create_res = await client.post(
            f"/api/v1/orders/{order_id}/shipments",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )
        shipment_id = create_res.json()["id"]

        async def ship() -> int:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/api/v1/shipments/{shipment_id}/status",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"status": "SHIPPED", "carrier": "CarrierA", "tracking_number": f"TRK_A_{rep}_{uuid.uuid4().hex[:6]}"},
                )
                return r.status_code

        async def cancel() -> int:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch(
                    f"/api/v1/shipments/{shipment_id}/status",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"status": "CANCELLED"},
                )
                return r.status_code

        # Compete SHIPPED vs CANCELLED concurrently
        r1, r2 = await asyncio.gather(ship(), cancel())

        # One must succeed (200), and the losing transition must fail with 409
        status_codes = [r1, r2]
        assert 200 in status_codes
        assert 409 in status_codes

        # Final state must be consistent
        async with TestSessionLocal() as session:
            s = (await session.execute(select(Shipment).where(Shipment.id == shipment_id))).scalar_one()
            assert s.status in (ShipmentStatus.SHIPPED, ShipmentStatus.CANCELLED)


async def test_duplicate_carrier_tracking_conflict(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_1, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="DUP_TRK_1")
    order_2, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="DUP_TRK_2")

    # Shipment 1
    create1 = await client.post(
        f"/api/v1/orders/{order_1}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"carrier": "DHL", "tracking_number": "SHARED_TRACK_123"},
    )
    assert create1.status_code == 201

    # Shipment 2 tries to create with SAME carrier and tracking_number -> 409 Conflict
    create2 = await client.post(
        f"/api/v1/orders/{order_2}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"carrier": "DHL", "tracking_number": "SHARED_TRACK_123"},
    )
    assert create2.status_code == 409
    assert "already assigned" in create2.json()["detail"].lower()

    # Shipment 2 created without tracking, then tries to update to SAME carrier and tracking_number -> 409 Conflict
    create2_empty = await client.post(
        f"/api/v1/orders/{order_2}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert create2_empty.status_code == 201
    shipment_2_id = create2_empty.json()["id"]

    update2 = await client.patch(
        f"/api/v1/shipments/{shipment_2_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "DHL", "tracking_number": "SHARED_TRACK_123"},
    )
    assert update2.status_code == 409
    assert "already assigned" in update2.json()["detail"].lower()

    # Different carrier with same tracking number is ALLOWED (scoped to carrier)
    update_diff_carrier = await client.patch(
        f"/api/v1/shipments/{shipment_2_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "FedEx", "tracking_number": "SHARED_TRACK_123"},
    )
    assert update_diff_carrier.status_code == 200


async def test_rollback_on_forced_failure(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="ROLLBACK_SHIP")

    # Mock db.commit to raise an exception during shipment creation
    from app.services.shipment import ShipmentService

    original_create = ShipmentService.create_shipment

    async def faulty_create(self: ShipmentService, *args, **kwargs):
        # Call original logic but raise before commit
        await self._get_authorized_order(kwargs["order_id"], kwargs["user"], for_update=True)
        raise RuntimeError("Simulated crash in shipment service")

    monkeypatch.setattr(ShipmentService, "create_shipment", faulty_create)

    with pytest.raises(RuntimeError, match="Simulated crash"):
        await client.post(
            f"/api/v1/orders/{order_id}/shipments",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )

    # In fresh DB session, ensure no shipment record exists
    async with TestSessionLocal() as session:
        shipment = (
            await session.execute(
                select(Shipment).where(Shipment.order_id == order_id)
            )
        ).scalar_one_or_none()
        assert shipment is None


async def test_existing_inventory_quantities_remain_unchanged(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, prod_id, wh_id = await create_confirmed_paid_order(client, admin_token, cust_token, sku_prefix="INV_UNCHANGED")

    # Check inventory before shipment creation
    async with TestSessionLocal() as session:
        inv_before = (
            await session.execute(
                select(Inventory).where(Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id)
            )
        ).scalar_one()
        avail_before = inv_before.available_quantity
        res_before = inv_before.reserved_quantity

    # Create Shipment
    create_res = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"carrier": "DHL", "tracking_number": "TRK-INV-1"},
    )
    assert create_res.status_code == 201
    shipment_id = create_res.json()["id"]

    # Transition to SHIPPED and DELIVERED
    await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED", "carrier": "DHL", "tracking_number": "TRK-INV-1"},
    )
    await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "DELIVERED"},
    )

    # Check inventory after all shipment operations
    async with TestSessionLocal() as session:
        inv_after = (
            await session.execute(
                select(Inventory).where(Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id)
            )
        ).scalar_one()
        assert inv_after.available_quantity == avail_before
        assert inv_after.reserved_quantity == res_before

        # Check Order status is still CONFIRMED
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert order.status == OrderStatus.CONFIRMED

        # Check Payment status is still SUCCEEDED
        pay = (await session.execute(select(Payment).where(Payment.order_id == order_id))).scalar_one()
        assert pay.status == PaymentStatus.SUCCEEDED


async def test_postgresql_constraints_direct_enforcement(
    admin_user: User,
    customer_user: User,
) -> None:
    """Direct PostgreSQL database constraint testing."""
    user_id = customer_user.id
    async with TestSessionLocal() as session:
        # Create order
        order = Order(user_id=user_id, status=OrderStatus.CONFIRMED)
        session.add(order)
        await session.flush()

        # Create shipment
        s1 = Shipment(
            order_id=order.id,
            carrier="BlueDart",
            tracking_number="BD-DIRECT-1",
            status=ShipmentStatus.PENDING,
        )
        session.add(s1)
        await session.commit()

        # 1. Test duplicate order_id unique constraint
        s_dup_order = Shipment(
            order_id=order.id,
            status=ShipmentStatus.PENDING,
        )
        session.add(s_dup_order)
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        # 2. Test duplicate (carrier, tracking_number) partial unique constraint on different order
        order2 = Order(user_id=user_id, status=OrderStatus.CONFIRMED)
        session.add(order2)
        await session.flush()

        s_dup_tracking = Shipment(
            order_id=order2.id,
            carrier="BlueDart",
            tracking_number="BD-DIRECT-1",
            status=ShipmentStatus.PENDING,
        )
        session.add(s_dup_tracking)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
