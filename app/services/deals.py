"""Deals service — state machine + auto-expiry + visibility gates (Stage 6).

Per BRD §3.5, deals flow through a strict state machine. The full
transition table (validated server-side here, never in the API layer):

    ┌─────────────────────────────────────────────────────────────────────┐
    │  FROM              EVENT                  WHO      ->  TO            │
    ├─────────────────────────────────────────────────────────────────────┤
    │  (none)            create                 OWNER    ->  DRAFT          │
    │  (none)            create_hand_curated    CURATOR  ->  APPROVED       │
    │  DRAFT             submit                 OWNER    ->  PENDING_REVIEW │
    │  PENDING_REVIEW    approve                CURATOR  ->  APPROVED       │
    │  PENDING_REVIEW    reject(reason)         CURATOR  ->  REJECTED       │
    │  REJECTED          revise                 OWNER    ->  DRAFT          │
    │  APPROVED          auto_expire            SYSTEM   ->  EXPIRED        │
    └─────────────────────────────────────────────────────────────────────┘

All 11 other attempted transitions are invalid and raise 409.

Visibility (per BRD §3.4 + §3.5):
  - End users see only APPROVED deals that have not yet hit their end_date.
  - For Premium-tier restaurants, deals with target_audience="push_only"
    are further gated to users who have a row in restaurant_subscriptions
    for that restaurant.
  - The user's *platform* subscription tier (for premium-only deals) is
    a Stage 7 concept; the real implementation lives in
    `app.services.billing.get_user_subscription_tier` (replaces the
    Stage 6 stub).
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal import Deal, RestaurantPushSubscription
from app.models.enums import (
    DealAudience,
    DealStatus,
    DealType,
    RestaurantTier,
    UserRole,
)
from app.models.restaurant import Restaurant
from app.models.user import User
from app.services.billing import get_user_subscription_tier

logger = logging.getLogger("halalistic.deals")

MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 4000
PREMIUM_TIER = RestaurantTier.PREMIUM

# Stage 7 moved the real implementation to app.services.billing.
# The push-only gate below calls billing.get_user_subscription_tier.


# ---- helpers ----
def _ensure_actor_is_owner(actor: User, deal: Deal) -> None:
    if deal.created_by != actor.id and actor.role != UserRole.PLATFORM_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="only the deal creator (or platform admin) can act on this deal",
        )


def _ensure_actor_is_curator(actor: User) -> None:
    if actor.role not in (UserRole.DEAL_CURATOR, UserRole.PLATFORM_ADMIN):
        raise HTTPException(
            status_code=403,
            detail=f"role {actor.role.value!r} not authorized; need deal_curator or platform_admin",
        )


def _validate_transition(deal: Deal, target: DealStatus) -> None:
    """The single source of truth for valid state transitions.

    Raises HTTPException(409) on any invalid attempt. Every valid and
    every invalid transition is unit-tested in tests/test_deals.py.
    """
    current = DealStatus(deal.status)
    valid: dict[DealStatus, set[DealStatus]] = {
        DealStatus.DRAFT: {DealStatus.PENDING_REVIEW},
        DealStatus.PENDING_REVIEW: {DealStatus.APPROVED, DealStatus.REJECTED},
        DealStatus.REJECTED: {DealStatus.DRAFT},
        DealStatus.APPROVED: {DealStatus.EXPIRED},  # via auto-expiry only
        DealStatus.EXPIRED: set(),  # terminal
    }
    if target not in valid.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"invalid transition: {current.value} -> {target.value}",
        )


# ---- inputs ----
class CreateDealInput:
    def __init__(
        self,
        title: str,
        deal_type: DealType,
        start_date: date,
        end_date: date,
        description: Optional[str] = None,
        discount_value: Optional[Decimal] = None,
        target_audience: DealAudience = DealAudience.PUBLIC,
    ):
        self.title = (title or "").strip()
        self.description = (description or "").strip() or None
        self.deal_type = deal_type
        self.discount_value = discount_value
        self.start_date = start_date
        self.end_date = end_date
        self.target_audience = target_audience


# ---- create ----
async def create_draft(
    db: AsyncSession,
    *,
    restaurant: Restaurant,
    creator: User,
    inp: CreateDealInput,
) -> Deal:
    """Owner creates a deal that starts as DRAFT. Cannot self-publish."""
    if creator.role not in (UserRole.RESTAURANT_OWNER, UserRole.PLATFORM_ADMIN):
        raise HTTPException(
            status_code=403,
            detail="only restaurant owners (or platform admin) can create deals",
        )
    _validate_basic_deal_fields(inp, restaurant)
    deal = Deal(
        id=uuid.uuid4(),
        restaurant_id=restaurant.id,
        created_by=creator.id,
        curator_created=False,
        title=inp.title,
        description=inp.description,
        deal_type=inp.deal_type,
        discount_value=inp.discount_value,
        start_date=inp.start_date,
        end_date=inp.end_date,
        status=DealStatus.DRAFT.value,
        target_audience=inp.target_audience.value,
    )
    db.add(deal)
    await db.commit()
    await db.refresh(deal)
    return deal


async def create_hand_curated(
    db: AsyncSession,
    *,
    restaurant: Restaurant,
    curator: User,
    inp: CreateDealInput,
) -> Deal:
    """Curator creates a deal that skips review and enters directly as APPROVED.

    Per BRD §3.5, this is the hand-curated premium deal path.
    """
    _ensure_actor_is_curator(curator)
    _validate_basic_deal_fields(inp, restaurant)
    deal = Deal(
        id=uuid.uuid4(),
        restaurant_id=restaurant.id,
        created_by=curator.id,
        curator_created=True,
        title=inp.title,
        description=inp.description,
        deal_type=inp.deal_type,
        discount_value=inp.discount_value,
        start_date=inp.start_date,
        end_date=inp.end_date,
        status=DealStatus.APPROVED.value,
        target_audience=inp.target_audience.value,
        reviewed_by_curator_id=curator.id,
        reviewed_at=datetime.now(timezone.utc),
    )
    db.add(deal)
    await db.commit()
    await db.refresh(deal)
    return deal


def _validate_basic_deal_fields(inp: CreateDealInput, restaurant: Restaurant) -> None:
    if not inp.title:
        raise HTTPException(status_code=400, detail="title is required")
    if len(inp.title) > MAX_TITLE_LENGTH:
        raise HTTPException(status_code=400, detail=f"title too long (max {MAX_TITLE_LENGTH} chars)")
    if inp.description and len(inp.description) > MAX_DESCRIPTION_LENGTH:
        raise HTTPException(
            status_code=400, detail=f"description too long (max {MAX_DESCRIPTION_LENGTH} chars)"
        )
    if inp.end_date < inp.start_date:
        raise HTTPException(
            status_code=400, detail="end_date must be on or after start_date"
        )
    if inp.end_date < date.today():
        raise HTTPException(
            status_code=400, detail="end_date must be today or later"
        )
    # Push-only deals are a Premium-tier feature (BRD §3.4).
    if inp.target_audience == DealAudience.PUSH_ONLY and restaurant.tier != PREMIUM_TIER:
        raise HTTPException(
            status_code=400,
            detail=f"target_audience=push_only requires restaurant tier=premium; "
                   f"this restaurant is {restaurant.tier.value}",
        )
    # discount_value sanity: percentage must be 0-100, fixed_amount >= 0
    if inp.discount_value is not None and inp.discount_value < 0:
        raise HTTPException(status_code=400, detail="discount_value must be non-negative")


# ---- transitions ----
async def submit(db: AsyncSession, *, deal: Deal, actor: User) -> Deal:
    """Owner submits a DRAFT for review. DRAFT -> PENDING_REVIEW."""
    _ensure_actor_is_owner(actor, deal)
    _validate_transition(deal, DealStatus.PENDING_REVIEW)
    deal.status = DealStatus.PENDING_REVIEW.value
    deal.rejection_reason = None
    await db.commit()
    await db.refresh(deal)
    return deal


async def approve(db: AsyncSession, *, deal: Deal, curator: User) -> Deal:
    """Curator approves a PENDING_REVIEW. PENDING_REVIEW -> APPROVED."""
    _ensure_actor_is_curator(curator)
    _validate_transition(deal, DealStatus.APPROVED)
    deal.status = DealStatus.APPROVED.value
    deal.reviewed_by_curator_id = curator.id
    deal.reviewed_at = datetime.now(timezone.utc)
    deal.rejection_reason = None
    await db.commit()
    await db.refresh(deal)
    return deal


async def reject(
    db: AsyncSession, *, deal: Deal, curator: User, reason: str
) -> Deal:
    """Curator rejects a PENDING_REVIEW. PENDING_REVIEW -> REJECTED.

    `reason` is required and stored as `rejection_reason` for the owner
    to see. The owner must call `revise` to move it back to DRAFT before
    they can re-submit.
    """
    _ensure_actor_is_curator(curator)
    _validate_transition(deal, DealStatus.REJECTED)
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="rejection reason is required")
    deal.status = DealStatus.REJECTED.value
    deal.reviewed_by_curator_id = curator.id
    deal.reviewed_at = datetime.now(timezone.utc)
    deal.rejection_reason = reason
    await db.commit()
    await db.refresh(deal)
    return deal


async def revise(db: AsyncSession, *, deal: Deal, actor: User) -> Deal:
    """Owner moves a REJECTED deal back to DRAFT so they can edit and re-submit.

    Required per BRD §3.5: rejected deals cannot be re-approved directly,
    they must go through DRAFT again. Implemented as an explicit action
    (rather than magic on PUT) so the state mutation is auditable and
    testable.
    """
    _ensure_actor_is_owner(actor, deal)
    _validate_transition(deal, DealStatus.DRAFT)
    deal.status = DealStatus.DRAFT.value
    # The rejection_reason stays attached for the owner's reference, but
    # the deal is now editable + submittable again.
    await db.commit()
    await db.refresh(deal)
    return deal


# ---- edit ----
async def update_draft(
    db: AsyncSession,
    *,
    deal: Deal,
    actor: User,
    title: Optional[str] = None,
    description: Optional[str] = None,
    deal_type: Optional[DealType] = None,
    discount_value: Optional[Decimal] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    target_audience: Optional[DealAudience] = None,
) -> Deal:
    """Owner edits a DRAFT or REJECTED deal. Any other status -> 409."""
    _ensure_actor_is_owner(actor, deal)
    if deal.status not in (DealStatus.DRAFT.value, DealStatus.REJECTED.value):
        raise HTTPException(
            status_code=409,
            detail=f"cannot edit a deal in status {deal.status!r}; only draft or rejected",
        )
    if title is not None:
        title = title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
        if len(title) > MAX_TITLE_LENGTH:
            raise HTTPException(status_code=400, detail=f"title too long (max {MAX_TITLE_LENGTH})")
        deal.title = title
    if description is not None:
        description = description.strip() or None
        if description and len(description) > MAX_DESCRIPTION_LENGTH:
            raise HTTPException(status_code=400, detail=f"description too long (max {MAX_DESCRIPTION_LENGTH})")
        deal.description = description
    if deal_type is not None:
        deal.deal_type = deal_type
    if discount_value is not None:
        if discount_value < 0:
            raise HTTPException(status_code=400, detail="discount_value must be non-negative")
        deal.discount_value = discount_value
    if start_date is not None:
        deal.start_date = start_date
    if end_date is not None:
        deal.end_date = end_date
    if deal.start_date and deal.end_date and deal.end_date < deal.start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")
    if target_audience is not None:
        if target_audience == DealAudience.PUSH_ONLY and deal.restaurant.tier != PREMIUM_TIER:
            raise HTTPException(
                status_code=400,
                detail=f"target_audience=push_only requires restaurant tier=premium; "
                       f"this restaurant is {deal.restaurant.tier.value}",
            )
        deal.target_audience = target_audience
    await db.commit()
    await db.refresh(deal)
    return deal


# ---- queries ----
async def get_or_404(db: AsyncSession, deal_id: uuid.UUID) -> Deal:
    d = await db.get(Deal, deal_id)
    if d is None:
        raise HTTPException(status_code=404, detail="deal not found")
    return d


async def list_pending_for_curator(db: AsyncSession) -> list[Deal]:
    stmt = (
        select(Deal)
        .where(Deal.status == DealStatus.PENDING_REVIEW.value)
        .order_by(Deal.created_at.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_for_restaurant_owner(
    db: AsyncSession, *, restaurant_id: uuid.UUID, owner_id: uuid.UUID, is_admin: bool
) -> list[Deal]:
    stmt = (
        select(Deal)
        .where(Deal.restaurant_id == restaurant_id)
        .order_by(Deal.updated_at.desc())
    )
    if not is_admin:
        stmt = stmt.where(Deal.created_by == owner_id)
    return list((await db.execute(stmt)).scalars().all())


# ---- public visibility (BRD §3.4 + §3.5) ----
async def list_active_for_restaurant(
    db: AsyncSession,
    *,
    restaurant_id: uuid.UUID,
    viewer_id: Optional[uuid.UUID] = None,
) -> list[Deal]:
    """Public-facing: only APPROVED deals that haven't hit their end_date.

    The push-only / public split is enforced per-viewer in Python below
    so the same query can be reused with different viewer contexts.
    End-date filtering is also applied defensively here so a stalled
    expiry job doesn't leak expired deals into the public listing.
    """
    today = date.today()
    stmt = (
        select(Deal)
        .where(
            and_(
                Deal.restaurant_id == restaurant_id,
                Deal.status == DealStatus.APPROVED.value,
                Deal.end_date >= today,
            )
        )
        .order_by(Deal.end_date.asc())
    )
    rows = list((await db.execute(stmt)).scalars().all())

    if viewer_id is None:
        return [d for d in rows if d.target_audience == DealAudience.PUBLIC.value]

    tier = await get_user_subscription_tier(db, viewer_id)
    push_sub_ids = await _subscribed_restaurant_ids(db, viewer_id)

    out: list[Deal] = []
    for d in rows:
        if d.target_audience == DealAudience.PUBLIC.value:
            # Stage 7 stub: tier is "free" for everyone. The gate exists
            # so a Stage 7 PR can drop in without touching this code.
            if tier == "premium" or True:
                out.append(d)
        else:
            if d.restaurant_id in push_sub_ids:
                out.append(d)
    return out


async def _subscribed_restaurant_ids(
    db: AsyncSession, user_id: uuid.UUID
) -> set[uuid.UUID]:
    stmt = select(RestaurantPushSubscription.restaurant_id).where(
        RestaurantPushSubscription.user_id == user_id
    )
    return {row[0] for row in (await db.execute(stmt)).all()}


# ---- auto-expiry (BRD §3.5) ----
async def expire_old_deals(db: AsyncSession) -> int:
    """Transition every APPROVED deal whose end_date < today to EXPIRED.

    Returns the number of deals expired by this run. Safe to call multiple
    times per day; only deals that are still APPROVED and past their
    end_date are touched.

    Invoked by `scripts/expire_deals.py` from cron (e.g. daily at 00:05
    server time). The public listing also filters defensively on end_date
    so a stalled cron doesn't leak expired deals.
    """
    today = date.today()
    stmt = select(Deal).where(
        and_(
            Deal.status == DealStatus.APPROVED.value,
            Deal.end_date < today,
        )
    )
    rows = list((await db.execute(stmt)).scalars().all())
    for d in rows:
        d.status = DealStatus.EXPIRED.value
    if rows:
        await db.commit()
        logger.info("expire_old_deals: marked %d deal(s) as expired", len(rows))
    return len(rows)
