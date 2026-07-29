"""Tag service — admin CRUD + public active list (Stage 5)."""
from __future__ import annotations

import re
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tag import Tag


def _slugify(s: str) -> str:
    """URL-safe slug. Lower-case, alnum + dashes, no leading/trailing dashes."""
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


async def list_tags(db: AsyncSession, *, active_only: bool) -> list[Tag]:
    stmt = select(Tag).order_by(Tag.category.is_(None), Tag.category, Tag.name)
    if active_only:
        stmt = stmt.where(Tag.is_active.is_(True))
    return list((await db.execute(stmt)).scalars().all())


async def get_or_404(db: AsyncSession, tag_id: int) -> Tag:
    t = await db.get(Tag, tag_id)
    if t is None:
        raise HTTPException(status_code=404, detail="tag not found")
    return t


async def create_tag(
    db: AsyncSession,
    *,
    name: str,
    slug: Optional[str] = None,
    category: Optional[str] = None,
) -> Tag:
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    slug = (slug or _slugify(name)).strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug is required (or supply a name that slugifies)")
    # Uniqueness check — friendly error rather than a 500 from the DB.
    existing = (await db.execute(select(Tag).where(Tag.slug == slug))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"tag with slug {slug!r} already exists")
    existing_name = (await db.execute(select(Tag).where(Tag.name == name))).scalar_one_or_none()
    if existing_name is not None:
        raise HTTPException(status_code=409, detail=f"tag with name {name!r} already exists")
    t = Tag(name=name, slug=slug, category=category, is_active=True)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def set_tag_active(
    db: AsyncSession, *, tag_id: int, is_active: bool
) -> Tag:
    t = await get_or_404(db, tag_id)
    t.is_active = is_active
    await db.commit()
    await db.refresh(t)
    return t
