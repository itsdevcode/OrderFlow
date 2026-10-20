from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


engine = create_async_engine(
    settings.database_url,
    # SQL parameters can contain credentials and personal information.
    echo=False,
    hide_parameters=True,
)


AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session

# Track flushed writes too: Session.new/dirty become empty after flush, but
# those changes still belong to the caller's uncommitted transaction.

@event.listens_for(Session, "after_flush")
def _mark_flushed_writes(session: Session, _context: object) -> None:
    session.info["uncommitted_writes"] = True


@event.listens_for(Session, "after_transaction_end")
def _clear_flushed_writes(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        session.info.pop("uncommitted_writes", None)
