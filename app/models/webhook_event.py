from datetime import datetime
from enum import Enum as PyEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.payment import Payment


class WebhookEventStatus(str, PyEnum):
    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class WebhookEvent(Base):
    __tablename__: str = "webhook_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String, nullable=False, index=True)
    provider_event_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[WebhookEventStatus] = mapped_column(
        Enum(WebhookEventStatus, name="webhookeventstatus_enum", native_enum=False),
        nullable=False,
        default=WebhookEventStatus.RECEIVED,
        index=True,
    )
    payload_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_webhook_events_provider_event_id",
        ),
    )

    # Relationship
    payment: Mapped["Payment | None"] = relationship("Payment")
