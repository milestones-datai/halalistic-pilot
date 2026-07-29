"""Points service — the append-only ledger (Stage 8).

Per BRD §3.7 + §6: `PointsTransaction` is a ledger. Rows are never
mutated after insert. The `User.points_balance` column is a derived /
cached value; the source of truth is the SUM of the ledger. We revalidate
the cached value against the ledger on every read so the two can never
silently drift.

Single entry-point: `_record_transaction(user_id, type, amount,
reference_id, note)`. Atomic with the `User.points_balance` update.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.points import PointsTransaction
from app.models.user import User

logger = logging.getLogger("halalistic.points")


def _value(action: str) -> int:
    """Read a per-action point value from settings. No magic numbers in
    service code — every value is in `settings.points_values` and is
    env-overridable.
    """
    try:
        return int(settings.points_values[action])
    except KeyError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"point value {action!r} missing from settings.points_values",
        ) from exc


# ---------- The single atomic insert + balance update ----------
async def _record_transaction(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    type_: str,
    amount: int,
    reference_id: uuid.UUID,
    note: Optional[str] = None,
) -> PointsTransaction:
    """Append one ledger row and bump `user.points_balance` by `amount`.

    This is the ONLY function that inserts into `points_transactions`. It
    is the only place the user balance changes. There is intentionally
    no `update_transaction` helper.

    The DB-level UNIQUE on (user_id, type, reference_id) prevents
    double-crediting the same referrer / review / checkin. The application
    handles the IntegrityError by returning the existing row instead of
    failing — this is the idempotency the prompt calls for.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if amount == 0:
        raise HTTPException(status_code=400, detail="amount must be non-zero")

    txn = PointsTransaction(
        id=uuid.uuid4(),
        user_id=user_id,
        type=type_,
        amount=amount,
        reference_id=reference_id,
        note=note,
    )
    db.add(txn)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        # Idempotency: a row for (user_id, type, reference_id) already
        # exists. Fetch and return it so the caller can treat the call
        # as a no-op.
        existing = await db.scalar(
            select(PointsTransaction).where(
                PointsTransaction.user_id == user_id,
                PointsTransaction.type == type_,
                PointsTransaction.reference_id == reference_id,
            )
        )
        if existing is not None:
            return existing
        raise HTTPException(status_code=500, detail=f"ledger insert failed: {exc}")

    # Update the cached balance in the same transaction. The commit()
    # in the calling endpoint (or our own below) persists both rows
    # atomically.
    user.points_balance = (user.points_balance or 0) + amount
    await db.commit()
    await db.refresh(txn)
    return txn


# ---------- Convenience credit / debit helpers ----------
async def credit_for_referral(db: AsyncSession, *, referrer_id: uuid.UUID, referred_user_id: uuid.UUID) -> Optional[PointsTransaction]:
    """Idempotent. Returns the existing txn if already credited, else creates one.
    Returns None if referrer has no qualifying referral credit to give (i.e.
    the user has no referrer set).
    """
    if referrer_id == referred_user_id:
        return None
    amount = _value("referral")
    return await _record_transaction(
        db, user_id=referrer_id, type_="referral",
        amount=amount, reference_id=referred_user_id,
        note="referral signup",
    )


async def credit_for_review(db: AsyncSession, *, reviewer_id: uuid.UUID, review_id: uuid.UUID) -> PointsTransaction:
    """Credit the reviewer when their review is APPROVED. Never on
    submission (pre-moderation consistency — no points for spam).
    """
    amount = _value("review")
    return await _record_transaction(
        db, user_id=reviewer_id, type_="review",
        amount=amount, reference_id=review_id,
        note="review approved",
    )


async def credit_for_checkin(db: AsyncSession, *, user_id: uuid.UUID, checkin_id: uuid.UUID) -> PointsTransaction:
    """Credit the diner when they check in. The 1/day/restaurant cap is
    enforced at the DB level by the `checkins` UNIQUE constraint — the
    IntegrityError propagates up and the caller turns it into 409.
    """
    amount = _value("checkin")
    return await _record_transaction(
        db, user_id=user_id, type_="checkin",
        amount=amount, reference_id=checkin_id,
        note="restaurant checkin",
    )


async def debit_for_redemption(
    db: AsyncSession, *, user_id: uuid.UUID, points_amount: int, redemption_id: uuid.UUID,
) -> PointsTransaction:
    """Debit the user when they request a gift card redemption. Caller
    has already verified the user has enough balance (`_can_redeem`).
    Stored as a negative-amount row in the ledger.
    """
    if points_amount <= 0:
        raise HTTPException(status_code=400, detail="points_amount must be positive")
    return await _record_transaction(
        db, user_id=user_id, type_="redemption",
        amount=-points_amount, reference_id=redemption_id,
        note="gift card redemption",
    )


# ---------- Balance reconciliation ----------
async def recompute_balance(db: AsyncSession, user_id: uuid.UUID) -> int:
    """The source of truth: SUM(amount) for the user. Use this to
    reconcile the cached `User.points_balance` if you ever suspect drift
    (e.g. after a manual SQL fix or a future migration). Cheap; indexed
    on (user_id, created_at).
    """
    rows = (await db.execute(
        select(PointsTransaction.amount).where(PointsTransaction.user_id == user_id)
    )).scalars().all()
    return sum(rows)


async def get_balance(db: AsyncSession, user_id: uuid.UUID) -> int:
    """The read-time view. We revalidate the cached balance against the
    ledger on every read so the two can never silently drift. If they
    disagree, we log a warning and re-sync (the cached value is purely
    a perf shortcut, the ledger wins).
    """
    ledger_total = await recompute_balance(db, user_id)
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if (user.points_balance or 0) != ledger_total:
        logger.warning(
            "points balance drift for user %s: cached=%s ledger=%s — resyncing",
            user_id, user.points_balance, ledger_total,
        )
        user.points_balance = ledger_total
        await db.commit()
    return ledger_total


async def get_recent_transactions(
    db: AsyncSession, user_id: uuid.UUID, limit: int = 20,
) -> list[PointsTransaction]:
    return list((await db.execute(
        select(PointsTransaction)
        .where(PointsTransaction.user_id == user_id)
        .order_by(PointsTransaction.created_at.desc())
        .limit(limit)
    )).scalars().all())


# ---------- Redemption gate ----------
def _min_redemption() -> int:
    return int(settings.points_values.get("min_redemption", 0))


async def can_redeem(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """True if the user has at least the configured minimum balance.

    Note: amount requested is checked separately by the gift_cards
    service against the actual balance.
    """
    bal = await get_balance(db, user_id)
    return bal >= _min_redemption()


# ---------- Optional reconcile-everyone (admin) ----------
async def reconcile_all_users(db: AsyncSession) -> int:
    """Walk every user and re-sync `points_balance` from the ledger.
    Returns the number of users that needed a re-sync. Heavy; admin-only
    on the future ops dashboard.
    """
    from sqlalchemy import update as _update
    users = (await db.execute(select(User.id))).scalars().all()
    drift = 0
    for uid in users:
        ledger_total = await recompute_balance(db, uid)
        user = await db.get(User, uid)
        if (user.points_balance or 0) != ledger_total:
            user.points_balance = ledger_total
            drift += 1
    if drift:
        await db.commit()
    return drift
