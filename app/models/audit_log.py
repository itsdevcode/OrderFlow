from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditAction(str, Enum):
    LOGIN_SUCCEEDED = "AUTH.LOGIN_SUCCEEDED"
    LOGIN_FAILED = "AUTH.LOGIN_FAILED"
    TOKEN_REFRESHED = "AUTH.TOKEN_REFRESHED"
    TOKEN_REUSE_DETECTED = "AUTH.TOKEN_REUSE_DETECTED"
    LOGOUT = "AUTH.LOGOUT"
    ORDER_CREATED = "ORDER.CREATED"
    INVENTORY_ADJUSTED = "INVENTORY.ADJUSTED"
    PAYMENT_SUCCEEDED = "PAYMENT.SUCCEEDED"
    PAYMENT_FAILED = "PAYMENT.FAILED"


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_actor_created", "actor_id", "created_at"),
        Index("ix_audit_logs_resource_created", "resource_type", "resource_id", "created_at"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Declarative Base reserves the Python name 'metadata'.
    details: Mapped[dict[str, int | str]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
