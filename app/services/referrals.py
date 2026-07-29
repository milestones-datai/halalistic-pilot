"""Referrals service — code generation + dual-trigger crediting (Stage 8).

Two qualifying events for "successful signup", per founder direction:
  - A (always on): the new user is `email_verified=True`. Currently a
    no-op because email verification isn't built yet (Open Item #6).
    Lights up automatically once that flow lands.
  - C (admin-toggleable via POINTS_REFERRAL_CREDIT_ON_FIRST_REVIEW or
    the admin endpoint): the new user has their FIRST APPROVED REVIEW.

Either event fires the referrer credit (idempotent — duplicate
attempts no-op via the ledger UNIQUE).
"""
from __future__ import annotations

import logging
import secrets
import string
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.review import Review
from app.models.enums import ReviewStatus
from app.models.user import User
from app.services import points as points_service

logger = logging.getLogger("halalistic.referrals")

_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LEN = 8


def _new_code() -> str:
    """8-char random code, e.g. 'K3F9P2X1'. Stripe-style readability
    (no 0/O, no 1/I) would be nicer but is overkill for MVP — collisions
    are astronomically unlikely at 8 chars of 36 symbols.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))


async def get_or_create_referral_code(db: AsyncSession, user: User) -> str:
    """Idempotent: if the user already has a code, return it. Otherwise
    generate a new one and persist. The DB UNIQUE on `users.referral_code`
    means we may collide; on collision we retry.
    """
    if user.referral_code:
        return user.referral_code
    for _ in range(5):  # 5 retries in the astronomically unlikely race
        code = _new_code()
        existing = await db.scalar(select(User).where(User.referral_code == code))
        if existing is None:
            user.referral_code = code
            await db.commit()
            await db.refresh(user)
            return code
    # 5 consecutive collisions = we have astronomical bad luck; raise.
    raise HTTPException(status_code=500, detail="could not generate a unique referral code")


async def attach_referrer_on_register(
    db: AsyncSession, *, new_user: User, ref_code: Optional[str]
) -> None:
    """Called from /auth/register when a `?ref=...` query param is
    supplied. If the code is valid, set the new user's
    `referred_by_user_id`. No points credit here — credit fires when
    one of the A/C triggers succeeds.
    """
    if not ref_code:
        return
    if new_user.referred_by_user_id is not None:
        return  # already set
    referrer = await db.scalar(select(User).where(User.referral_code == ref_code))
    if referrer is None:
        logger.info("ignoring unknown referral code: %r", ref_code)
        return
    if referrer.id == new_user.id:
        logger.info("ignoring self-referral: %r", ref_code)
        return
    new_user.referred_by_user_id = referrer.id
    await db.commit()


async def credit_referral_if_eligible(
    db: AsyncSession, *, referred_user_id: uuid.UUID
) -> bool:
    """The A + C trigger entry-point. Called when:
      - A fires: someone marks the user's email as verified.
      - C fires: the user's first review is approved.
    Returns True if a credit was issued (or was previously issued).

    Idempotent: if the ledger already has a referral row for
    (referrer, referred_user_id), `_record_transaction` is a no-op and
    returns the existing row.
    """
    user = await db.get(User, referred_user_id)
    if user is None or user.referred_by_user_id is None:
        return False
    # A trigger: email_verified (always considered; if it's True we
    # credit, otherwise we fall through to the C check).
    a_eligible = bool(user.email_verified)
    # C trigger: admin-toggleable; off by default.
    has_review = await _user_has_approved_review(db, referred_user_id)
    c_eligible = bool(settings.points_referral_credit_on_first_review) and has_review
    if not (a_eligible or c_eligible):
        return False
    await points_service.credit_for_referral(
        db, referrer_id=user.referred_by_user_id, referred_user_id=referred_user_id,
    )
    logger.info("referral credit issued: referrer=%s referred=%s (A=%s C=%s)",
                user.referred_by_user_id, referred_user_id, a_eligible, c_eligible)
    return True


async def _user_has_approved_review(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Helper for the C trigger. True if the user has at least one
    APPROVED review.
    """
    row = await db.scalar(
        select(Review.id).where(
            Review.reviewer_id == user_id,
            Review.moderation_status == ReviewStatus.APPROVED.value,
        ).limit(1)
    )
    return row is not None
