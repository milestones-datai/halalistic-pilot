"""/admin/* endpoints — internal-only, Admin-gated.

Stage 2 only ships the role-assignment endpoint. Future stages will add
admin moderation tools (review queue, deal override, etc.).
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps.auth import require_role
from app.models.enums import UserRole
from app.models.user import User
from app.services.auth_service import assign_role

router = APIRouter(prefix="/admin", tags=["admin"])


class RoleAssignIn(BaseModel):
    role: str  # validated server-side: must be UserRole.admin_assignable()


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: str


@router.post("/users/{user_id}/role", response_model=UserOut)
async def assign_user_role(
    user_id: uuid.UUID,
    body: RoleAssignIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> UserOut:
    user = await assign_role(db, user_id, body.role)
    return UserOut(
        id=user.id, email=user.email, display_name=user.display_name, role=user.role.value,
    )
