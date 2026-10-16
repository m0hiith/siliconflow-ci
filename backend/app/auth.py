"""Who is calling, and may they?

Sign-in (GitHub OAuth sessions) is not implemented yet. Until it is,
get_current_user identifies nobody, so every endpoint that requires a user
answers 401: write endpoints fail closed rather than trusting a header.
Tests supply a user through FastAPI's dependency_overrides.
"""
from typing import Annotated

from fastapi import Depends, HTTPException

from app.models import User, UserRole


async def get_current_user() -> User:
    raise HTTPException(status_code=401, detail="authentication required")


async def require_maintainer(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role is not UserRole.maintainer:
        raise HTTPException(status_code=403, detail="maintainer role required")
    return user


Maintainer = Annotated[User, Depends(require_maintainer)]
