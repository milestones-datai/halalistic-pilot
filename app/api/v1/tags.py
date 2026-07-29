"""Public tag list — used by the new-review tag picker (Stage 5)."""
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import tags as tag_service

router = APIRouter(prefix="/tags", tags=["tags"])


class TagOut(BaseModel):
    id: int
    name: str
    slug: str
    category: str | None = None
    is_active: bool


@router.get("", response_model=list[TagOut])
async def list_active_tags(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TagOut]:
    """Public list of active tags (deactivated tags are hidden)."""
    rows = await tag_service.list_tags(db, active_only=True)
    return [
        TagOut(id=t.id, name=t.name, slug=t.slug, category=t.category, is_active=t.is_active)
        for t in rows
    ]
