"""Points, referrals, gift cards API (Stage 8).

Routes:
  GET  /api/v1/users/me/referral-code           (any user; returns their code)
  GET  /api/v1/users/me/points                  (balance + recent transactions)
  POST /api/v1/users/me/checkins                (diner checks in; 200 points; 1/day/restaurant)
  POST /api/v1/users/me/gift-card-redemptions   (request; pending_fulfillment)
  GET  /api/v1/users/me/gift-card-redemptions   (list own)
  POST /api/v1/admin/gift-card-redemptions/{id}/fulfill   (admin; sets external_ref)
  POST /api/v1/admin/gift-card-redemptions/{id}/fail      (admin; sets reason)
  POST /api/v1/admin/users/{id}/verify-email              (admin; sets email_verified=true → A trigger fires)
  PATCH /api/v1/admin/settings/points-referral-on-first-review  (admin; toggles C trigger)
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.deps.auth import get_current_user, require_role
from app.models.enums import UserRole
from app.models.points import Checkin, GiftCardRedemption
from app.models.user import User
from app.services import gift_cards as gift_cards_service
from app.services import points as points_service
from app.services import referrals as referrals_service

router = APIRouter(prefix="/users", tags=["points"])
admin_router = APIRouter(prefix="/admin", tags=["points-admin"])


# ---- DTOs ----
class ReferralCodeOut(BaseModel):
    referral_code: str
    referral_url: str  # convenience: full URL the user can share


class TransactionOut(BaseModel):
    id: uuid.UUID
    type: str
    amount: int
    reference_id: uuid.UUID
    note: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class PointsOut(BaseModel):
    balance: int
    min_redemption: int
    can_redeem: bool
    recent_transactions: list[TransactionOut]


class CheckinOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    restaurant_id: uuid.UUID
    checkin_at: datetime
    points_awarded: int


class CheckinIn(BaseModel):
    restaurant_id: uuid.UUID


class RedemptionRequestIn(BaseModel):
    points_amount: int = Body(..., ge=1)


class RedemptionOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    points_spent: int
    status: str
    external_ref: Optional[str] = None
    fulfillment_note: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    fulfilled_at: Optional[datetime] = None


class FulfillIn(BaseModel):
    external_ref: str = Field(min_length=1, max_length=200)
    note: Optional[str] = None


class FailIn(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


class ReferralOnFirstReviewToggleIn(BaseModel):
    enabled: bool


# ---- User: referral code ----
@router.get("/me/referral-code", response_model=ReferralCodeOut)
async def get_my_referral_code(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> ReferralCodeOut:
    code = await referrals_service.get_or_create_referral_code(db, actor)
    return ReferralCodeOut(
        referral_code=code,
        referral_url=f"{settings.app_public_url}/auth/register?ref={code}",
    )


# ---- User: points balance + recent transactions ----
@router.get("/me/points", response_model=PointsOut)
async def get_my_points(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> PointsOut:
    balance = await points_service.get_balance(db, actor.id)
    recent = await points_service.get_recent_transactions(db, actor.id, limit=20)
    min_redemption = int(settings.points_values.get("min_redemption", 0))
    return PointsOut(
        balance=balance,
        min_redemption=min_redemption,
        can_redeem=balance >= min_redemption,
        recent_transactions=[
            TransactionOut(
                id=t.id, type=t.type, amount=t.amount, reference_id=t.reference_id,
                note=t.note, created_at=t.created_at,
            )
            for t in recent
        ],
    )


# ---- User: checkin (1/day/restaurant, 200 points) ----
@router.post(
    "/me/checkins",
    response_model=CheckinOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_checkin(
    body: CheckinIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> CheckinOut:
    today = datetime.now(timezone.utc).date()
    checkin = Checkin(
        id=uuid.uuid4(),
        user_id=actor.id,
        restaurant_id=body.restaurant_id,
        checkin_date=today,
    )
    db.add(checkin)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="you've already checked in at this restaurant today",
        )
    # Award points (idempotent via ledger UNIQUE on (user, type, ref)).
    txn = await points_service.credit_for_checkin(
        db, user_id=actor.id, checkin_id=checkin.id,
    )
    await db.commit()
    await db.refresh(checkin)
    return CheckinOut(
        id=checkin.id, user_id=checkin.user_id, restaurant_id=checkin.restaurant_id,
        checkin_at=checkin.checkin_at, points_awarded=txn.amount,
    )


# ---- User: gift card redemption ----
@router.post(
    "/me/gift-card-redemptions",
    response_model=RedemptionOut,
    status_code=status.HTTP_201_CREATED,
)
async def request_gift_card_redemption(
    body: RedemptionRequestIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> RedemptionOut:
    r = await gift_cards_service.request_redemption(
        db, user=actor, points_amount=body.points_amount,
    )
    return _to_redemption_out(r)


@router.get("/me/gift-card-redemptions", response_model=list[RedemptionOut])
async def list_my_redemptions(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> list[RedemptionOut]:
    rows = await gift_cards_service.list_for_user(db, actor.id)
    return [_to_redemption_out(r) for r in rows]


# ---- Admin: gift card fulfill / fail ----
@admin_router.post(
    "/gift-card-redemptions/{redemption_id}/fulfill",
    response_model=RedemptionOut,
)
async def admin_fulfill_redemption(
    redemption_id: uuid.UUID,
    body: FulfillIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> RedemptionOut:
    redemption = await db.get(GiftCardRedemption, redemption_id)
    if redemption is None:
        raise HTTPException(status_code=404, detail="redemption not found")
    r = await gift_cards_service.fulfill(
        db, redemption=redemption, admin=admin,
        external_ref=body.external_ref, note=body.note,
    )
    return _to_redemption_out(r)


@admin_router.post(
    "/gift-card-redemptions/{redemption_id}/fail",
    response_model=RedemptionOut,
)
async def admin_fail_redemption(
    redemption_id: uuid.UUID,
    body: FailIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> RedemptionOut:
    redemption = await db.get(GiftCardRedemption, redemption_id)
    if redemption is None:
        raise HTTPException(status_code=404, detail="redemption not found")
    r = await gift_cards_service.fail(
        db, redemption=redemption, admin=admin, reason=body.reason,
    )
    return _to_redemption_out(r)


# ---- Admin: verify user email → fires A trigger ----
@admin_router.post(
    "/users/{user_id}/verify-email",
)
async def admin_verify_user_email(
    user_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> dict:
    """Mark a user's email as verified. Fires the A trigger for any
    pending referral credit. In production this will be wired to the
    real email-verification pipeline (Open Item #6); the admin endpoint
    is a manual backstop for MVP.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if not user.email_verified:
        user.email_verified = True
        await db.commit()
    # Fire the A trigger (which is also the C-gate check).
    credited = await referrals_service.credit_referral_if_eligible(db, referred_user_id=user_id)
    return {"email_verified": True, "referral_credited": bool(credited)}


# ---- Admin: toggle the C trigger (first-approved-review referral) ----
@admin_router.patch(
    "/settings/points-referral-on-first-review",
)
async def admin_toggle_points_referral_on_first_review(
    body: ReferralOnFirstReviewToggleIn,
) -> dict:
    """Flip the C trigger on or off. NOTE: this only updates the
    in-process settings singleton — a server restart resets to the env
    var. For a permanent flip, change the env var
    `POINTS_REFERRAL_CREDIT_ON_FIRST_REVIEW` in the deployment config.
    """
    settings.points_referral_credit_on_first_review = body.enabled
    return {"enabled": bool(settings.points_referral_credit_on_first_review)}


# ---- helpers ----
def _to_redemption_out(r: GiftCardRedemption) -> RedemptionOut:
    return RedemptionOut(
        id=r.id, user_id=r.user_id, points_spent=r.points_spent,
        status=r.status, external_ref=r.external_ref,
        fulfillment_note=r.fulfillment_note,
        created_at=r.created_at, updated_at=r.updated_at,
        fulfilled_at=r.fulfilled_at,
    )
