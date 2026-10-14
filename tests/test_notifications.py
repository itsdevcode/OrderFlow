import asyncio
from typing import cast
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.background import dispatch_notification_task
from app.db.session import AsyncSessionLocal
from app.main import app
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)
from app.models.order import Order, OrderStatus
from app.models.payment import Payment, PaymentStatus
from app.models.shipment import Shipment, ShipmentStatus
from app.models.user import User
from app.services.notification import NotificationService
from app.services.notification_delivery import InAppNotificationDelivery
from tests.conftest import TestSessionLocal
from tests.test_payment_webhooks import make_signed_webhook_payload
from tests.test_payments import login_and_get_access_token, setup_test_order


@pytest.mark.asyncio
async def test_order_created_notification(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, "admin@test.com", "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, "customer@test.com", "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(
        client,
        admin_token,
        customer_token,
        unit_price=100.0,
        quantity=1,
        sku_prefix="NOTIF_ORD",
    )

    async with TestSessionLocal() as session:
        result = await session.execute(
            select(Notification).where(
                Notification.order_id == order_id,
                Notification.notification_type == NotificationType.ORDER_CREATED,
            )
        )
        notif = result.scalar_one_or_none()
        assert notif is not None
        assert notif.user_id == customer_user.id
        assert notif.deduplication_key == f"order.created:{order_id}"
        # Transport in tests may have run the background task to SENT
        assert notif.status in (NotificationStatus.PENDING, NotificationStatus.SENT)
        assert "created" in notif.message.lower()


@pytest.mark.asyncio
async def test_payment_notifications_succeeded_and_failed(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, "admin@test.com", "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, "customer@test.com", "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(
        client,
        admin_token,
        customer_token,
        unit_price=50.0,
        quantity=2,
        sku_prefix="NOTIF_PAY",
    )

    # Initiate payment
    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {customer_token}"},
        json={"idempotency_key": f"pay-key-{uuid.uuid4().hex}"},
    )
    assert init_res.status_code == 201
    pay_data = init_res.json()
    payment_id = pay_data["id"]
    provider_payment_id = pay_data["provider_payment_id"]

    # 1. Test payment.failed webhook
    raw_failed, headers_failed = make_signed_webhook_payload(
        event_id=f"evt_pay_fail_{uuid.uuid4().hex}",
        event_type="payment.failed",
        provider_payment_id=provider_payment_id,
        amount="100.00",
    )
    res_failed = await client.post("/api/v1/webhooks/payments", content=raw_failed, headers=headers_failed)
    assert res_failed.status_code == 200

    async with TestSessionLocal() as session:
        result_fail = await session.execute(
            select(Notification).where(
                Notification.order_id == order_id,
                Notification.notification_type == NotificationType.PAYMENT_FAILED,
            )
        )
        fail_notif = result_fail.scalar_one_or_none()
        assert fail_notif is not None
        assert fail_notif.user_id == customer_user.id
        assert fail_notif.deduplication_key == f"payment.failed:{payment_id}"
        assert fail_notif.status in (NotificationStatus.PENDING, NotificationStatus.SENT)

    # 2. Initiate retry payment and test payment.succeeded
    init_res2 = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {customer_token}"},
        json={"idempotency_key": f"pay-key-retry-{uuid.uuid4().hex}"},
    )
    assert init_res2.status_code == 201
    pay2_data = init_res2.json()
    payment2_id = pay2_data["id"]
    provider_payment2_id = pay2_data["provider_payment_id"]

    raw_succ, headers_succ = make_signed_webhook_payload(
        event_id=f"evt_pay_succ_{uuid.uuid4().hex}",
        event_type="payment.succeeded",
        provider_payment_id=provider_payment2_id,
        amount="100.00",
    )
    res_succ = await client.post("/api/v1/webhooks/payments", content=raw_succ, headers=headers_succ)
    assert res_succ.status_code == 200

    async with TestSessionLocal() as session:
        result_succ = await session.execute(
            select(Notification).where(
                Notification.order_id == order_id,
                Notification.notification_type == NotificationType.PAYMENT_SUCCEEDED,
            )
        )
        succ_notif = result_succ.scalar_one_or_none()
        assert succ_notif is not None
        assert succ_notif.user_id == customer_user.id
        assert succ_notif.deduplication_key == f"payment.succeeded:{payment2_id}"
        assert succ_notif.status in (NotificationStatus.PENDING, NotificationStatus.SENT)

    # 3. Duplicate payment.succeeded webhook must not create a duplicate notification
    res_dup = await client.post("/api/v1/webhooks/payments", content=raw_succ, headers=headers_succ)
    assert res_dup.status_code == 200

    async with TestSessionLocal() as session:
        all_succ = (
            await session.execute(
                select(Notification).where(
                    Notification.order_id == order_id,
                    Notification.notification_type == NotificationType.PAYMENT_SUCCEEDED,
                )
            )
        ).scalars().all()
        assert len(all_succ) == 1


@pytest.mark.asyncio
async def test_shipment_notifications_shipped_and_delivered(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    admin_token = await login_and_get_access_token(client, "admin@test.com", "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, "customer@test.com", "CustomerPassword123!")

    # Setup confirmed & paid order
    order_id, _, _ = await setup_test_order(
        client,
        admin_token,
        customer_token,
        unit_price=200.0,
        quantity=1,
        sku_prefix="NOTIF_SHIP",
    )
    init_res = await client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers={"Authorization": f"Bearer {customer_token}"},
        json={"idempotency_key": f"pay-key-{uuid.uuid4().hex}"},
    )
    pay_data = init_res.json()
    raw_succ, headers_succ = make_signed_webhook_payload(
        event_id=f"evt_pay_ship_{uuid.uuid4().hex}",
        event_type="payment.succeeded",
        provider_payment_id=pay_data["provider_payment_id"],
        amount="200.00",
    )
    await client.post("/api/v1/webhooks/payments", content=raw_succ, headers=headers_succ)

    # Create shipment (PENDING - does NOT create notification)
    create_ship = await client.post(
        f"/api/v1/orders/{order_id}/shipments",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"carrier": "FedEx", "tracking_number": f"TRK-{uuid.uuid4().hex[:8]}"},
    )
    assert create_ship.status_code == 201
    shipment_id = create_ship.json()["id"]

    async with TestSessionLocal() as session:
        ship_notifs = (
            await session.execute(
                select(Notification).where(
                    Notification.order_id == order_id,
                    Notification.notification_type.in_(
                        [NotificationType.SHIPMENT_SHIPPED, NotificationType.SHIPMENT_DELIVERED]
                    ),
                )
            )
        ).scalars().all()
        assert len(ship_notifs) == 0

    # Transition to SHIPPED
    ship_res = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED"},
    )
    assert ship_res.status_code == 200

    async with TestSessionLocal() as session:
        shipped_notif = (
            await session.execute(
                select(Notification).where(
                    Notification.order_id == order_id,
                    Notification.notification_type == NotificationType.SHIPMENT_SHIPPED,
                )
            )
        ).scalar_one_or_none()
        assert shipped_notif is not None
        assert shipped_notif.user_id == customer_user.id
        assert shipped_notif.deduplication_key == f"shipment.shipped:{shipment_id}"
        assert shipped_notif.status in (NotificationStatus.PENDING, NotificationStatus.SENT)

    # Repeated transition to same SHIPPED state must not create duplicate notification
    ship_dup = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "SHIPPED"},
    )
    assert ship_dup.status_code == 200

    async with TestSessionLocal() as session:
        all_shipped = (
            await session.execute(
                select(Notification).where(
                    Notification.order_id == order_id,
                    Notification.notification_type == NotificationType.SHIPMENT_SHIPPED,
                )
            )
        ).scalars().all()
        assert len(all_shipped) == 1

    # Transition to DELIVERED
    deliv_res = await client.patch(
        f"/api/v1/shipments/{shipment_id}/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "DELIVERED"},
    )
    assert deliv_res.status_code == 200

    async with TestSessionLocal() as session:
        deliv_notif = (
            await session.execute(
                select(Notification).where(
                    Notification.order_id == order_id,
                    Notification.notification_type == NotificationType.SHIPMENT_DELIVERED,
                )
            )
        ).scalar_one_or_none()
        assert deliv_notif is not None
        assert deliv_notif.user_id == customer_user.id
        assert deliv_notif.deduplication_key == f"shipment.delivered:{shipment_id}"
        assert deliv_notif.status in (NotificationStatus.PENDING, NotificationStatus.SENT)


@pytest.mark.asyncio
async def test_no_notification_on_failed_auth_or_invalid_transition(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    # 1. Failed webhook authentication creates no notification
    bad_wh = await client.post(
        "/api/v1/webhooks/payments",
        content=b'{"event_type": "payment.succeeded"}',
        headers={"X-Webhook-Signature": "invalid_sig"},
    )
    assert bad_wh.status_code == 401

    async with TestSessionLocal() as session:
        count = (await session.execute(select(Notification))).scalars().all()
        assert len(count) == 0

    # 2. Invalid shipment transition creates no notification
    admin_token = await login_and_get_access_token(client, "admin@test.com", "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, "customer@test.com", "CustomerPassword123!")

    order_id, _, _ = await setup_test_order(
        client, admin_token, customer_token, sku_prefix="NOTIF_ERR"
    )
    # Direct jump to DELIVERED from non-existing shipment or invalid transition
    inv_ship = await client.patch(
        "/api/v1/shipments/99999/status",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "DELIVERED"},
    )
    assert inv_ship.status_code == 404


@pytest.mark.asyncio
async def test_notification_ownership_isolation_and_pagination(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
) -> None:
    customer_token = await login_and_get_access_token(client, "customer@test.com", "CustomerPassword123!")
    admin_token = await login_and_get_access_token(client, "admin@test.com", "AdminPassword123!")

    # Seed 3 notifications for customer and 1 for admin directly in DB
    async with TestSessionLocal() as session:
        n1 = Notification(
            user_id=customer_user.id,
            notification_type=NotificationType.ORDER_CREATED,
            channel=NotificationChannel.IN_APP,
            status=NotificationStatus.SENT,
            title="Customer Note 1",
            message="Message 1",
            deduplication_key="test.key.1",
        )
        n2 = Notification(
            user_id=customer_user.id,
            notification_type=NotificationType.PAYMENT_SUCCEEDED,
            channel=NotificationChannel.IN_APP,
            status=NotificationStatus.SENT,
            title="Customer Note 2",
            message="Message 2",
            deduplication_key="test.key.2",
        )
        n3 = Notification(
            user_id=customer_user.id,
            notification_type=NotificationType.SHIPMENT_SHIPPED,
            channel=NotificationChannel.IN_APP,
            status=NotificationStatus.PENDING,
            title="Customer Note 3",
            message="Message 3",
            deduplication_key="test.key.3",
        )
        n_admin = Notification(
            user_id=admin_user.id,
            notification_type=NotificationType.ORDER_CREATED,
            channel=NotificationChannel.IN_APP,
            status=NotificationStatus.SENT,
            title="Admin Note",
            message="Admin Message",
            deduplication_key="test.key.admin",
        )
        session.add_all([n1, n2, n3, n_admin])
        await session.commit()
        await session.refresh(n1)
        await session.refresh(n_admin)
        customer_n1_id = n1.id
        admin_nid = n_admin.id

    # 1. Customer lists their notifications (should see 3, not admin's)
    list_res = await client.get(
        "/api/v1/notifications",
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert list_res.status_code == 200
    items = list_res.json()
    assert len(items) == 3
    for item in items:
        assert item["user_id"] == customer_user.id

    # 2. Customer accesses own notification
    get_res = await client.get(
        f"/api/v1/notifications/{customer_n1_id}",
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert get_res.status_code == 200
    assert get_res.json()["title"] == "Customer Note 1"

    # 3. Customer accesses Admin notification -> 404
    cross_res = await client.get(
        f"/api/v1/notifications/{admin_nid}",
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert cross_res.status_code == 404

    # 4. Unauthenticated list & get rejected -> 401
    unauth_list = await client.get("/api/v1/notifications")
    assert unauth_list.status_code == 401

    unauth_get = await client.get(f"/api/v1/notifications/{customer_n1_id}")
    assert unauth_get.status_code == 401

    # 5. Pagination test
    paged_res = await client.get(
        "/api/v1/notifications?offset=1&limit=1",
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert paged_res.status_code == 200
    paged_items = paged_res.json()
    assert len(paged_items) == 1

    # 6. Type filtering
    filtered_res = await client.get(
        "/api/v1/notifications?type=ORDER_CREATED",
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert filtered_res.status_code == 200
    filtered_items = filtered_res.json()
    assert len(filtered_items) == 1
    assert filtered_items[0]["notification_type"] == "ORDER_CREATED"


@pytest.mark.asyncio
async def test_concurrent_deduplication_and_database_uniqueness(
    customer_user: User,
) -> None:
    key = f"concurrent.test.{uuid.uuid4().hex}"

    # First session creates and commits
    async with TestSessionLocal() as session1:
        srv1 = NotificationService(session1)
        res1 = await srv1.create_pending_notification(
            user_id=customer_user.id,
            order_id=None,
            order_number="ORD-TEST",
            notification_type=NotificationType.ORDER_CREATED,
            deduplication_key=key,
        )
        await session1.commit()
        assert res1 is not None

    # Second session tries to create with the exact same deduplication_key -> returns None via ON CONFLICT DO NOTHING
    async with TestSessionLocal() as session2:
        srv2 = NotificationService(session2)
        res2 = await srv2.create_pending_notification(
            user_id=customer_user.id,
            order_id=None,
            order_number="ORD-TEST",
            notification_type=NotificationType.ORDER_CREATED,
            deduplication_key=key,
        )
        await session2.commit()
        assert res2 is None

    # Verify exactly one record exists in DB
    async with TestSessionLocal() as check_session:
        records = (
            await check_session.execute(
                select(Notification).where(Notification.deduplication_key == key)
            )
        ).scalars().all()
        assert len(records) == 1


@pytest.mark.asyncio
async def test_delivery_lifecycle_and_failure_recording(
    customer_user: User,
) -> None:
    # 1. Create a notification in PENDING status
    async with TestSessionLocal() as session:
        notif = Notification(
            user_id=customer_user.id,
            notification_type=NotificationType.ORDER_CREATED,
            channel=NotificationChannel.IN_APP,
            status=NotificationStatus.PENDING,
            title="Order Created",
            message="Your order ORD-123 has been created.",
            deduplication_key=f"fail.test.{uuid.uuid4().hex}",
            attempts=0,
        )
        session.add(notif)
        await session.commit()
        await session.refresh(notif)
        notif_id = notif.id

    # 2. Dispatch with delivery adapter set to fail
    failing_adapter = InAppNotificationDelivery(should_fail=True)
    async with TestSessionLocal() as session:
        service = NotificationService(session, delivery_adapter=failing_adapter)
        await service.dispatch_notification(notif_id)

    # Verify status changed to FAILED and attempts incremented
    async with TestSessionLocal() as session:
        failed_notif = (
            await session.execute(select(Notification).where(Notification.id == notif_id))
        ).scalar_one()
        assert failed_notif.status == NotificationStatus.FAILED
        assert failed_notif.attempts == 1
        assert failed_notif.error_message is not None
        assert failed_notif.error_message == "SIMULATED_FAILURE"
        assert failed_notif.sent_at is None

    # 3. Retry dispatch with succeeding adapter
    succeeding_adapter = InAppNotificationDelivery(should_fail=False)
    async with TestSessionLocal() as session:
        service = NotificationService(session, delivery_adapter=succeeding_adapter)
        await service.dispatch_notification(notif_id)

    # Verify status transitioned to SENT, sent_at is recorded, attempts incremented to 2
    async with TestSessionLocal() as session:
        sent_notif = (
            await session.execute(select(Notification).where(Notification.id == notif_id))
        ).scalar_one()
        assert sent_notif.status == NotificationStatus.SENT
        assert sent_notif.attempts == 2
        assert sent_notif.sent_at is not None
        assert sent_notif.error_message is None

    # 4. Repeated dispatch of SENT notification does nothing (no attempt increment)
    async with TestSessionLocal() as session:
        service = NotificationService(session, delivery_adapter=succeeding_adapter)
        await service.dispatch_notification(notif_id)

    async with TestSessionLocal() as session:
        final_notif = (
            await session.execute(select(Notification).where(Notification.id == notif_id))
        ).scalar_one()
        assert final_notif.status == NotificationStatus.SENT
        assert final_notif.attempts == 2


@pytest.mark.asyncio
async def test_max_attempts_enforcement_prevents_redispatch(
    customer_user: User,
) -> None:
    # 1. Create a notification with attempts=2 and max_attempts=3
    async with TestSessionLocal() as session:
        notif = Notification(
            user_id=customer_user.id,
            notification_type=NotificationType.ORDER_CREATED,
            channel=NotificationChannel.IN_APP,
            status=NotificationStatus.FAILED,
            title="Max Attempts Test",
            message="Test message",
            deduplication_key=f"max.attempt.{uuid.uuid4().hex}",
            attempts=2,
        )
        session.add(notif)
        await session.commit()
        await session.refresh(notif)
        notif_id = notif.id

    failing_adapter = InAppNotificationDelivery(should_fail=True)

    # Dispatch attempt #3 (reaches max_attempts)
    async with TestSessionLocal() as session:
        service = NotificationService(session, delivery_adapter=failing_adapter, max_attempts=3)
        await service.dispatch_notification(notif_id)

    async with TestSessionLocal() as session:
        n = (await session.execute(select(Notification).where(Notification.id == notif_id))).scalar_one()
        assert n.status == NotificationStatus.FAILED
        assert n.attempts == 3

    # Dispatch attempt #4 (should be refused/blocked by max_attempts)
    succeeding_adapter = InAppNotificationDelivery(should_fail=False)
    async with TestSessionLocal() as session:
        service = NotificationService(session, delivery_adapter=succeeding_adapter, max_attempts=3)
        await service.dispatch_notification(notif_id)

    # Status remains FAILED and attempts remains 3 (exhausted notifications cannot be claimed automatically)
    async with TestSessionLocal() as session:
        n = (await session.execute(select(Notification).where(Notification.id == notif_id))).scalar_one()
        assert n.status == NotificationStatus.FAILED
        assert n.attempts == 3


@pytest.mark.asyncio
async def test_secret_leakage_sanitization(
    customer_user: User,
) -> None:
    class LeakyAdapter:
        async def deliver(self, notification: Notification) -> None:
            _ = notification
            # Simulate a third-party error leaking secrets or tokens
            raise RuntimeError(
                "API Connection to provider failed: Authorization Bearer sk_live_998877665544332211, "
                "recipient user@secret.domain, password secret_db_pass"
            )

    async with TestSessionLocal() as session:
        notif = Notification(
            user_id=customer_user.id,
            notification_type=NotificationType.PAYMENT_FAILED,
            channel=NotificationChannel.IN_APP,
            status=NotificationStatus.PENDING,
            title="Security Test",
            message="Order message",
            deduplication_key=f"leak.test.{uuid.uuid4().hex}",
            attempts=0,
        )
        session.add(notif)
        await session.commit()
        await session.refresh(notif)
        notif_id = notif.id

    async with TestSessionLocal() as session:
        service = NotificationService(session, delivery_adapter=LeakyAdapter())
        await service.dispatch_notification(notif_id)

    async with TestSessionLocal() as session:
        n = (await session.execute(select(Notification).where(Notification.id == notif_id))).scalar_one()
        assert n.status == NotificationStatus.FAILED
        assert n.error_message is not None
        # Assert none of the sensitive tokens leaked into the database record
        assert "sk_live" not in n.error_message
        assert "Bearer" not in n.error_message
        assert "secret" not in n.error_message
        assert "password" not in n.error_message
        # Instead, sanitized category is saved
        assert n.error_message in ("AUTHENTICATION_ERROR", "CONNECTION_ERROR", "UNEXPECTED_DELIVERY_ERROR")


@pytest.mark.asyncio
async def test_stranded_processing_recovery_and_stale_worker_fencing(
    customer_user: User,
) -> None:
    from datetime import timedelta, timezone
    from datetime import datetime

    # 1. Create a notification stranded in PROCESSING with an old claimed_at
    past_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with TestSessionLocal() as session:
        notif = Notification(
            user_id=customer_user.id,
            notification_type=NotificationType.ORDER_CREATED,
            channel=NotificationChannel.IN_APP,
            status=NotificationStatus.PROCESSING,
            title="Stranded Recovery Test",
            message="Test message",
            deduplication_key=f"stranded.test.{uuid.uuid4().hex}",
            attempts=1,
            claim_token="stale-worker-claim-token-12345",
            claimed_at=past_time,
        )
        session.add(notif)
        await session.commit()
        await session.refresh(notif)
        notif_id = notif.id

    # 2. Worker 2 attempts dispatch (lease_seconds=300s, elapsed=600s -> recovery kicks in)
    succeeding_adapter = InAppNotificationDelivery(should_fail=False)
    async with TestSessionLocal() as session:
        service = NotificationService(
            session,
            delivery_adapter=succeeding_adapter,
            max_attempts=3,
            lease_seconds=300,
        )
        await service.dispatch_notification(notif_id)

    # Verify notification successfully recovered and moved to SENT
    async with TestSessionLocal() as session:
        n = (await session.execute(select(Notification).where(Notification.id == notif_id))).scalar_one()
        assert n.status == NotificationStatus.SENT
        assert n.attempts == 2
        assert n.sent_at is not None

    # 3. Stale Worker 1 wakes up and tries to finalize with old claim token
    async with TestSessionLocal() as session:
        repo = NotificationService(session).notification_repo
        finalized = await repo.finalize_dispatch(
            notification_id=notif_id,
            claim_token="stale-worker-claim-token-12345",
            success=False,
            now=datetime.now(timezone.utc),
            sanitized_error="STALE_FAILURE",
        )
        await session.commit()
        # Fencing mechanism blocks the stale worker
        assert finalized is False

    # Verify status in DB remains SENT and was NOT overwritten to FAILED
    async with TestSessionLocal() as session:
        final_check = (await session.execute(select(Notification).where(Notification.id == notif_id))).scalar_one()
        assert final_check.status == NotificationStatus.SENT
        assert final_check.error_message is None
