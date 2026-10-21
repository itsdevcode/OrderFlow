"""Validate CI configuration and provision an empty, isolated test database."""
import asyncio
import os

import asyncpg
from redis.asyncio import Redis
from sqlalchemy.engine import make_url

from app.core.config import settings


async def main() -> None:
    database = make_url(settings.database_url)
    test = make_url(os.environ["TEST_DATABASE_URL"])
    # These scripts deliberately accept only local, explicitly named CI resources.
    for url in (database, test):
        if url.host not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("CI database must be local")
    if database.database != "orderflow_migrations" or test.database != "orderflow_test":
        raise RuntimeError("Expected dedicated CI database names")
    connection = await asyncpg.connect(database.render_as_string(hide_password=False).replace("postgresql+asyncpg:", "postgresql:"))
    try:
        if await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = 'orderflow_test'"):
            raise RuntimeError("Test database must not already exist")
        await connection.execute("CREATE DATABASE orderflow_test")
    finally:
        await connection.close()
    connection = await asyncpg.connect(test.render_as_string(hide_password=False).replace("postgresql+asyncpg:", "postgresql:"))
    try:
        assert await connection.fetchval("SELECT 1") == 1
    finally:
        await connection.close()
    for url, expected_db in ((settings.redis_url, 0), (os.environ["TEST_REDIS_URL"], 1)):
        client = Redis.from_url(url)
        try:
            options = client.connection_pool.connection_kwargs
            if options.get("host") not in {"127.0.0.1", "localhost"} or options.get("db", 0) != expected_db:
                raise RuntimeError("Expected isolated local Redis databases 0 and 1")
            assert await client.ping()
        finally:
            await client.aclose()
    print("Settings, PostgreSQL and Redis validated")


if __name__ == "__main__":
    asyncio.run(main())
