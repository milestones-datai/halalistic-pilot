"""Admin UI dependencies — Stage 10.

Cookie-based session auth for the internal admin/curator console
(/admin/ui/*). Separate from the API's bearer-token auth so we can
have a different login UX (form post) and different timeout policy.

The session is signed with `admin_ui_session_secret` (defaults to a
derivation of SECRET_KEY for dev). In prod, override the env var.

Why a separate auth path:
  - Internal users (curator / admin) authenticate less often, with
    longer session lifetimes, and the console has different CSRF /
    password policy needs.
  - Reusing the bearer auth would force every UI page to ship a
    hidden input with the JWT, which leaks it to template logs.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.user import User


# Map allowed roles to a label for the dependency factory.
ADMIN_UI_ROLES: tuple[UserRole, ...] = (
    UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR,
)


def session_secret() -> str:
    """Resolve the session secret. Falls back to SECRET_KEY so dev works
    out of the box; in prod set ADMIN_UI_SESSION_SECRET to a distinct
    strong random.
    """
    if settings.admin_ui_session_secret:
        return settings.admin_ui_session_secret
    return f"admin-ui:{settings.secret_key}"


async def ui_login(
    db: AsyncSession, request: Request, email: str, password: str,
) -> User:
    """Authenticate a curator or admin against the User table and write
    the session cookie. Returns the User on success; raises 401 on
    bad credentials, 403 on wrong role.
    """
    from app.core.security import verify_password
    user = (await db.execute(
        select(User).where(User.email == email)
    )).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    if user.role not in ADMIN_UI_ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"role {user.role.value!r} is not authorized for the admin console",
        )
    # Persist just enough in the session. The full User is reloaded on
    # every request so role changes take effect immediately.
    request.session["user_id"] = str(user.id)
    return user


async def ui_logout(request: Request) -> None:
    request.session.clear()


async def get_current_ui_user(
    request: Request,
    db: AsyncSession,
) -> Optional[User]:
    """Resolve the current user from the session cookie, or None if not
    signed in. Does NOT raise 401 — endpoints that need auth should
    use `require_ui_role` instead.
    """
    uid = request.session.get("user_id")
    if not uid:
        return None
    import uuid as _uuid
    try:
        user_id = _uuid.UUID(uid)
    except (TypeError, ValueError):
        return None
    user = await db.get(User, user_id)
    return user if (user is not None and user.is_active) else None


def require_ui_role(*allowed: UserRole):
    """FastAPI dep factory for UI routes. 302s to /admin/ui/login if not
    signed in; 403s (rendered as an error page) if the wrong role.

    NB: sub-dependencies are declared via `Depends(...)` so FastAPI
    treats this the same way it treats `require_role` in deps/auth.py.
    Otherwise FastAPI's response-model inference sees the raw SQLAlchemy
    `User` type and crashes.
    """
    from fastapi import Depends as _Depends
    allowed_values = {r.value if isinstance(r, UserRole) else r for r in allowed}

    async def _dep(
        request: Request,
        db: AsyncSession = _Depends(get_db),
    ) -> User:
        user = await get_current_ui_user(request, db)
        if user is None:
            # Redirect to login, preserving the original URL.
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/admin/ui/login"},
            )
        if user.role.value not in allowed_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {user.role.value!r} not authorized; need one of {sorted(allowed_values)}",
            )
        return user

    return _dep
