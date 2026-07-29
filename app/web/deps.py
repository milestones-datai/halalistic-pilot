"""Web (consumer + owner portal) auth deps — Stage 11.

Cookie-based session, same SessionMiddleware as the admin console.
The session dict is unified across surfaces — `request.session["user_id"]`
is the one and only key. The role determines which UI the user lands on
after login and which routes they can access.

We keep the admin UI's `require_ui_role` separate (admin/curator) and
add a new `require_consumer_role` (diner/owner) and `require_owner_role`
(owner only) here. This means a single login at /web/login routes:
  - diner        -> /                            (consumer home)
  - owner        -> /owner/dashboard             (owner portal)
  - admin/curator-> /admin/ui/dashboard          (admin console)

Role boundaries are enforced both server-side (these deps) AND visibly
in the navbar — a diner never sees "Owner portal" in the nav, and an
owner can switch between consumer home and owner portal with a single
click.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.enums import UserRole
from app.models.user import User


async def get_optional_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Optional[User]:
    import uuid as _uuid
    uid = request.session.get("user_id")
    if not uid:
        return None
    try:
        user_id = _uuid.UUID(uid)
    except (TypeError, ValueError):
        return None
    user = await db.get(User, user_id)
    return user if (user is not None and user.is_active) else None


def require_consumer_role(*allowed: UserRole):
    """For consumer-facing routes that a Diner (or Owner) can use.
    Curator/Admin are also allowed — they may browse the consumer app
    for QA. The reverse (consumer trying admin UI) is blocked by the
    admin's own `require_ui_role`.

    Returns a User or raises HTTPException (303 with Location for
    unauthenticated, 403 for wrong role).
    """
    allowed_values = {r.value if isinstance(r, UserRole) else r for r in allowed}

    async def _dep(
        request: Request,
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        user = await get_optional_user(request, db)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/web/login"},
            )
        if user.role.value not in allowed_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {user.role.value!r} not authorized for this page",
            )
        return user
    return _dep


def require_owner_role():
    """Owner portal — owner (or admin acting on owner's behalf) only.
    Diners get 403 with a clear "this is the owner portal" message.
    """
    async def _dep(
        request: Request,
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        user = await get_optional_user(request, db)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/web/login"},
            )
        if user.role not in (UserRole.RESTAURANT_OWNER, UserRole.PLATFORM_ADMIN):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This page is for restaurant owners only.",
            )
        return user
    return _dep
