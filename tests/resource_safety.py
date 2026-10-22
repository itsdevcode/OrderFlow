"""Validate destructive test resources before creating engines or Redis clients."""
from sqlalchemy.engine import make_url


def validate_test_resources(database_url: str, redis_url: str) -> None:
    database = make_url(database_url)
    redis = make_url(redis_url)
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if (database.drivername != "postgresql+asyncpg"
            or database.host not in local_hosts
            or database.database != "orderflow_test" or database.query):
        raise ValueError("Tests require a local PostgreSQL database named orderflow_test")
    if (redis.drivername != "redis" or redis.host not in local_hosts
            or redis.database != "1" or redis.query):
        raise ValueError("Tests require local Redis database 1")
