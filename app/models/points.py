"""Points & referral & gift-card models (Stage 8).

Three entities per BRD §3.7 + §6:

  1. `PointsTransaction` — the canonical ledger. Append-only. NEVER
     mutated after insert. The User.points_balance column is a cached
     derived value; the source of truth is the SUM of this table.
  2. `Checkin` — one row per diner check-in. Has a unique constraint
     on (user_id, restaurant_id, checkin_date) so a user can't farm
     points by checking in repeatedly. The 200-point award is recorded
     as a PointsTransaction of type=checkin in the same insert.
  3. `GiftCardRedemption` — user requests a redemption once their
     balance is above the threshold. Status starts at
     `pending_fulfillment` and a future Stage 8.1 will integrate a
     third-party provider (Tremendous / Tango Card / Rybbon). For now
     the admin manually marks rows fulfilled or failed.

The system also adds three fields to `User`:
  - `email_verified` (default false) — drives the "A" referral trigger
  - `referral_code` (unique) — short code shared with friends
  - `referred_by_user_id` (FK, nullable) — who referred this user
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ---- PointsTransaction: the append-only ledger ----
class PointsTransaction(Base):
    """Append-only ledger. NO updated_at column on purpose: rows must
    not be mutated after insert. If you ever need to "correct" a row,
    insert a compensating transaction (negative amount for the same
    reference_id) — never UPDATE this table.

    `type` is one of:
      - "referral":   referrer earns when a referee signs up
      - "review":     reviewer earns when their review is approved
      - "checkin":    diner earns when they check in at a restaurant
      - "redemption": user SPENDS (negative amount) when they request
                      a gift card; the matching GiftCardRedemption row
                      is the audit trail for what they got in return

    `amount` is always a positive integer (credits) or negative integer
    (debits). The CHECK constraint below enforces sign-by-type to keep
    the ledger self-consistent.
    """
    __tablename__ = "points_transactions"
    __table_args__ = (
        CheckConstraint(
            "(type IN ('referral','review','checkin') AND amount > 0) "
            "OR (type = 'redemption' AND amount < 0)",
            name="ck_points_txn_amount_sign",
        ),
        # Reference id is a free-form UUID. We index it for the dedupe
        # check: "did we already credit for this review/checkin/signup?"
        # Each (user_id, type, reference_id) tuple must be unique.
        UniqueConstraint("user_id", "type", "reference_id",
                         name="uq_points_txn_user_type_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    # Generic reference: a review_id, checkin_id, redemption_id, or the
    # referred_user's id for referral credits. NOT a FK — the reference
    # target may live in any table depending on type.
    reference_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    # Optional human-readable note (e.g. "referral signup", "approved
    # review of Al-Mansoori's"). Not exposed in any UI yet, but kept
    # for ops debugging.
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # NB: NO `updated_at` column. Append-only. If you need to "fix" a
    # row, insert a compensating transaction (see module docstring).


Index("ix_points_txn_user_created", PointsTransaction.user_id, PointsTransaction.created_at)


# ---- Checkin: diner physical visit to a restaurant ----
class Checkin(Base):
    """One row per diner checkin. UNIQUE on (user_id, restaurant_id,
    checkin_date) so a user can't check in twice at the same restaurant
    in the same day to farm the 200-point reward.

    `checkin_date` is a `date` column populated in Python (server-local
    timezone). It's separated from `checkin_at` (a timestamp) so the
    UNIQUE constraint can use a plain column, not a functional index.
    """
    __tablename__ = "checkins"
    __table_args__ = (
        UniqueConstraint("user_id", "restaurant_id", "checkin_date",
                         name="uq_checkin_user_rest_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    checkin_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    checkin_date: Mapped[date] = mapped_column(Date, nullable=False)


Index("ix_checkin_restaurant_date", Checkin.restaurant_id, Checkin.checkin_date)


# ---- GiftCardRedemption ----
# Status lifecycle:
#   pending_fulfillment -> fulfilled         (admin: payment was sent)
#   pending_fulfillment -> failed            (admin: payment failed, points refunded)
#   pending_fulfillment -> canceled          (admin or system: user canceled within window)
#   fulfilled / failed / canceled are TERMINAL.
#
# The PROMPT explicitly forbids building the third-party provider
# integration in Stage 8. The redemption stops at
# `pending_fulfillment` and waits for the founder to pick a provider
# (Tremendous / Tango Card / Rybbon / direct ACH). A clear
# `TODO Stage 8.1` block in `app/services/gift_cards.py` flags where
# the provider call goes. Per BRD §10, this has real financial
# liability implications, so the explicit no-op is the right call.
class GiftCardRedemption(Base):
    __tablename__ = "gift_card_redemptions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    # The companion PointsTransaction (debit) — the ledger link.
    points_txn_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True,
    )
    # Snapshot of the points cost at request time (denormalized for
    # auditability — the ledger amount should always match).
    points_spent: Mapped[int] = mapped_column(Integer, nullable=False)
    # Set when fulfilled: the gift card code, ACH ref, etc. from
    # whatever provider we eventually wire up.
    external_ref: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )
    # Set when fulfilled / failed / canceled.
    fulfillment_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending_fulfillment", server_default="pending_fulfillment"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    fulfilled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
