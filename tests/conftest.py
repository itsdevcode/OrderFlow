import os
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.models  # pyright: ignore[reportUnusedImport]
from app.db.base import Base
from app.db.session import get_db
from app.main import app

from app.core.roles import RoleName
from app.core.security import hash_password
from app.models.role import Role
from app.models.user import User

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://orderflow_user:YOUR_PASSWORD@localhost:5432/orderflow_test",
)

test_engine = create_async_engine(
    TEST_DATABASE_URL,
    poolclass=NullPool,
)

TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@pytest_asyncio.fixture(autouse=True)
async def setup_database() -> AsyncGenerator[None, None]:
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    yield

    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)

    await test_engine.dispose()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionLocal() as session:
        yield session




@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as test_client:
        yield test_client

    app.dependency_overrides.clear()

@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    role = Role(name=RoleName.ADMIN.value)
    db_session.add(role)
    await db_session.flush()

    user = User(
        name="Test Admin",
        email="admin@test.com",
        hashed_password=hash_password("AdminPassword123!"),
        role_id=role.id,
    )

    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    return user


@pytest_asyncio.fixture
async def customer_user(db_session: AsyncSession) -> User:
    role = Role(name=RoleName.CUSTOMER.value)
    db_session.add(role)
    await db_session.flush()

    user = User(
        name="Test Customer",
        email="customer@test.com",
        hashed_password=hash_password("CustomerPassword123!"),
        role_id=role.id,
    )

    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    return user