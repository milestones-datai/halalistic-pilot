"""Billing models — Stripe-backed subscriptions for restaurants and users (Stage 7).

Two flavors:

  - `RestaurantBillingSubscription`: paid tier subscription for a restaurant
    (Free / Photo+ / Featured / Premium per BRD §3.4). Webhook events from
    Stripe update `status`, `current_period_end`, `cancel_at_period_end` here
    AND the `Restaurant.tier` field atomically (so the photo-cap and
    push-only gate read from the same source of truth).

  - `UserBillingSubscription`: single flat-rate monthly tier for end users
    (per BRD §3.6). Status flips on Stripe lifecycle events; the
    `get_user_subscription_tier` helper in `app/services/billing.py`
    reads from here.

CRITICAL: per BRD §7, NO card data lives in this DB. We store only Stripe
IDs and the data we need to react to lifecycle events. Stripe Elements /
Checkout collect the actual card data on Stripe's side; we never see it.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# Stripe subscription statuses we map onto our `status` column. The set is
# what Stripe actually emits on `customer.subscription.*` events.
STRIPE_STATUS_ACTIVE = "active"
STRIPE_STATUS_TRIALING = "trialing"
STRIPE_STATUS_PAST_DUE = "past_due"
STRIPE_STATUS_UNPAID = "unpaid"
STRIPE_STATUS_CANCELED = "canceled"
STRIPE_STATUS_INCOMPLETE = "incomplete"
STRIPE_STATUS_INCOMPLETE_EXPIRED = "incomplete_expired"


class RestaurantBillingSubscription(Base):
    __tablename__ = "restaurant_billing_subscriptions"
    __table_args__ = (
        UniqueConstraint("restaurant_id", name="uq_restaurant_billing_one_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Stripe identifiers. Unique per customer / per subscription at the
    # Stripe level; we store them for webhook lookup.
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(
        String(80), nullable=True, unique=True, index=True,
    )
    # Cached tier — mirrors Restaurant.tier. Updated atomically with the
    # restaurant row by the webhook handler so the photo-cap gate and
    # this row never disagree.
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="free")
    # Mirrors Stripe's `status` field (one of STRIPE_STATUS_* above).
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="incomplete")
    current_period_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


Index("ix_restaurant_billing_status", RestaurantBillingSubscription.status)


class UserBillingSubscription(Base):
    __tablename__ = "user_billing_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_billing_one_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(
        String(80), nullable=True, unique=True, index=True,
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="incomplete")
    current_period_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


Index("ix_user_billing_status", UserBillingSubscription.status)
