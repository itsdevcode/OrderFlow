import pytest

from tests.resource_safety import validate_test_resources


@pytest.mark.parametrize("database,redis", [
    ("postgresql+asyncpg://localhost/production", "redis://localhost/1"),
    ("postgresql+asyncpg://remote.example/orderflow_test", "redis://localhost/1"),
    ("postgresql+asyncpg://localhost/orderflow_test", "redis://localhost/0"),
    ("postgresql+asyncpg://localhost/orderflow_test", "redis://remote.example/1"),
    ("postgresql+asyncpg://localhost/orderflow_test?host=remote.example", "redis://localhost/1"),
    ("postgresql+asyncpg://localhost/orderflow_test", "redis://localhost/1?db=0"),
])
def test_destructive_fixtures_reject_non_test_resources(database, redis):
    with pytest.raises(ValueError):
        validate_test_resources(database, redis)


def test_dedicated_local_test_resources_are_accepted():
    validate_test_resources("postgresql+asyncpg://127.0.0.1/orderflow_test", "redis://localhost:6379/1")
