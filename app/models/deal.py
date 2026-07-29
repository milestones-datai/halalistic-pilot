"""Deal model + Subscription (push-notification) model (Stage 6).

Per BRD §3.5, deals go through a strict state machine (see DealStatus enum).
The model here is intentionally minimal — all transition logic lives in
`app/services/deals.py` so the API layer can't accidentally bypass it.

`Subscription` is the push-notification subscription: a user opts in to
receive deals from a specific restaurant. Per BRD §3.4, push-only deals
(only allowed for Premium-tier restaurants) are gated to this list. This
is a separate concept from the user's *platform* subscription tier — that
latter concept belongs to Stage 7 and is currently stubbed in
`app/services/deals.py:get_user_subscription_tier`.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import DealAudience, DealStatus, DealType
from app.models.restaurant import Restaurant


class Deal(Base):
    __tablename__ = "deals"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_deal_dates_order"),
        CheckConstraint("status IN ('draft','pending_review','approved','rejected','expired')",
                        name="ck_deal_status_enum"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    # True if the deal was hand-curated by a Deal Curator (skipped review).
    # False for owner-submitted deals. Useful for analytics and admin views.
    curator_created: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deal_type: Mapped[DealType] = mapped_column(
        String(30), nullable=False
    )
    # For percentage_off: 0-100 (e.g. 20 = 20% off)
    # For fixed_amount: amount in cents (e.g. 500 = $5.00)
    # For bogo / free_item / bundle: unused, NULL allowed
    discount_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[DealStatus] = mapped_column(
        String(20), nullable=False, default=DealStatus.DRAFT, server_default="draft"
    )
    target_audience: Mapped[DealAudience] = mapped_column(
        String(20), nullable=False, default=DealAudience.PUBLIC, server_default="public"
    )

    # Curator review
    reviewed_by_curator_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    restaurant: Mapped[Restaurant] = relationship(lazy="joined")


# Index that makes the public active-listing query fast:
#   WHERE restaurant_id = ? AND status = 'approved' AND end_date >= today
Index(
    "ix_deals_restaurant_status_end",
    Deal.restaurant_id,
    Deal.status,
    Deal.end_date,
)
# Index for the curator pending queue.
Index("ix_deals_status_created", Deal.status, Deal.created_at)


class RestaurantPushSubscription(Base):
    """A user opting in to receive push-notification deals from a restaurant.

    Per BRD §3.4: push-only deals (Premium-tier feature) are visible ONLY
    to users who have a row here for that restaurant. Public deals are
    visible to all eligible users regardless of this table.

    Note: this is NOT the same as a paid billing subscription (see
    `app.models.billing.RestaurantBillingSubscription`). The push opt-in
    here is free; billing is a separate concept handled in Stage 7.
    """
    __tablename__ = "restaurant_push_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "restaurant_id", name="uq_pushsub_user_restaurant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


Index("ix_pushsubs_user", RestaurantPushSubscription.user_id)
Index("ix_pushsubs_restaurant", RestaurantPushSubscription.restaurant_id)
