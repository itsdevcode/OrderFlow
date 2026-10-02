from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.core.roles import RoleName
from app.dependencies.auth import CurrentUser
from app.models.user import User


def require_roles(
    *allowed_roles: RoleName,
) -> Callable[..., Awaitable[User]]:

    async def role_checker(
        current_user: CurrentUser,
    ) -> User:
        if current_user.role.name not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )

        return current_user

    return role_checker


AdminUser = Annotated[
    User,
    Depends(require_roles(RoleName.ADMIN)),
]