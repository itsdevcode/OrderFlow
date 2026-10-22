"""Real PostgreSQL commit failures and persisted cross-module invariants."""
import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.audit_log import AuditAction, AuditLog
from app.models.cart_item import CartItem
from app.models.inventory import Inventory
from app.models.inventory_reservation import InventoryReservation, ReservationStatus
from app.models.notification import Notification, NotificationStatus, NotificationType
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.models.payment import Payment, PaymentStatus
from app.models.webhook_event import WebhookEvent
from app.services.inventory_reservation import InventoryReservationService
from tests.conftest import TestSessionLocal
from tests.test_audit_logs import prepared_order
from tests.test_payment_webhooks import make_signed_webhook_payload


async def reject_commit_on(table):
    # A deferred constraint trigger runs at COMMIT, after every service flush,
    # cart deletion, notification insert and audit insert has already succeeded.
    assert table in {"orders", "webhook_events"}
    async with TestSessionLocal() as db:
        await db.execute(text("""
            CREATE FUNCTION test_reject_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'test commit rejection' USING ERRCODE = '23514';
            END $$
        """))
        await db.execute(text(f"""
            CREATE CONSTRAINT TRIGGER test_commit_failure
            AFTER INSERT ON {table} DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION test_reject_commit()
        """))
        await db.commit()


async def remove_commit_rejection(table):
    assert table in {"orders", "webhook_events"}
    async with TestSessionLocal() as db:
        await db.execute(text(f"DROP TRIGGER IF EXISTS test_commit_failure ON {table}"))
        await db.execute(text("DROP FUNCTION IF EXISTS test_reject_commit()"))
        await db.commit()


async def test_order_commit_failure_preserves_cart_and_all_business_state(client, admin_user, customer_user):
    order_id, product_id, _, _, token = await prepared_order(client, admin_user, customer_user)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/v1/cart/items", headers=headers,
                             json={"product_id": product_id, "quantity": 2})).status_code == 200
    await reject_commit_on("orders")
    try:
        with pytest.raises(IntegrityError, match="test commit rejection"):
            await client.post("/api/v1/orders", headers=headers)
        async with TestSessionLocal() as db:
            assert list(await db.scalars(select(Order.id))) == [order_id]
            assert len(list(await db.scalars(select(OrderItem)))) == 1
            assert len(list(await db.scalars(select(InventoryReservation)))) == 1
            assert len(list(await db.scalars(select(Notification)))) == 1
            assert len(list(await db.scalars(select(AuditLog).where(
                AuditLog.action == AuditAction.ORDER_CREATED.value)))) == 1
            stock = (await db.scalars(select(Inventory))).one()
            assert (stock.available_quantity, stock.reserved_quantity) == (99, 1)
            assert list(await db.scalars(select(CartItem.quantity))) == [2]
    finally:
        await remove_commit_rejection("orders")
    assert (await client.post("/api/v1/orders", headers=headers)).status_code == 201
    async with TestSessionLocal() as db:
        assert list(await db.scalars(select(CartItem))) == []
        stock = (await db.scalars(select(Inventory))).one()
        assert (stock.available_quantity, stock.reserved_quantity) == (97, 3)


async def test_webhook_commit_failure_then_concurrent_retry_has_one_business_effect(client, admin_user, customer_user):
    order_id, _, _, _, token = await prepared_order(client, admin_user, customer_user)
    response = await client.post(f"/api/v1/orders/{order_id}/payments",
                                 headers={"Authorization": f"Bearer {token}"},
                                 json={"idempotency_key": "commit-retry"})
    assert response.status_code == 201
    payment = response.json()
    raw, headers = make_signed_webhook_payload("commit-retry-event", "payment.succeeded",
                                               payment["provider_payment_id"], "100.00")
    await reject_commit_on("webhook_events")
    try:
        with pytest.raises(IntegrityError, match="test commit rejection"):
            await client.post("/api/v1/webhooks/payments", content=raw, headers=headers)
        async with TestSessionLocal() as db:
            assert (await db.get(Payment, payment["id"])).status == PaymentStatus.PENDING
            assert (await db.get(Order, order_id)).status == OrderStatus.PENDING
            assert (await db.scalars(select(InventoryReservation.status))).one() == ReservationStatus.ACTIVE
            stock = (await db.scalars(select(Inventory))).one()
            assert (stock.available_quantity, stock.reserved_quantity) == (99, 1)
            assert list(await db.scalars(select(WebhookEvent))) == []
            assert list(await db.scalars(select(AuditLog).where(
                AuditLog.action == AuditAction.PAYMENT_SUCCEEDED.value))) == []
            assert list(await db.scalars(select(Notification).where(
                Notification.notification_type == NotificationType.PAYMENT_SUCCEEDED))) == []
    finally:
        await remove_commit_rejection("webhook_events")
    barrier = asyncio.Barrier(3)

    async def retry():
        await asyncio.wait_for(barrier.wait(), 10)
        return await client.post("/api/v1/webhooks/payments", content=raw, headers=headers)

    responses = await asyncio.wait_for(asyncio.gather(*(retry() for _ in range(3))), 20)
    assert [r.status_code for r in responses] == [200, 200, 200]
    async with TestSessionLocal() as db:
        assert (await db.get(Payment, payment["id"])).status == PaymentStatus.SUCCEEDED
        assert (await db.get(Order, order_id)).status == OrderStatus.CONFIRMED
        stock = (await db.scalars(select(Inventory))).one()
        assert (stock.available_quantity, stock.reserved_quantity) == (99, 0)
        assert list(await db.scalars(select(InventoryReservation.status))) == [ReservationStatus.CONFIRMED]
        assert len(list(await db.scalars(select(WebhookEvent)))) == 1
        assert len(list(await db.scalars(select(AuditLog).where(
            AuditLog.action == AuditAction.PAYMENT_SUCCEEDED.value)))) == 1
        notifications = list(await db.scalars(select(Notification).where(
            Notification.notification_type == NotificationType.PAYMENT_SUCCEEDED)))
        assert len(notifications) == 1
        # HTTPX waits for Starlette background tasks: the real worker must use
        # the test factory and dispatch the committed record exactly once.
        assert notifications[0].status == NotificationStatus.SENT
        assert notifications[0].attempts == 1


async def test_competing_reservation_release_and_confirmation_conserve_stock(client, admin_user, customer_user):
    await prepared_order(client, admin_user, customer_user)
    async with TestSessionLocal() as db:
        reservation_id = (await db.scalars(select(InventoryReservation.id))).one()
    barrier = asyncio.Barrier(2)

    async def transition(target):
        from app.exceptions.inventory_reservation import InvalidReservationStateError
        async with TestSessionLocal() as db:
            await asyncio.wait_for(barrier.wait(), 10)
            try:
                await InventoryReservationService(db).transition_reservation(reservation_id, target)
                await db.commit()
                return target
            except InvalidReservationStateError:
                await db.rollback()
                return None

    results = await asyncio.wait_for(asyncio.gather(
        transition(ReservationStatus.RELEASED), transition(ReservationStatus.CONFIRMED)), 20)
    assert sum(result is not None for result in results) == 1
    async with TestSessionLocal() as db:
        status = (await db.scalars(select(InventoryReservation.status))).one()
        stock = (await db.scalars(select(Inventory))).one()
        assert stock.reserved_quantity == 0
        assert stock.available_quantity == (100 if status == ReservationStatus.RELEASED else 99)
