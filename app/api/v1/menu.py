"""Menu CRUD endpoints (4-level).

Public-readable menu is exposed via the restaurant profile (GET /restaurants/{id}).
Owner-only write endpoints below.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps.auth import get_current_user
from app.models.menu import MenuCategory, MenuItem, MenuItemVariant, MenuSubcategory
from app.models.user import User
from app.services import restaurant_service

router = APIRouter(tags=["menu"])


def _ensure_owner_or_admin(restaurant_id, actor: User, *, is_admin: bool) -> None:
    """Stub: the actual ownership check happens via the service; this is
    a placeholder so the dependency is in one place. Real check lives in
    restaurant_service for now — menu endpoints route through it.
    """
    if is_admin:
        return
    # Service layer does the lookup; we just gate on role here.
    if actor.role.value not in ("restaurant_owner", "platform_admin"):
        raise HTTPException(status_code=403, detail="only Restaurant Owners or Admins can edit menus")


# ---- Schemas ----
class CategoryIn(BaseModel):
    name: str
    sort_order: int = 0
    is_active: bool = True


class CategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    sort_order: int
    is_active: bool


class SubcategoryIn(BaseModel):
    name: str
    sort_order: int = 0
    is_active: bool = True


class SubcategoryOut(BaseModel):
    id: uuid.UUID
    category_id: uuid.UUID
    name: str
    sort_order: int
    is_active: bool


class ItemIn(BaseModel):
    category_id: uuid.UUID
    subcategory_id: Optional[uuid.UUID] = None
    name: str
    description: Optional[str] = None
    base_price_cents: int = 0
    currency: str = "USD"
    photo_url: Optional[str] = None
    allergens: Optional[list[str]] = None
    calories: Optional[int] = None
    prep_time_minutes: Optional[int] = None
    is_available: bool = True
    sort_order: int = 0


class ItemOut(BaseModel):
    id: uuid.UUID
    category_id: uuid.UUID
    subcategory_id: Optional[uuid.UUID]
    name: str
    description: Optional[str]
    base_price_cents: int
    currency: str
    is_available: bool
    sort_order: int


class VariantIn(BaseModel):
    name: str
    price_cents: int
    is_default: bool = False
    is_available: bool = True
    sort_order: int = 0


class VariantOut(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID
    name: str
    price_cents: int
    is_default: bool
    is_available: bool
    sort_order: int


# ---- Category ----
@router.post(
    "/restaurants/{restaurant_id}/menu/categories",
    response_model=CategoryOut,
    status_code=201,
)
async def create_category(
    restaurant_id: uuid.UUID,
    body: CategoryIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> CategoryOut:
    r = await restaurant_service.get_or_404(db, restaurant_id)
    is_admin = actor.role.value == "platform_admin"
    restaurant_service._ensure_owner_or_admin(r, actor, is_admin=is_admin)
    cat = MenuCategory(
        id=uuid.uuid4(), restaurant_id=restaurant_id,
        name=body.name, sort_order=body.sort_order, is_active=body.is_active,
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return CategoryOut(id=cat.id, name=cat.name, sort_order=cat.sort_order, is_active=cat.is_active)


# ---- Subcategory ----
@router.post(
    "/restaurants/{restaurant_id}/menu/categories/{category_id}/subcategories",
    response_model=SubcategoryOut,
    status_code=201,
)
async def create_subcategory(
    restaurant_id: uuid.UUID,
    category_id: uuid.UUID,
    body: SubcategoryIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> SubcategoryOut:
    r = await restaurant_service.get_or_404(db, restaurant_id)
    is_admin = actor.role.value == "platform_admin"
    restaurant_service._ensure_owner_or_admin(r, actor, is_admin=is_admin)
    cat = await db.get(MenuCategory, category_id)
    if cat is None or cat.restaurant_id != restaurant_id:
        raise HTTPException(status_code=404, detail="category not found in this restaurant")
    sub = MenuSubcategory(
        id=uuid.uuid4(), category_id=category_id, restaurant_id=restaurant_id,
        name=body.name, sort_order=body.sort_order, is_active=body.is_active,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return SubcategoryOut(
        id=sub.id, category_id=sub.category_id, name=sub.name,
        sort_order=sub.sort_order, is_active=sub.is_active,
    )


# ---- Item ----
@router.post(
    "/restaurants/{restaurant_id}/menu/items",
    response_model=ItemOut,
    status_code=201,
)
async def create_item(
    restaurant_id: uuid.UUID,
    body: ItemIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> ItemOut:
    r = await restaurant_service.get_or_404(db, restaurant_id)
    is_admin = actor.role.value == "platform_admin"
    restaurant_service._ensure_owner_or_admin(r, actor, is_admin=is_admin)
    cat = await db.get(MenuCategory, body.category_id)
    if cat is None or cat.restaurant_id != restaurant_id:
        raise HTTPException(status_code=404, detail="category not found in this restaurant")
    if body.subcategory_id is not None:
        sub = await db.get(MenuSubcategory, body.subcategory_id)
        if sub is None or sub.restaurant_id != restaurant_id:
            raise HTTPException(status_code=404, detail="subcategory not found in this restaurant")
    it = MenuItem(
        id=uuid.uuid4(),
        category_id=body.category_id,
        subcategory_id=body.subcategory_id,
        restaurant_id=restaurant_id,
        name=body.name,
        description=body.description,
        base_price_cents=body.base_price_cents,
        currency=body.currency,
        photo_url=body.photo_url,
        allergens=body.allergens,
        calories=body.calories,
        prep_time_minutes=body.prep_time_minutes,
        is_available=body.is_available,
        sort_order=body.sort_order,
    )
    db.add(it)
    await db.commit()
    await db.refresh(it)
    return ItemOut(
        id=it.id, category_id=it.category_id, subcategory_id=it.subcategory_id,
        name=it.name, description=it.description, base_price_cents=it.base_price_cents,
        currency=it.currency, is_available=it.is_available, sort_order=it.sort_order,
    )


# ---- Variant ----
@router.post(
    "/restaurants/{restaurant_id}/menu/items/{item_id}/variants",
    response_model=VariantOut,
    status_code=201,
)
async def create_variant(
    restaurant_id: uuid.UUID,
    item_id: uuid.UUID,
    body: VariantIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> VariantOut:
    r = await restaurant_service.get_or_404(db, restaurant_id)
    is_admin = actor.role.value == "platform_admin"
    restaurant_service._ensure_owner_or_admin(r, actor, is_admin=is_admin)
    it = await db.get(MenuItem, item_id)
    if it is None or it.restaurant_id != restaurant_id:
        raise HTTPException(status_code=404, detail="item not found in this restaurant")
    v = MenuItemVariant(
        id=uuid.uuid4(), item_id=item_id,
        name=body.name, price_cents=body.price_cents,
        is_default=body.is_default, is_available=body.is_available,
        sort_order=body.sort_order,
    )
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return VariantOut(
        id=v.id, item_id=v.item_id, name=v.name, price_cents=v.price_cents,
        is_default=v.is_default, is_available=v.is_available, sort_order=v.sort_order,
    )
