import json
from unittest.mock import patch
from uuid import uuid4

from httpx import AsyncClient
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import request_context
from app.models.audit_log import AuditAction, AuditLog
from app.models.category import Category
from app.models.inventory import Inventory
from app.models.order import Order, OrderStatus
from app.models.payment import Payment, PaymentStatus
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.audit_log import AuditLogRepository
from app.schemas.inventory import InventoryAdjustment
from app.services.audit import AuditService, AuditWriteError
from app.services.auth import AuthService
from app.services.inventory import InventoryService
from tests.conftest import TestSessionLocal
from tests.test_payment_webhooks import make_signed_webhook_payload
from tests.test_payments import login_and_get_access_token, setup_test_order


async def audits(action: AuditAction) -> list[AuditLog]:
    async with TestSessionLocal() as db:
        return list((await db.execute(select(AuditLog).where(AuditLog.action == action.value).order_by(AuditLog.id))).scalars())


async def prepared_order(client: AsyncClient, admin: User, customer: User):
    admin_token = await login_and_get_access_token(client, admin.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer.email, "CustomerPassword123!")
    order_id, product_id, warehouse_id = await setup_test_order(client, admin_token, customer_token, unit_price=100, quantity=1)
    return order_id, product_id, warehouse_id, admin_token, customer_token


async def test_order_success_creates_exactly_one_correlated_audit(client: AsyncClient, admin_user: User, customer_user: User):
    request_id = str(uuid4())
    client.headers["X-Request-ID"] = request_id
    order_id, *_ = await prepared_order(client, admin_user, customer_user)
    entries = await audits(AuditAction.ORDER_CREATED)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_id == customer_user.id and entry.resource_id == str(order_id)
    assert entry.request_id == request_id and entry.outcome == "SUCCESS"
    assert entry.details == {"item_count": 1}
    assert entry.created_at.tzinfo is not None


async def test_failed_order_has_no_success_audit(client: AsyncClient, admin_user: User, customer_user: User):
    _, product_id, _, _, token = await prepared_order(client, admin_user, customer_user)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/v1/cart/items", headers=headers, json={"product_id": product_id, "quantity": 1})).status_code == 200
    async with TestSessionLocal() as db:
        await db.execute(update(Inventory).where(Inventory.product_id == product_id).values(available_quantity=0))
        await db.commit()
    response = await client.post("/api/v1/orders", headers=headers)
    assert response.status_code == 409
    assert len(await audits(AuditAction.ORDER_CREATED)) == 1


async def test_inventory_audit_exact_quantities_and_failed_adjustment(client: AsyncClient, admin_user: User, customer_user: User):
    _, product_id, warehouse_id, token, _ = await prepared_order(client, admin_user, customer_user)
    path = f"/api/v1/inventory/products/{product_id}/warehouses/{warehouse_id}/adjust"
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post(path, headers=headers, json={"available_quantity_change": -3, "reserved_quantity_change": 2})
    assert response.status_code == 200
    events = await audits(AuditAction.INVENTORY_ADJUSTED)
    assert len(events) == 2  # Initial stock plus this explicit adjustment.
    assert events[-1].actor_id == admin_user.id
    assert events[-1].details == {
        "product_id": product_id, "warehouse_id": warehouse_id,
        "available_delta": -3, "reserved_delta": 2,
        "available_before": 99, "available_after": 96,
        "reserved_before": 1, "reserved_after": 3,
    }
    assert (await client.post(path, headers=headers, json={"available_quantity_change": -999})).status_code == 409
    assert len(await audits(AuditAction.INVENTORY_ADJUSTED)) == 2
    async with TestSessionLocal() as db:
        stock = (await db.execute(select(Inventory).where(Inventory.product_id == product_id))).scalar_one()
        assert (stock.available_quantity, stock.reserved_quantity) == (96, 3)


@pytest.mark.parametrize("event_type,action,target", [
    ("payment.succeeded", AuditAction.PAYMENT_SUCCEEDED, PaymentStatus.SUCCEEDED),
    ("payment.failed", AuditAction.PAYMENT_FAILED, PaymentStatus.FAILED),
])
async def test_payment_transition_and_replays_are_audited_once(client: AsyncClient, admin_user: User, customer_user: User, event_type, action, target):
    order_id, _, _, _, token = await prepared_order(client, admin_user, customer_user)
    payment = await client.post(f"/api/v1/orders/{order_id}/payments", headers={"Authorization": f"Bearer {token}"}, json={"idempotency_key": "audit-payment"})
    assert payment.status_code == 201
    data = payment.json()
    for event_id in ("audit-event", "audit-event", "audit-distinct-event-same-transition"):
        raw, headers = make_signed_webhook_payload(event_id, event_type, data["provider_payment_id"], "100.00", timestamp=1_000_000_000)
        # Use a stable signed timestamp so same-event callbacks have identical bodies.
        from datetime import datetime, timezone
        with patch("app.services.payment_webhook.get_current_utc_time", return_value=datetime.fromtimestamp(1_000_000_000, timezone.utc)):
            assert (await client.post("/api/v1/webhooks/payments", content=raw, headers=headers)).status_code == 200
    entries = await audits(action)
    assert len(entries) == 1 and entries[0].actor_id is None
    assert entries[0].resource_id == str(data["id"])
    assert entries[0].details == {"order_id": order_id, "from_status": "PENDING", "to_status": target.value}
    # Invalid signatures never create success/failure payment evidence.
    assert (await client.post("/api/v1/webhooks/payments", content=raw, headers={"X-Webhook-Signature": "invalid"})).status_code == 401
    assert len(await audits(action)) == 1


async def test_auth_lifecycle_and_anonymous_login_failure(client: AsyncClient, customer_user: User):
    response = await client.post("/api/v1/auth/login", json={"email": customer_user.email, "password": "CustomerPassword123!"})
    assert response.status_code == 200
    first_token = response.json()["refresh_token"]
    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": first_token})
    assert response.status_code == 200
    last_token = response.json()["refresh_token"]
    assert (await client.post("/api/v1/auth/logout", json={"refresh_token": last_token})).status_code == 204
    assert (await client.post("/api/v1/auth/logout", json={"refresh_token": last_token})).status_code == 204
    for action in (AuditAction.LOGIN_SUCCEEDED, AuditAction.TOKEN_REFRESHED, AuditAction.LOGOUT):
        entries = await audits(action)
        assert len(entries) == 1 and entries[0].actor_id == customer_user.id
    bad_email, bad_password = "private-entered@example.com", "private-entered-password"
    assert (await client.post("/api/v1/auth/login", json={"email": bad_email, "password": bad_password})).status_code == 401
    failures = await audits(AuditAction.LOGIN_FAILED)
    assert len(failures) == 1 and failures[0].actor_id is None and failures[0].resource_id is None
    assert failures[0].details == {} and failures[0].outcome == "FAILURE"
    for entry in failures + await audits(AuditAction.LOGIN_SUCCEEDED):
        safe = json.dumps({"details": entry.details, "resource": entry.resource_id, "action": entry.action})
        for private in (bad_email, bad_password, first_token, last_token, customer_user.email):
            assert private not in safe


async def test_failed_login_audit_is_isolated_from_caller_rollback(db_session: AsyncSession):
    from app.exceptions.auth import InvalidCredentialsError
    db_session.add(Category(name="Uncommitted caller", slug="uncommitted-caller"))
    with pytest.raises(InvalidCredentialsError):
        await AuthService(db_session).login("unknown-private@example.com", "private-password")
    await db_session.rollback()
    assert (await db_session.execute(select(Category))).scalars().all() == []
    assert len(await audits(AuditAction.LOGIN_FAILED)) == 1


async def test_failed_login_audit_outage_preserves_401(client: AsyncClient):
    with patch.object(AuditLogRepository, "stage", side_effect=RuntimeError("private database error")):
        response = await client.post("/api/v1/auth/login", json={"email": "unknown@example.com", "password": "private-password"})
    assert response.status_code == 401


@pytest.mark.parametrize("operation", ["login", "refresh", "logout"])
async def test_mandatory_auth_audit_failure_rolls_back_tokens(client: AsyncClient, customer_user: User, operation):
    token = None
    if operation != "login":
        login = await client.post("/api/v1/auth/login", json={"email": customer_user.email, "password": "CustomerPassword123!"})
        assert login.status_code == 200
        token = login.json()["refresh_token"]
    data = {"email": customer_user.email, "password": "CustomerPassword123!"} if operation == "login" else {"refresh_token": token}
    with patch.object(AuditLogRepository, "stage", side_effect=RuntimeError("forced audit failure")):
        with pytest.raises(AuditWriteError):
            await client.post(f"/api/v1/auth/{operation}", json=data)
    async with TestSessionLocal() as db:
        tokens = list((await db.execute(select(RefreshToken))).scalars())
        assert len(tokens) == (0 if operation == "login" else 1)
        assert all(not item.is_revoked for item in tokens)
    assert await audits(AuditAction.TOKEN_REFRESHED) == []
    assert await audits(AuditAction.LOGOUT) == []


async def test_mandatory_inventory_audit_failure_rolls_back_stock(client: AsyncClient, admin_user: User, customer_user: User):
    _, product_id, warehouse_id, _, _ = await prepared_order(client, admin_user, customer_user)
    async with TestSessionLocal() as db:
        with patch.object(AuditLogRepository, "stage", side_effect=RuntimeError("forced audit failure")):
            with pytest.raises(AuditWriteError):
                await InventoryService(db).adjust_stock(product_id, warehouse_id, InventoryAdjustment(available_quantity_change=5), actor_id=admin_user.id)
    async with TestSessionLocal() as db:
        assert (await db.execute(select(Inventory.available_quantity).where(Inventory.product_id == product_id))).scalar_one() == 99
    assert len(await audits(AuditAction.INVENTORY_ADJUSTED)) == 1


async def test_mandatory_order_audit_failure_rolls_back_all_changes(client: AsyncClient, admin_user: User, customer_user: User):
    _, product_id, _, _, token = await prepared_order(client, admin_user, customer_user)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/v1/cart/items", headers=headers, json={"product_id": product_id, "quantity": 1})).status_code == 200
    with patch.object(AuditLogRepository, "stage", side_effect=RuntimeError("forced audit failure")):
        with pytest.raises(AuditWriteError):
            await client.post("/api/v1/orders", headers=headers)
    async with TestSessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(Order))).scalar_one() == 1
        stock = (await db.execute(select(Inventory).where(Inventory.product_id == product_id))).scalar_one()
        assert (stock.available_quantity, stock.reserved_quantity) == (99, 1)
    assert len(await audits(AuditAction.ORDER_CREATED)) == 1


async def test_mandatory_payment_audit_failure_rolls_back_business_state(client: AsyncClient, admin_user: User, customer_user: User):
    order_id, product_id, _, _, token = await prepared_order(client, admin_user, customer_user)
    payment = await client.post(f"/api/v1/orders/{order_id}/payments", headers={"Authorization": f"Bearer {token}"}, json={"idempotency_key": "audit-rollback"})
    assert payment.status_code == 201
    raw, headers = make_signed_webhook_payload("audit-rollback-event", "payment.succeeded", payment.json()["provider_payment_id"], "100.00")
    with patch.object(AuditLogRepository, "stage", side_effect=RuntimeError("forced audit failure")):
        with pytest.raises(AuditWriteError):
            await client.post("/api/v1/webhooks/payments", content=raw, headers=headers)
    async with TestSessionLocal() as db:
        assert (await db.execute(select(Payment.status))).scalar_one() == PaymentStatus.PENDING
        assert (await db.execute(select(Order.status))).scalar_one() == OrderStatus.PENDING
        assert (await db.execute(select(Inventory.reserved_quantity).where(Inventory.product_id == product_id))).scalar_one() == 1
    assert await audits(AuditAction.PAYMENT_SUCCEEDED) == []


async def test_audit_stage_never_commits_and_rollback_removes_both(db_session: AsyncSession):
    db_session.add(Category(name="Atomic", slug="atomic"))
    await AuditService(db_session).stage(AuditAction.ORDER_CREATED, resource_id=42, metadata={"item_count": 1})
    async with TestSessionLocal() as observer:
        assert (await observer.execute(select(Category))).scalars().all() == []
        assert (await observer.execute(select(AuditLog))).scalars().all() == []
    await db_session.rollback()
    assert (await db_session.execute(select(Category))).scalars().all() == []
    assert await audits(AuditAction.ORDER_CREATED) == []
    assert request_context.get() is None


async def test_audit_history_survives_user_deletion(db_session: AsyncSession, customer_user: User):
    entry = await AuditService(db_session).stage(AuditAction.LOGIN_SUCCEEDED, actor_id=customer_user.id, resource_id=customer_user.id)
    audit_id = entry.id
    await db_session.commit()
    await db_session.execute(delete(User).where(User.id == customer_user.id))
    await db_session.commit()
    async with TestSessionLocal() as observer:
        stored = await observer.get(AuditLog, audit_id)
        assert stored is not None and stored.actor_id is None
        assert stored.action == AuditAction.LOGIN_SUCCEEDED.value


@pytest.mark.parametrize("metadata", [{"password": "private"}, {"email": "person@example.com"}, {"item_count": "private"}, {"item_count": True}])
async def test_audit_metadata_rejects_non_allowlisted_fields(db_session: AsyncSession, metadata):
    with pytest.raises(ValueError, match="allowlisted"):
        await AuditService(db_session).stage(AuditAction.ORDER_CREATED, metadata=metadata)
    assert (await db_session.execute(select(AuditLog))).scalars().all() == []


async def test_business_and_audit_commit_are_visible_together(db_session: AsyncSession):
    db_session.add(Category(name="Committed", slug="committed"))
    await AuditService(db_session).stage(AuditAction.ORDER_CREATED, resource_id=42, metadata={"item_count": 1})
    await db_session.commit()
    async with TestSessionLocal() as observer:
        assert len((await observer.execute(select(Category))).scalars().all()) == 1
        assert len((await observer.execute(select(AuditLog))).scalars().all()) == 1


async def test_token_reuse_revocation_and_audit_are_atomic(client: AsyncClient, customer_user: User):
    login = await client.post("/api/v1/auth/login", json={"email": customer_user.email, "password": "CustomerPassword123!"})
    assert login.status_code == 200
    original = login.json()["refresh_token"]
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": original})).status_code == 200
    # Failure to audit the all-session revocation must undo that revocation.
    with patch.object(AuditLogRepository, "stage", side_effect=RuntimeError("forced audit failure")):
        with pytest.raises(AuditWriteError):
            await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
    async with TestSessionLocal() as db:
        active = (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.is_revoked.is_(False)))).scalar_one()
        assert active == 1
    assert await audits(AuditAction.TOKEN_REUSE_DETECTED) == []
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": original})).status_code == 401
    assert len(await audits(AuditAction.TOKEN_REUSE_DETECTED)) == 1


async def test_failed_login_audit_timeout_still_rejects_credentials(client: AsyncClient):
    # Controlled timeout injection; no sleeps or external dependency failure.
    with patch("app.services.audit.asyncio.timeout", side_effect=TimeoutError):
        response = await client.post("/api/v1/auth/login", json={"email": "unknown@example.com", "password": "private"})
    assert response.status_code == 401
    assert await audits(AuditAction.LOGIN_FAILED) == []
