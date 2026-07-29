"""Deals API — owner, curator, and public (Stage 6).

Endpoints
---------
Owner (RESTAURANT_OWNER or PLATFORM_ADMIN):
  POST   /api/v1/restaurants/{rid}/deals                create DRAFT
  GET    /api/v1/restaurants/{rid}/deals                list own (all states)
  GET    /api/v1/deals/{id}                             read one
  PUT    /api/v1/deals/{id}                             edit (DRAFT or REJECTED only)
  POST   /api/v1/deals/{id}/submit                      DRAFT -> PENDING_REVIEW
  POST   /api/v1/deals/{id}/revise                      REJECTED -> DRAFT

Curator (DEAL_CURATOR or PLATFORM_ADMIN):
  GET    /api/v1/admin/deals/pending                    list PENDING_REVIEW
  POST   /api/v1/admin/deals                            create at APPROVED
  POST   /api/v1/admin/deals/{id}/approve               PENDING_REVIEW -> APPROVED
  POST   /api/v1/admin/deals/{id}/reject                PENDING_REVIEW -> REJECTED

Public (any authenticated, or anonymous for PUBLIC-audience only):
  GET    /api/v1/restaurants/{rid}/deals/active         public active listing
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps.auth import get_current_user, optional_user, require_role
from app.models.deal import Deal
from app.models.enums import (
    DealAudience,
    DealStatus,
    DealType,
    UserRole,
)
from app.models.user import User
from app.services import deals as deal_service
from app.services import restaurant_service
from app.services.restaurant_service import get_or_404 as get_restaurant_or_404

router = APIRouter(tags=["deals"])
admin_router = APIRouter(prefix="/admin/deals", tags=["deals"])


# ---- DTOs ----
class CreateDealIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    deal_type: DealType
    discount_value: Optional[Decimal] = None
    start_date: date
    end_date: date
    target_audience: DealAudience = DealAudience.PUBLIC


class UpdateDealIn(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    deal_type: Optional[DealType] = None
    discount_value: Optional[Decimal] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    target_audience: Optional[DealAudience] = None


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


class DealOut(BaseModel):
    id: uuid.UUID
    restaurant_id: uuid.UUID
    created_by: uuid.UUID
    curator_created: bool
    title: str
    description: Optional[str] = None
    deal_type: str
    discount_value: Optional[Decimal] = None
    start_date: date
    end_date: date
    status: str
    target_audience: str
    reviewed_by_curator_id: Optional[uuid.UUID] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


def _to_out(d: Deal) -> DealOut:
    return DealOut(
        id=d.id, restaurant_id=d.restaurant_id, created_by=d.created_by,
        curator_created=d.curator_created,
        title=d.title, description=d.description,
        deal_type=d.deal_type.value if hasattr(d.deal_type, "value") else str(d.deal_type),
        discount_value=d.discount_value,
        start_date=d.start_date, end_date=d.end_date,
        status=d.status,
        target_audience=d.target_audience.value if hasattr(d.target_audience, "value") else str(d.target_audience),
        reviewed_by_curator_id=d.reviewed_by_curator_id,
        reviewed_at=d.reviewed_at,
        rejection_reason=d.rejection_reason,
        created_at=d.created_at, updated_at=d.updated_at,
    )


def _to_input(body: CreateDealIn | UpdateDealIn) -> deal_service.CreateDealInput:
    """Helper for the create path. Update path is handled inline."""
    if not isinstance(body, CreateDealIn):
        raise HTTPException(status_code=400, detail="internal: expected CreateDealIn")
    return deal_service.CreateDealInput(
        title=body.title, description=body.description, deal_type=body.deal_type,
        discount_value=body.discount_value, start_date=body.start_date,
        end_date=body.end_date, target_audience=body.target_audience,
    )


# ---- Owner ----
@router.post(
    "/restaurants/{restaurant_id}/deals",
    response_model=DealOut,
    status_code=status.HTTP_201_CREATED,
)
async def owner_create_deal(
    restaurant_id: uuid.UUID,
    body: CreateDealIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_role(UserRole.RESTAURANT_OWNER, UserRole.PLATFORM_ADMIN))],
) -> DealOut:
    r = await get_restaurant_or_404(db, restaurant_id)
    is_admin = actor.role == UserRole.PLATFORM_ADMIN
    restaurant_service._ensure_owner_or_admin(r, actor, is_admin=is_admin)
    deal = await deal_service.create_draft(db, restaurant=r, creator=actor, inp=_to_input(body))
    return _to_out(deal)


@router.get("/restaurants/{restaurant_id}/deals", response_model=list[DealOut])
async def list_owner_deals(
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> list[DealOut]:
    is_admin = actor.role == UserRole.PLATFORM_ADMIN
    rows = await deal_service.list_for_restaurant_owner(
        db, restaurant_id=restaurant_id, owner_id=actor.id, is_admin=is_admin,
    )
    return [_to_out(d) for d in rows]


@router.get("/deals/{deal_id}", response_model=DealOut)
async def get_deal(
    deal_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> DealOut:
    deal = await deal_service.get_or_404(db, deal_id)
    # Owner can see their own (any state); curator/admin can see any.
    is_admin = actor.role in (UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR)
    if not is_admin and deal.created_by != actor.id:
        raise HTTPException(status_code=403, detail="not your deal")
    return _to_out(deal)


@router.put("/deals/{deal_id}", response_model=DealOut)
async def update_deal(
    deal_id: uuid.UUID,
    body: UpdateDealIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> DealOut:
    deal = await deal_service.get_or_404(db, deal_id)
    deal = await deal_service.update_draft(
        db, deal=deal, actor=actor,
        title=body.title, description=body.description,
        deal_type=body.deal_type, discount_value=body.discount_value,
        start_date=body.start_date, end_date=body.end_date,
        target_audience=body.target_audience,
    )
    return _to_out(deal)


@router.post("/deals/{deal_id}/submit", response_model=DealOut)
async def submit_deal(
    deal_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> DealOut:
    deal = await deal_service.get_or_404(db, deal_id)
    deal = await deal_service.submit(db, deal=deal, actor=actor)
    return _to_out(deal)


@router.post("/deals/{deal_id}/revise", response_model=DealOut)
async def revise_deal(
    deal_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> DealOut:
    deal = await deal_service.get_or_404(db, deal_id)
    deal = await deal_service.revise(db, deal=deal, actor=actor)
    return _to_out(deal)


# ---- Public ----
@router.get("/restaurants/{restaurant_id}/deals/active", response_model=list[DealOut])
async def list_active_deals(
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[Optional[User], Depends(optional_user)] = None,
) -> list[DealOut]:
    rows = await deal_service.list_active_for_restaurant(
        db, restaurant_id=restaurant_id, viewer_id=(actor.id if actor else None),
    )
    return [_to_out(d) for d in rows]


# ---- Curator / admin ----
@admin_router.get("/pending", response_model=list[DealOut])
async def list_pending(
    db: Annotated[AsyncSession, Depends(get_db)],
    _curator: Annotated[User, Depends(require_role(UserRole.DEAL_CURATOR, UserRole.PLATFORM_ADMIN))],
) -> list[DealOut]:
    rows = await deal_service.list_pending_for_curator(db)
    return [_to_out(d) for d in rows]


@admin_router.post(
    "/create",
    response_model=DealOut,
    status_code=status.HTTP_201_CREATED,
)
async def curator_create_deal_for(
    restaurant_id: uuid.UUID,
    body: CreateDealIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    curator: Annotated[User, Depends(require_role(UserRole.DEAL_CURATOR, UserRole.PLATFORM_ADMIN))],
) -> DealOut:
    r = await get_restaurant_or_404(db, restaurant_id)
    deal = await deal_service.create_hand_curated(
        db, restaurant=r, curator=curator, inp=_to_input(body),
    )
    return _to_out(deal)


@admin_router.post("/{deal_id}/approve", response_model=DealOut)
async def approve_deal(
    deal_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    curator: Annotated[User, Depends(require_role(UserRole.DEAL_CURATOR, UserRole.PLATFORM_ADMIN))],
) -> DealOut:
    deal = await deal_service.get_or_404(db, deal_id)
    deal = await deal_service.approve(db, deal=deal, curator=curator)
    # Stage 9: fan out notifications on approve.
    # - Web push: every diner subscribed to this restaurant gets a push.
    # - Marketing email: every diner with an account gets a "new deal"
    #   email (best-effort; a Phase 2 task is per-restaurant email opt-in).
    from app.models.restaurant import Restaurant as _Restaurant
    from app.models.user import User as _User
    from app.services import push as push_service
    from app.services.email import send_new_deal_alert
    from app.services.sharing import deal_share_url
    from sqlalchemy import select as _select
    restaurant = await db.get(_Restaurant, deal.restaurant_id)
    if restaurant is not None:
        await push_service.notify_deal_approved(
            db, deal=deal, restaurant=restaurant,
        )
        all_diners = list((await db.execute(
            _select(_User).where(_User.role == UserRole.DINER.value)
        )).scalars().all())
        for u in all_diners:
            send_new_deal_alert(
                email_addr=u.email,
                deal_title=deal.title,
                restaurant_name=restaurant.name,
                share_url=deal_share_url(deal.id),
            )
    return _to_out(deal)


@admin_router.post("/{deal_id}/reject", response_model=DealOut)
async def reject_deal(
    deal_id: uuid.UUID,
    body: RejectIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    curator: Annotated[User, Depends(require_role(UserRole.DEAL_CURATOR, UserRole.PLATFORM_ADMIN))],
) -> DealOut:
    deal = await deal_service.get_or_404(db, deal_id)
    deal = await deal_service.reject(db, deal=deal, curator=curator, reason=body.reason)
    return _to_out(deal)
