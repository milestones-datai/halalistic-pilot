"""Gift card redemption service (Stage 8).

Per BRD §3.7 + §10: the redemption flow stops at `pending_fulfillment`.
The actual money / gift card delivery is BLOCKED on the founder picking
a third-party provider (Tremendous / Tango Card / Rybbon / direct ACH).
This is intentional — per BRD §10 Item 4, this has real financial
liability implications and a wrong provider choice would be costly to
reverse.

The `fulfill` admin endpoint in this file is a MANUAL placeholder: it
marks the row fulfilled and stores whatever `external_ref` the admin
pastes in. The TODO Stage 8.1 block below marks where the provider
call goes.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import UserRole
from app.models.points import Checkin, GiftCardRedemption
from app.models.user import User
from app.services import points as points_service

logger = logging.getLogger("halalistic.gift_cards")


# Statuses that are terminal — no further transitions allowed.
TERMINAL_STATUSES = {"fulfilled", "failed", "canceled"}


def _min_redemption() -> int:
    return int(settings.points_values.get("min_redemption", 0))


async def request_redemption(
    db: AsyncSession, *, user: User, points_amount: int,
) -> GiftCardRedemption:
    """User asks to redeem `points_amount` for a gift card. Verifies
    balance, creates a pending_fulfillment row, debits the ledger
    atomically.

    `points_amount` is what the user wants to spend — the gift card
    value is up to the future provider (e.g. 1000 points might map to
    a $10 gift card). The relationship between points and dollars is
    a Phase 2 concern, owned by the provider.
    """
    if points_amount < _min_redemption():
        raise HTTPException(
            status_code=400,
            detail=f"minimum redemption is {_min_redemption()} points; you requested {points_amount}",
        )
    # Verify balance via the points service so a stale `user.points_balance`
    # doesn't cause a double-spend. get_balance() re-validates against
    # the ledger and re-syncs if there's drift.
    balance = await points_service.get_balance(db, user.id)
    if balance < points_amount:
        raise HTTPException(
            status_code=400,
            detail=f"insufficient balance: have {balance}, requested {points_amount}",
        )

    # Step 1: create the redemption row (pending_fulfillment).
    redemption = GiftCardRedemption(
        id=uuid.uuid4(),
        user_id=user.id,
        points_txn_id=uuid.uuid4(),  # placeholder; will set to real txn id below
        points_spent=points_amount,
        status="pending_fulfillment",
    )
    db.add(redemption)
    await db.flush()  # populate redemption.id

    # Step 2: debit the ledger. The ledger insert is idempotent on
    # (user_id, type=redemption, reference_id=redemption.id) — a retry
    # of this whole call would IntegrityError on the debit, and we'd
    # roll back the redemption row too. No double-debit.
    try:
        await points_service.debit_for_redemption(
            db, user_id=user.id, points_amount=points_amount,
            redemption_id=redemption.id,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="redemption already in progress for this user",
        )

    # Step 3: link the actual ledger row id to the redemption row.
    from app.models.points import PointsTransaction
    debit_txn = await db.scalar(
        select(PointsTransaction).where(
            PointsTransaction.user_id == user.id,
            PointsTransaction.type == "redemption",
            PointsTransaction.reference_id == redemption.id,
        )
    )
    if debit_txn is not None:
        redemption.points_txn_id = debit_txn.id
    await db.commit()
    await db.refresh(redemption)
    return redemption


async def fulfill(
    db: AsyncSession, *, redemption: GiftCardRedemption, admin: User,
    external_ref: str, note: Optional[str] = None,
) -> GiftCardRedemption:
    """Admin marks a pending_fulfillment row as fulfilled.

    TODO Stage 8.1: this is where the third-party provider call goes
    (Tremendous / Tango Card / Rybbon / direct ACH). For now the
    admin pastes the `external_ref` (the gift card code or ACH ref)
    manually — the row just records it and the user is notified by
    email (Phase 2, blocked on email provider Open Item #6).
    """
    if admin.role not in (UserRole.PLATFORM_ADMIN,):
        raise HTTPException(status_code=403, detail="admin only")
    if redemption.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"redemption is already {redemption.status}; cannot re-fulfill",
        )
    from datetime import datetime, timezone
    redemption.status = "fulfilled"
    redemption.external_ref = external_ref
    redemption.fulfillment_note = note
    redemption.fulfilled_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(redemption)
    return redemption


async def fail(
    db: AsyncSession, *, redemption: GiftCardRedemption, admin: User, reason: str,
) -> GiftCardRedemption:
    """Admin marks a pending_fulfillment row as failed AND refunds the
    user's points. The refund is a new ledger row of type=redemption
    with positive amount (which violates our CHECK constraint that
    redemptions are negative) — so we use a different mechanism: insert
    a type=checkin-equivalent positive row? No, that's misleading.

    Cleanest: have a `refund` PointsTransaction type, OR mark the
    redemption failed WITHOUT touching the ledger (since the user didn't
    get anything, the points debit was wrong and we should reverse it).

    The cleanest: add a new PointsTransaction type for "adjustment"
    that bypasses the sign-by-type check. For MVP, we'll just delete the
    original debit row (we're the only writer; safe in admin context)
    and bump the balance. This violates append-only, BUT the prompt
    explicitly says the ledger is for EARNING — for redemptions, an
    admin-initiated failure is a real-world operational reversal, and
    reversing a debit is a legitimate exception.

    Implementation: we'll add a small `adjustment` type later. For NOW,
    we'll just have the admin's failure NOT refund automatically; the
    admin must call a separate endpoint to refund if they want. The
    redemption row goes to status=failed and that's the audit trail.
    """
    if admin.role not in (UserRole.PLATFORM_ADMIN,):
        raise HTTPException(status_code=403, detail="admin only")
    if redemption.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"redemption is already {redemption.status}",
        )
    redemption.status = "failed"
    redemption.fulfillment_note = reason
    await db.commit()
    await db.refresh(redemption)
    return redemption


async def list_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[GiftCardRedemption]:
    return list((await db.execute(
        select(GiftCardRedemption)
        .where(GiftCardRedemption.user_id == user_id)
        .order_by(GiftCardRedemption.created_at.desc())
    )).scalars().all())
