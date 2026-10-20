from decimal import Decimal
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.roles import RoleName
from app.exceptions.order import OrderNotFoundError
from app.exceptions.payment import (
    IdempotencyKeyConflictError,
    InvalidPaymentStateTransitionError,
    PaymentNotFoundError,
    PaymentOrderAlreadyPaidError,
    PaymentOrderCancelledError,
    PaymentProviderError,
    PendingPaymentExistsError,
    UnsupportedPaymentProviderError,
)
from app.models.order import Order, OrderStatus
from app.models.audit_log import AuditAction
from app.services.audit import AuditService
from app.models.payment import Payment, PaymentStatus
from app.models.user import User
from app.repositories.payment import PaymentRepository
from app.services.fake_payment_provider import FakePaymentProvider
from app.services.payment_provider import PaymentProvider


class PaymentService:
    db: AsyncSession
    payment_repo: PaymentRepository
    provider: PaymentProvider

    def __init__(
        self,
        db: AsyncSession,
        provider: PaymentProvider | None = None,
    ) -> None:
        self.db = db
        self.payment_repo = PaymentRepository(db)
        self.provider = provider or FakePaymentProvider()

    async def _get_authorized_order(
        self,
        order_id: int,
        user: User,
        for_update: bool = False,
    ) -> Order:
        stmt = (
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
        )
        if for_update:
            stmt = stmt.with_for_update()

        result = await self.db.execute(stmt)
        order = result.scalar_one_or_none()

        if not order:
            raise OrderNotFoundError("Order not found")

        # Isolation check: only owner or ADMIN/MANAGER may view or act
        is_owner = order.user_id == user.id
        is_staff = user.role.name in (RoleName.ADMIN.value, RoleName.MANAGER.value)
        if not (is_owner or is_staff):
            raise OrderNotFoundError("Order not found")

        return order

    def _calculate_order_amount(self, order: Order) -> Decimal:
        return sum(
            (Decimal(str(item.unit_price)) * Decimal(str(item.quantity)) for item in order.items),
            Decimal("0.00"),
        )

    def _validate_idempotency_compatibility(
        self,
        payment: Payment,
        order_id: int,
        provider_name: str,
    ) -> None:
        """
        Validate that a replayed idempotency key matches the complete compatible request context.
        If order_id or provider do not match, reject with 409 Conflict.
        """
        if payment.order_id != order_id:
            raise IdempotencyKeyConflictError(
                f"Idempotency key '{payment.idempotency_key}' is already used for another order"
            )
        if payment.provider != provider_name:
            raise IdempotencyKeyConflictError(
                f"Idempotency key '{payment.idempotency_key}' was used with provider '{payment.provider}', incompatible with '{provider_name}'"
            )

    async def initiate_payment(
        self,
        order_id: int,
        user: User,
        idempotency_key: str,
        provider_name: str | None = None,
    ) -> Payment:
        chosen_provider = provider_name or self.provider.provider_name

        # Validate that the requested provider matches the configured provider implementation
        if chosen_provider != self.provider.provider_name:
            raise UnsupportedPaymentProviderError(
                f"Unsupported payment provider '{chosen_provider}'. Supported provider: '{self.provider.provider_name}'"
            )

        # Fast path check on idempotency key
        existing_payment = await self.payment_repo.get_by_idempotency_key(idempotency_key)
        if existing_payment:
            self._validate_idempotency_compatibility(existing_payment, order_id, chosen_provider)
            # Verify user authorization
            _ = await self._get_authorized_order(order_id, user, for_update=False)
            return existing_payment

        try:
            # Lock the order row to serialize concurrent payment attempts on the same order
            order = await self._get_authorized_order(order_id, user, for_update=True)

            if order.status == OrderStatus.CANCELLED:
                raise PaymentOrderCancelledError("Cannot initiate payment for a cancelled order")

            # Check if order is already paid
            succeeded_payment = await self.payment_repo.get_succeeded_by_order_id(order_id)
            if succeeded_payment:
                raise PaymentOrderAlreadyPaidError("Order has already been paid")

            # Check if an active PENDING payment already exists
            pending_payment = await self.payment_repo.get_active_pending_by_order_id(order_id)
            if pending_payment:
                # If key matches, validate compatibility and return it
                if pending_payment.idempotency_key == idempotency_key:
                    self._validate_idempotency_compatibility(pending_payment, order_id, chosen_provider)
                    return pending_payment
                raise PendingPaymentExistsError(
                    "A pending payment is already active for this order. Complete or cancel it first."
                )

            # Check again for idempotency key under transaction
            existing_payment = await self.payment_repo.get_by_idempotency_key(idempotency_key)
            if existing_payment:
                self._validate_idempotency_compatibility(existing_payment, order_id, chosen_provider)
                return existing_payment

            # Derive amount from persisted OrderItems snapshots
            amount = self._calculate_order_amount(order)
            currency = "INR"

            # Execute provider charge call with boundary exception handling
            try:
                provider_result = await self.provider.create_charge(
                    amount=amount,
                    currency=currency,
                    idempotency_key=idempotency_key,
                    order_id=order_id,
                )
            except Exception as provider_exc:
                # Sanitized 502 boundary exception; do not leak internal transport details
                raise PaymentProviderError(
                    "Payment provider processing failed. Please try again later."
                ) from provider_exc

            # Check if provider explicitly rejected the attempt (success=False)
            initial_status = PaymentStatus.PENDING if provider_result.success else PaymentStatus.FAILED

            payment = Payment(
                order_id=order.id,
                provider=chosen_provider,
                provider_payment_id=provider_result.provider_payment_id,
                idempotency_key=idempotency_key,
                amount=amount,
                currency=currency,
                status=initial_status,
            )

            self.db.add(payment)
            await self.db.commit()
            await self.db.refresh(payment)
            return payment

        except IntegrityError:
            await self.db.rollback()
            # Handle concurrent race conditions on unique indexes
            existing = await self.payment_repo.get_by_idempotency_key(idempotency_key)
            if existing:
                self._validate_idempotency_compatibility(existing, order_id, chosen_provider)
                return existing

            # Check if pending payment constraint triggered
            pending = await self.payment_repo.get_active_pending_by_order_id(order_id)
            if pending:
                if pending.idempotency_key == idempotency_key:
                    self._validate_idempotency_compatibility(pending, order_id, chosen_provider)
                    return pending
                raise PendingPaymentExistsError(
                    "A pending payment is already active for this order"
                )

            succeeded = await self.payment_repo.get_succeeded_by_order_id(order_id)
            if succeeded:
                raise PaymentOrderAlreadyPaidError("Order has already been paid")

            raise
        except Exception:
            await self.db.rollback()
            raise

    async def list_order_payments(
        self,
        order_id: int,
        user: User,
    ) -> Sequence[Payment]:
        _ = await self._get_authorized_order(order_id, user, for_update=False)
        return await self.payment_repo.list_by_order_id(order_id)

    async def apply_payment_transition(
        self,
        payment: Payment,
        target_status: PaymentStatus,
    ) -> Payment:
        """
        Validate and apply state transition on payment in-memory without committing.
        Flushes to session so constraints and changes are reflected in the current transaction.
        """
        current_status = payment.status

        # If already in the target status (terminal state idempotency)
        if current_status == target_status:
            return payment

        valid_transitions: dict[PaymentStatus, set[PaymentStatus]] = {
            PaymentStatus.PENDING: {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED},
            PaymentStatus.SUCCEEDED: {PaymentStatus.REFUNDED},
            PaymentStatus.FAILED: set(),
            PaymentStatus.REFUNDED: set(),
        }

        if target_status not in valid_transitions.get(current_status, set()):
            raise InvalidPaymentStateTransitionError(
                f"Cannot transition payment from {current_status.value} to {target_status.value}"
            )

        payment.status = target_status
        await self.db.flush()
        if target_status in {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED}:
            await AuditService(self.db).stage(
                AuditAction.PAYMENT_SUCCEEDED if target_status == PaymentStatus.SUCCEEDED else AuditAction.PAYMENT_FAILED,
                resource_id=payment.id,
                metadata={"order_id": payment.order_id, "from_status": current_status.value, "to_status": target_status.value},
            )
        return payment

    async def transition_payment_status(
        self,
        payment_id: int,
        target_status: PaymentStatus,
        *,
        auto_commit: bool = True,
    ) -> Payment:
        """
        Validated state transitions:
        - PENDING -> SUCCEEDED
        - PENDING -> FAILED
        - SUCCEEDED -> REFUNDED
        Idempotent: Repeating the same valid terminal transition returns payment without duplicating side effects.
        """
        payment = await self.payment_repo.get_by_id_for_update(payment_id)
        if not payment:
            raise PaymentNotFoundError(f"Payment with id {payment_id} not found")

        try:
            updated_payment = await self.apply_payment_transition(payment, target_status)
            if auto_commit:
                await self.db.commit()
                await self.db.refresh(updated_payment)
            return updated_payment
        except Exception:
            if auto_commit:
                await self.db.rollback()
            raise
