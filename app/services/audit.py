from asyncio import timeout
import logging
from collections.abc import Mapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import request_context
from app.db import session as session_module
from app.models.audit_log import AuditAction, AuditLog
from app.repositories.audit_log import AuditLogRepository

logger = logging.getLogger(__name__)

_NUMERIC_FIELDS: dict[AuditAction, set[str]] = {
    AuditAction.ORDER_CREATED: {"item_count"},
    AuditAction.INVENTORY_ADJUSTED: {
        "product_id", "warehouse_id", "available_delta", "reserved_delta",
        "available_before", "available_after", "reserved_before", "reserved_after",
    },
    AuditAction.PAYMENT_SUCCEEDED: {"order_id"},
    AuditAction.PAYMENT_FAILED: {"order_id"},
}
_STATUS_FIELDS = {"from_status", "to_status"}


class AuditWriteError(RuntimeError):
    """Mandatory audit insertion failed; the business owner must roll back."""


class AuditService:
    """Stage mandatory audits in the business session; never commit/rollback it."""

    def __init__(self, db: AsyncSession) -> None:
        self.repo = AuditLogRepository(db)

    async def stage(
        self, action: AuditAction, *, actor_id: int | None = None,
        resource_id: int | None = None, metadata: Mapping[str, int | str] | None = None,
    ) -> AuditLog:
        action = AuditAction(action)
        details = dict(metadata or {})
        numeric = _NUMERIC_FIELDS.get(action, set())
        for key, value in details.items():
            if key in numeric and type(value) is int:
                continue
            if action in {AuditAction.PAYMENT_SUCCEEDED, AuditAction.PAYMENT_FAILED} and key in _STATUS_FIELDS and isinstance(value, str) and value in {"PENDING", "SUCCEEDED", "FAILED"}:
                continue
            raise ValueError("Audit metadata is not allowlisted")
        context = request_context.get()
        prefix = action.value.split(".")[0].lower()
        entry = AuditLog(
            actor_id=actor_id, action=action.value, resource_type=prefix,
            resource_id=str(resource_id) if resource_id is not None else None,
            outcome="FAILURE" if action in {AuditAction.LOGIN_FAILED, AuditAction.TOKEN_REUSE_DETECTED, AuditAction.PAYMENT_FAILED} else "SUCCESS",
            request_id=context.request_id if context else None, details=details,
        )
        try:
            return await self.repo.stage(entry)
        except Exception as exc:
            raise AuditWriteError("Mandatory audit persistence failed") from exc

    @staticmethod
    async def record_login_failure() -> None:
        """Anonymous best-effort evidence, isolated from the caller's read session.

        No submitted identity or credentials are accepted. Failure must not
        replace an authentication rejection with a diagnostic/database error.
        """
        try:
            async with timeout(settings.audit_failure_timeout_seconds):
                async with session_module.AsyncSessionLocal() as db:
                    async with db.begin():
                        await AuditService(db).stage(AuditAction.LOGIN_FAILED)
        except Exception:
            logger.warning("audit unavailable", extra={"event": "audit.login_failure_unavailable"})
