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
from app.main import app as fastapi_app

from app.core.config import settings
from app.core.redis import init_redis_client
from app.core.roles import RoleName
from app.core.security import hash_password
from app.models.role import Role
from app.models.user import User

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://orderflow_user:orderflow_password@localhost:5432/orderflow_test",
)
TEST_REDIS_URL = os.getenv(
    "TEST_REDIS_URL",
    "redis://localhost:6379/1",
)
settings.redis_url = TEST_REDIS_URL

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


@pytest_asyncio.fixture(autouse=True)
async def setup_redis() -> AsyncGenerator[None, None]:
    client = await init_redis_client()
    if client is not None:
        try:
            _ = await client.flushdb()
        except Exception:
            pass

    yield

    if client is not None:
        try:
            _ = await client.flushdb()
        except Exception:
            pass


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionLocal() as session:
        yield session




import pytest
from app.db import session as db_session_module

@pytest.fixture(autouse=True)
def bind_test_session_local():
    orig_session_local = db_session_module.AsyncSessionLocal
    db_session_module.AsyncSessionLocal = TestSessionLocal
    yield
    db_session_module.AsyncSessionLocal = orig_session_local


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with TestSessionLocal() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=fastapi_app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as test_client:
        yield test_client

    fastapi_app.dependency_overrides.clear()

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