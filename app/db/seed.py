from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.role import Role
from app.core.roles import RoleName

DEFAULT_ROLES = tuple(role.value for role in RoleName)


async def seed_roles(db: AsyncSession) -> None:
    result = await db.execute(
        select(Role.name).where(Role.name.in_(DEFAULT_ROLES))
    )

    existing_roles = set(result.scalars().all())

    missing_roles = [
        Role(name=role_name)
        for role_name in DEFAULT_ROLES
        if role_name not in existing_roles
    ]

    if not missing_roles:
        return

    db.add_all(missing_roles)
    await db.commit()