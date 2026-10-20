from datetime import datetime
from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CachePendingInvalidation(Base):
    __tablename__: str = "cache_pending_invalidations"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    namespace: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    claim_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
