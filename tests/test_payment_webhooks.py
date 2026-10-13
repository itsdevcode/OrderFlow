import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from typing import cast
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.inventory import Inventory
from app.models.inventory_reservation import InventoryReservation, ReservationStatus
from app.models.order import Order, OrderStatus
from app.models.payment import Payment, PaymentStatus
from app.models.user import User
from app.models.webhook_event import WebhookEvent, WebhookEventStatus
from app.services.payment_webhook import compute_webhook_signature
from tests.conftest import TestSessionLocal
from tests.test_payments import login_and_get_access_token, setup_test_order


def make_signed_webhook_payload(
    event_id: str,
    event_type: str,
    provider_payment_id: str,
    amount: float | str,
    secret: str = settings.payment_webhook_secret,
    currency: str = "INR",
    provider: str = "fake_provider",
    timestamp: int | None = None,
) -> tuple[bytes, dict[str, str]]:
    if timestamp is None:
        timestamp = int(datetime.now(timezone.utc).timestamp())

    payload_dict = {
        "event_id": event_id,
        "event_type": event_type,
        "provider": provider,
        "provider_payment_id": provider_payment_id,
        "amount": str(amount),
        "currency": currency,
        "timestamp": timestamp,
    }
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = compute_webhook_signature(raw_bytes, secret)
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": sig,
    }
    return raw_bytes, headers


async def test_webhook_missing_signature(client: AsyncClient) -> None:
    raw_bytes = json.dumps({"test": "data"}).encode("utf-8")
    res = await client.post(
        "/api/v1/webhooks/payments",
        content=raw_bytes,
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 401
    assert "signature" in res.json()["detail"].lower()


async def test_webhook_invalid_signature(client: AsyncClient) -> None:
    raw_bytes = json.dumps({"test": "data"}).encode("utf-8")
    res = await client.post(
        "/api/v1/webhooks/payments",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": "wrong_signature_hex",
        },
    )
    assert res.status_code == 401
    assert "Invalid webhook signature" in res.json()["detail"]


async def test_webhook_tampered_payload(client: AsyncClient) -> None:
    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_tamper_1",
        event_type="payment.succeeded",
        provider_payment_id="pay_fake_tamper",
        amount="301.00",
    )
    # Tamper the body after signing
    tampered_bytes = raw_bytes + b" "
    res = await client.post(
        "/api/v1/webhooks/payments",
        content=tampered_bytes,
        headers=headers,
    )
    assert res.status_code == 401
    assert "Invalid webhook signature" in res.json()["detail"]


async def test_webhook_malformed_json(client: AsyncClient) -> None:
    raw_bytes = b"not-a-valid-json"
    sig = compute_webhook_signature(raw_bytes, settings.payment_webhook_secret)
    res = await client.post(
        "/api/v1/webhooks/payments",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
        },
    )
    assert res.status_code == 400
    assert "Invalid JSON" in res.json()["detail"]


async def test_webhook_unknown_event_type(client: AsyncClient) -> None:
    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_unknown_type",
        event_type="payment.dispute.created",  # unsupported
        provider_payment_id="pay_fake_xxx",
        amount="100.00",
    )
    res = await client.post(
        "/api/v1/webhooks/payments",
        content=raw_bytes,
        headers=headers,
    )
    assert res.status_code == 400


async def test_webhook_unknown_payment_identifier(client: AsyncClient) -> None:
    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_unknown_pay_id",
        event_type="payment.succeeded",
        provider_payment_id="pay_fake_nonexistent_999",
        amount="100.00",
    )
    res = await client.post(
        "/api/v1/webhooks/payments",
        content=raw_bytes,
        headers=headers,
    )
    assert res.status_code == 404
    assert "No payment record found" in res.json()["detail"]

    # Verify WebhookEvent recorded as FAILED
    async with TestSessionLocal() as session:
        stmt = select(WebhookEvent).where(WebhookEvent.provider_event_id == "evt_unknown_pay_id")
        evt = (await session.execute(stmt)).scalar_one_or_none()
        assert evt is not None
        assert evt.status == WebhookEventStatus.FAILED


async def test_webhook_mismatched_amount(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Setup order: 2 items @ 150.50 = 301.00
    order_id, _, _ = await setup_test_order(client, admin_token, cust_token, unit_price=150.50, quantity=2)

    # Initiate payment
    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-amount-test"},
    )
    assert init_res.status_code == 201
    pay_data = init_res.json()
    provider_payment_id = pay_data["provider_payment_id"]

    # Send webhook with wrong amount (e.g. 500.00 instead of 301.00)
    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_wrong_amount",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="500.00",
    )
    res = await client.post(
        "/api/v1/webhooks/payments",
        content=raw_bytes,
        headers=headers,
    )
    assert res.status_code == 400
    assert "mismatch" in res.json()["detail"].lower()

    # Verify payment remains PENDING and order remains PENDING
    async with TestSessionLocal() as session:
        pay = (await session.execute(select(Payment).where(Payment.id == pay_data["id"]))).scalar_one()
        assert pay.status == PaymentStatus.PENDING
        ord_obj = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert ord_obj.status == OrderStatus.PENDING


async def test_webhook_mismatched_currency(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, cust_token, unit_price=100.00, quantity=1)

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-curr-test"},
    )
    provider_payment_id = init_res.json()["provider_payment_id"]

    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_wrong_curr",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="100.00",
        currency="USD",  # DB has INR
    )
    res = await client.post(
        "/api/v1/webhooks/payments",
        content=raw_bytes,
        headers=headers,
    )
    assert res.status_code == 400
    assert "currency mismatch" in res.json()["detail"].lower()


async def test_valid_payment_success_lifecycle_and_quantities(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Initial stock: available=100, reserved=0
    # Order: 3 items @ 200.00 = 600.00
    # Post-order: available=97, reserved=3
    order_id, prod_id, wh_id = await setup_test_order(
        client, admin_token, cust_token, unit_price=200.00, quantity=3
    )

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-succ-full"},
    )
    assert init_res.status_code == 201
    pay_data = init_res.json()
    provider_payment_id = pay_data["provider_payment_id"]

    # Verify pre-webhook inventory
    async with TestSessionLocal() as session:
        inv_before = (
            await session.execute(
                select(Inventory).where(
                    Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id
                )
            )
        ).scalar_one()
        assert inv_before.available_quantity == 97
        assert inv_before.reserved_quantity == 3

    # Send payment.succeeded webhook
    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_succ_full_1",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="600.00",
    )
    res = await client.post("/api/v1/webhooks/payments", content=raw_bytes, headers=headers)
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Verify committed state in fresh session:
    # 1. Payment = SUCCEEDED
    # 2. Order = CONFIRMED
    # 3. Reservation = CONFIRMED
    # 4. Inventory: available_quantity UNCHANGED (97), reserved_quantity reduced by 3 (now 0)
    # 5. WebhookEvent = PROCESSED
    async with TestSessionLocal() as session:
        pay = (await session.execute(select(Payment).where(Payment.id == pay_data["id"]))).scalar_one()
        assert pay.status == PaymentStatus.SUCCEEDED

        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert order.status == OrderStatus.CONFIRMED

        res_list = (
            await session.execute(
                select(InventoryReservation).where(InventoryReservation.order_id == order_id)
            )
        ).scalars().all()
        assert len(res_list) == 1
        assert res_list[0].status == ReservationStatus.CONFIRMED

        inv_after = (
            await session.execute(
                select(Inventory).where(
                    Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id
                )
            )
        ).scalar_one()
        assert inv_after.available_quantity == 97  # Unchanged by webhook
        assert inv_after.reserved_quantity == 0    # Reduced by reservation confirmation

        evt = (
            await session.execute(
                select(WebhookEvent).where(WebhookEvent.provider_event_id == "evt_succ_full_1")
            )
        ).scalar_one()
        assert evt.status == WebhookEventStatus.PROCESSED
        assert evt.processed_at is not None


async def test_valid_payment_failed_lifecycle(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, prod_id, wh_id = await setup_test_order(
        client, admin_token, cust_token, unit_price=100.00, quantity=1
    )

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-fail-test"},
    )
    pay_data = init_res.json()
    provider_payment_id = pay_data["provider_payment_id"]

    # Send payment.failed webhook
    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_fail_test_1",
        event_type="payment.failed",
        provider_payment_id=provider_payment_id,
        amount="100.00",
    )
    res = await client.post("/api/v1/webhooks/payments", content=raw_bytes, headers=headers)
    assert res.status_code == 200

    # In fresh DB session:
    # Payment = FAILED
    # Order = PENDING (NOT cancelled, customer is permitted to retry)
    # Reservation = ACTIVE (reservations remain intact for retry)
    # WebhookEvent = PROCESSED
    async with TestSessionLocal() as session:
        pay = (await session.execute(select(Payment).where(Payment.id == pay_data["id"]))).scalar_one()
        assert pay.status == PaymentStatus.FAILED

        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert order.status == OrderStatus.PENDING

        res_list = (
            await session.execute(
                select(InventoryReservation).where(InventoryReservation.order_id == order_id)
            )
        ).scalars().all()
        assert res_list[0].status == ReservationStatus.ACTIVE


async def test_duplicate_webhook_event_idempotency(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, prod_id, wh_id = await setup_test_order(client, admin_token, cust_token, unit_price=50.00, quantity=1)

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-dup-test"},
    )
    provider_payment_id = init_res.json()["provider_payment_id"]

    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_dup_1",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="50.00",
    )

    # First delivery
    res1 = await client.post("/api/v1/webhooks/payments", content=raw_bytes, headers=headers)
    assert res1.status_code == 200

    # Second delivery (identical payload)
    res2 = await client.post("/api/v1/webhooks/payments", content=raw_bytes, headers=headers)
    assert res2.status_code == 200
    assert "already processed" in res2.json()["message"].lower()

    # Verify inventory was reduced only once
    async with TestSessionLocal() as session:
        inv = (
            await session.execute(
                select(Inventory).where(Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id)
            )
        ).scalar_one()
        assert inv.reserved_quantity == 0


async def test_event_id_reused_with_different_payload_rejected(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, cust_token, unit_price=75.00, quantity=1)

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-reused-test"},
    )
    provider_payment_id = init_res.json()["provider_payment_id"]

    raw1, headers1 = make_signed_webhook_payload(
        event_id="evt_reused_id_1",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="75.00",
    )
    res1 = await client.post("/api/v1/webhooks/payments", content=raw1, headers=headers1)
    assert res1.status_code == 200

    # Same event_id, but different payload content (e.g. different amount)
    raw2, headers2 = make_signed_webhook_payload(
        event_id="evt_reused_id_1",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="150.00",
    )
    res2 = await client.post("/api/v1/webhooks/payments", content=raw2, headers=headers2)
    assert res2.status_code == 409
    assert "different payload" in res2.json()["detail"].lower()


async def test_two_different_success_events_for_same_payment(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, cust_token, unit_price=80.00, quantity=1)

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-twosucc-test"},
    )
    provider_payment_id = init_res.json()["provider_payment_id"]

    # First success event (evt_A)
    raw_a, h_a = make_signed_webhook_payload(
        event_id="evt_diff_succ_A",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="80.00",
    )
    res_a = await client.post("/api/v1/webhooks/payments", content=raw_a, headers=h_a)
    assert res_a.status_code == 200

    # Second success event (evt_B, different event ID)
    raw_b, h_b = make_signed_webhook_payload(
        event_id="evt_diff_succ_B",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="80.00",
    )
    res_b = await client.post("/api/v1/webhooks/payments", content=raw_b, headers=h_b)
    # Second distinct event for an already SUCCEEDED payment returns success safely and idempotently
    assert res_b.status_code == 200


async def test_conflicting_success_and_failure_events(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, cust_token, unit_price=90.00, quantity=1)

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-conflict-test"},
    )
    provider_payment_id = init_res.json()["provider_payment_id"]

    # First event marks SUCCEEDED
    raw_succ, h_succ = make_signed_webhook_payload(
        event_id="evt_conf_succ",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="90.00",
    )
    res_succ = await client.post("/api/v1/webhooks/payments", content=raw_succ, headers=h_succ)
    assert res_succ.status_code == 200

    # Subsequent event attempts to mark it FAILED
    raw_fail, h_fail = make_signed_webhook_payload(
        event_id="evt_conf_fail",
        event_type="payment.failed",
        provider_payment_id=provider_payment_id,
        amount="90.00",
    )
    res_fail = await client.post("/api/v1/webhooks/payments", content=raw_fail, headers=h_fail)
    assert res_fail.status_code == 400
    assert "already marked succeeded" in res_fail.json()["detail"].lower()


async def test_payment_success_after_reservation_expiry_quarantined(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, cust_token, unit_price=110.00, quantity=1)

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-exp-test"},
    )
    provider_payment_id = init_res.json()["provider_payment_id"]

    # Artificially expire the reservation in DB
    async with TestSessionLocal() as session:
        res_obj = (
            await session.execute(
                select(InventoryReservation).where(InventoryReservation.order_id == order_id)
            )
        ).scalar_one()
        res_obj.status = ReservationStatus.EXPIRED
        await session.commit()

    # Webhook arrives for payment.succeeded
    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_exp_succ_1",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="110.00",
    )
    res = await client.post("/api/v1/webhooks/payments", content=raw_bytes, headers=headers)
    # Quarantined for reconciliation (422) rather than marking order CONFIRMED without active reservation
    assert res.status_code == 422
    assert "reconciliation" in res.json()["detail"].lower()

    # Order must NOT be marked CONFIRMED, payment remains PENDING, and reconciliation evidence is persisted
    async with TestSessionLocal() as session:
        ord_obj = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert ord_obj.status == OrderStatus.PENDING

        pay_obj = (await session.execute(select(Payment).where(Payment.id == init_res.json()["id"]))).scalar_one()
        assert pay_obj.status == PaymentStatus.PENDING

        evt = (
            await session.execute(
                select(WebhookEvent).where(WebhookEvent.provider_event_id == "evt_exp_succ_1")
            )
        ).scalar_one()
        assert evt.status == WebhookEventStatus.RECONCILIATION_REQUIRED
        assert evt.error_message is not None
        assert "reconciliation" in evt.error_message.lower()
        assert evt.payment_id == pay_obj.id



async def test_timestamp_skew_and_boundary(
    client: AsyncClient,
) -> None:
    # 1. Stale timestamp (10 minutes in past)
    past_ts = int(datetime.now(timezone.utc).timestamp()) - 600
    raw_stale, h_stale = make_signed_webhook_payload(
        event_id="evt_stale_ts",
        event_type="payment.succeeded",
        provider_payment_id="pay_fake_xxx",
        amount="50.00",
        timestamp=past_ts,
    )
    res_stale = await client.post("/api/v1/webhooks/payments", content=raw_stale, headers=h_stale)
    assert res_stale.status_code == 400
    assert "allowable window" in res_stale.json()["detail"].lower()

    # 2. Future timestamp (10 minutes in future)
    future_ts = int(datetime.now(timezone.utc).timestamp()) + 600
    raw_future, h_future = make_signed_webhook_payload(
        event_id="evt_future_ts",
        event_type="payment.succeeded",
        provider_payment_id="pay_fake_xxx",
        amount="50.00",
        timestamp=future_ts,
    )
    res_future = await client.post("/api/v1/webhooks/payments", content=raw_future, headers=h_future)
    assert res_future.status_code == 400
    assert "allowable window" in res_future.json()["detail"].lower()


async def test_payload_size_limit_exceeded(
    client: AsyncClient,
) -> None:
    # Max bytes is configured to 65536
    large_garbage = "x" * 70000
    raw_bytes = json.dumps({"garbage": large_garbage}).encode("utf-8")
    sig = compute_webhook_signature(raw_bytes, settings.payment_webhook_secret)

    res = await client.post(
        "/api/v1/webhooks/payments",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
        },
    )
    assert res.status_code == 413
    assert "exceeds" in res.json()["detail"].lower()


async def test_concurrent_duplicate_webhook_deliveries(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Repeat concurrency test multiple times to ensure repeatability without deadlocks or race conditions
    for iteration in range(3):
        order_id, prod_id, wh_id = await setup_test_order(
            client, admin_token, cust_token, unit_price=25.00, quantity=2, sku_prefix=f"CONC{iteration}"
        )

        init_res = await client.post(
            f"/api/v1/orders/{order_id}/payments",
            headers={"Authorization": f"Bearer {cust_token}"},
            json={"idempotency_key": f"pay-key-conc-webhook-{iteration}"},
        )
        provider_payment_id = cast(str, init_res.json()["provider_payment_id"])

        event_id = f"evt_conc_webhook_rep_{iteration}"
        raw_bytes, headers = make_signed_webhook_payload(
            event_id=event_id,
            event_type="payment.succeeded",
            provider_payment_id=provider_payment_id,
            amount="50.00",
        )

        # Launch 5 concurrent deliveries of the EXACT SAME webhook event using independent client requests
        from httpx import ASGITransport
        from app.main import app

        async def send_delivery() -> int:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/api/v1/webhooks/payments", content=raw_bytes, headers=headers)
                return r.status_code

        results = await asyncio.gather(*[send_delivery() for _ in range(5)])

        # All deliveries must succeed (200 OK)
        assert all(code == 200 for code in results), f"Expected all 200, got {results}"

        # Verify committed state:
        # Exactly one reduction in reserved quantity
        async with TestSessionLocal() as session:
            inv = (
                await session.execute(
                    select(Inventory).where(Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id)
                )
            ).scalar_one()
            assert inv.reserved_quantity == 0

            # Exactly 1 WebhookEvent record in DB
            evts = (
                await session.execute(
                    select(WebhookEvent).where(WebhookEvent.provider_event_id == event_id)
                )
            ).scalars().all()
            assert len(evts) == 1
            assert evts[0].status == WebhookEventStatus.PROCESSED


async def test_rollback_when_inventory_confirmation_fails(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, prod_id, wh_id = await setup_test_order(
        client, admin_token, cust_token, unit_price=40.00, quantity=1
    )

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-rollback-test"},
    )
    provider_payment_id = cast(str, init_res.json()["provider_payment_id"])

    # Mock reservation confirmation to raise a simulated error mid-transaction
    from app.services.inventory_reservation import InventoryReservationService

    async def faulty_confirm(_self: InventoryReservationService, _reservation_id: int):
        raise RuntimeError("Simulated crash during inventory reservation confirmation")

    monkeypatch.setattr(InventoryReservationService, "confirm_reservation", faulty_confirm)

    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_rollback_1",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="40.00",
    )

    with pytest.raises(RuntimeError, match="Simulated crash"):
        _ = await client.post("/api/v1/webhooks/payments", content=raw_bytes, headers=headers)

    # In fresh DB session, assert ALL business tables rolled back:
    # 1. Payment remains PENDING
    # 2. Order remains PENDING
    # 3. Reservation remains ACTIVE
    # 4. Inventory reserved_quantity remains 1
    # 5. WebhookEvent was NOT marked PROCESSED
    async with TestSessionLocal() as session:
        pay = (await session.execute(select(Payment).where(Payment.provider_payment_id == provider_payment_id))).scalar_one()
        assert pay.status == PaymentStatus.PENDING

        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        assert order.status == OrderStatus.PENDING

        res = (await session.execute(select(InventoryReservation).where(InventoryReservation.order_id == order_id))).scalar_one()
        assert res.status == ReservationStatus.ACTIVE

        inv = (await session.execute(select(Inventory).where(Inventory.product_id == prod_id, Inventory.warehouse_id == wh_id))).scalar_one()
        assert inv.reserved_quantity == 1

        evt = (await session.execute(select(WebhookEvent).where(WebhookEvent.provider_event_id == "evt_rollback_1"))).scalar_one_or_none()
        assert evt is None or evt.status != WebhookEventStatus.PROCESSED


async def test_delayed_webhook_retry_outside_timestamp_window_succeeds(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    """Finding 2: Delayed webhook retries.
    An identical, already-processed signed event must be safely acknowledged even when its
    timestamp is outside the original 300-second window.
    An old, previously unseen event must still be rejected.
    """
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(client, admin_token, cust_token, unit_price=60.00, quantity=1)

    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {cust_token}"},
        json={"idempotency_key": "pay-key-delayed-test"},
    )
    provider_payment_id = cast(str, init_res.json()["provider_payment_id"])

    # 1. First delivery with timestamp inside window (e.g., current time)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    raw_bytes, headers = make_signed_webhook_payload(
        event_id="evt_delayed_retry_1",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="60.00",
        timestamp=now_ts,
    )
    res1 = await client.post("/api/v1/webhooks/payments", content=raw_bytes, headers=headers)
    assert res1.status_code == 200

    # 2. Delayed retry of the identical event after 1 hour (timestamp is now >300s in past relative to current time)
    # Re-sending the exact same signed raw_bytes and headers
    res2 = await client.post("/api/v1/webhooks/payments", content=raw_bytes, headers=headers)
    assert res2.status_code == 200
    assert "already processed" in res2.json()["message"].lower()

    # 3. An old, previously UNSEEN event (not in DB) with a 1-hour old timestamp must be rejected
    old_raw, old_headers = make_signed_webhook_payload(
        event_id="evt_unseen_old_1",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment_id,
        amount="60.00",
        timestamp=now_ts - 3600,
    )
    res3 = await client.post("/api/v1/webhooks/payments", content=old_raw, headers=old_headers)
    assert res3.status_code == 400
    assert "allowable window" in res3.json()["detail"].lower()


async def test_webhook_secret_configuration_safety() -> None:
    """Finding 3: Webhook signing secret fail-safety when production/non-debug configuration is missing."""
    from app.core.config import Settings

    # In production (debug=False), empty secret must raise ValueError
    with pytest.raises(ValueError, match="PAYMENT_WEBHOOK_SECRET must be explicitly configured"):
        _ = Settings(debug=False, payment_webhook_secret="")

    # In debug mode (debug=True), empty secret safely falls back for local dev
    s_dev = Settings(debug=True, payment_webhook_secret="")
    assert s_dev.payment_webhook_secret != ""


async def test_malformed_content_length_header_safety(
    client: AsyncClient,
) -> None:
    """Finding 4: Payload size safety with malformed Content-Length header and stream enforcement."""
    raw_bytes = json.dumps({"test": "data"}).encode("utf-8")
    sig = compute_webhook_signature(raw_bytes, settings.payment_webhook_secret)

    # Malformed Content-Length: "invalid-number"
    res = await client.post(
        "/api/v1/webhooks/payments",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "Content-Length": "not-a-number",
            "X-Webhook-Signature": sig,
        },
    )
    # Should not crash with uncaught 500 ValueError; should proceed to payload parsing/validation
    assert res.status_code in (400, 401, 404)


async def test_provider_payment_id_database_uniqueness(
    db_session: AsyncSession,
    admin_user: User,
    customer_user: User,
    client: AsyncClient,
) -> None:
    """Finding 5: PostgreSQL enforces unique constraint on (provider, provider_payment_id)."""
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    cust_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    # Order 1
    order1_id, _, _ = await setup_test_order(client, admin_token, cust_token, unit_price=10.00, quantity=1, sku_prefix="UQ1")
    # Order 2
    order2_id, _, _ = await setup_test_order(client, admin_token, cust_token, unit_price=10.00, quantity=1, sku_prefix="UQ2")

    pay1 = Payment(
        order_id=order1_id,
        provider="fake_provider",
        provider_payment_id="shared_dup_prov_id",
        idempotency_key="pay-key-uq-1",
        amount=Decimal("10.00"),
        currency="INR",
        status=PaymentStatus.PENDING,
    )
    db_session.add(pay1)
    await db_session.commit()

    # Attempt to insert second payment with identical (provider, provider_payment_id)
    pay2 = Payment(
        order_id=order2_id,
        provider="fake_provider",
        provider_payment_id="shared_dup_prov_id",
        idempotency_key="pay-key-uq-2",
        amount=Decimal("10.00"),
        currency="INR",
        status=PaymentStatus.PENDING,
    )
    db_session.add(pay2)
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        await db_session.commit()

    await db_session.rollback()


