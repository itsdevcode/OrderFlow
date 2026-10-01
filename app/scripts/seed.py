import asyncio

from app.db.seed import seed_roles
from app.db.session import AsyncSessionLocal


async def main() -> None:
    async with AsyncSessionLocal() as db:
        await seed_roles(db)

    print("Roles seeded successfully.")


if __name__ == "__main__":
    asyncio.run(main())