"""Fresh migration and latest downgrade smoke check; disposable CI database only."""
import asyncio

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings


async def revisions(*, require_empty: bool = False) -> set[str]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            if require_empty:
                tables = (await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).all()
                if tables:
                    raise RuntimeError("Migration smoke check requires an empty database")
                return set()
            return set((await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars())
    finally:
        await engine.dispose()


def main() -> None:
    url = make_url(settings.database_url)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "orderflow_migrations":
        raise RuntimeError("Migration smoke check requires local orderflow_migrations")
    config = Config("alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"Expected one migration head; found {len(heads)}")
    head = scripts.get_revision(heads[0])
    if head is None or not isinstance(head.down_revision, str):
        raise RuntimeError("Expected latest migration with a single parent")
    asyncio.run(revisions(require_empty=True))
    command.upgrade(config, "head")
    assert asyncio.run(revisions()) == set(heads), "Database did not reach head"
    command.current(config)
    command.downgrade(config, head.down_revision)
    assert asyncio.run(revisions()) == {head.down_revision}, "Downgrade revision mismatch"
    command.upgrade(config, "head")
    assert asyncio.run(revisions()) == set(heads), "Re-upgrade revision mismatch"
    print("Fresh upgrade and latest downgrade/re-upgrade passed")


if __name__ == "__main__":
    main()
