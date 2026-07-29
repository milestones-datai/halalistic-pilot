"""Auth dependencies: current-user extraction and role gating."""
import uuid
from typing import Annotated, Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.user import User

# auto_error=False so we can return 401 (not 403) on missing/invalid credentials,
# per OAuth2 spec. The default auto_error=True returns 403 which is for
# "authenticated but not authorized", not "no credentials provided".
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Extract the access-token bearer, decode it, and load the User row.

    Returns 401 (not 403, not 500, not silent success) on every failure path.
    """
    auth = await bearer_scheme(request)
    if auth is None or not auth.credentials:
        raise HTTPException(
            status_code=401,
            detail="missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(auth.credentials, expected_type="access")
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401, detail="access token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=401, detail=f"invalid access token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=401, detail="malformed access token")

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or inactive")
    return user


async def optional_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Optional[User]:
    """Best-effort current-user extraction. Returns the User if a valid
    bearer token is present, else None — no 401. Use for endpoints that
    work for both anonymous and authenticated viewers (e.g. the public
    active-deals listing), and gate personalized content based on the
    result.
    """
    auth = await bearer_scheme(request)
    if auth is None or not auth.credentials:
        return None
    try:
        payload = decode_token(auth.credentials, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None
    user = await db.get(User, user_id)
    return user if (user is not None and user.is_active) else None


def require_role(*allowed: UserRole | str):
    """Dependency factory: returns a dep that 403s if the current user lacks any of `allowed`.

    Usage:
        @router.post("/admin/foo")
        async def foo(
            _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
        ):
            ...
    """
    allowed_values = {r.value if isinstance(r, UserRole) else r for r in allowed}

    async def _checker(
        user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if user.role.value not in allowed_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {user.role.value!r} not authorized; need one of {sorted(allowed_values)}",
            )
        return user

    return _checker
