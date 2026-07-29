"""Push subscription model (Stage 9).

One row per browser/device that subscribed to web push. The
`restaurant_id` column is the per-restaurant opt-in (BRD §3.4 + §3.8):
if NULL, the subscription is "all restaurants I follow" (a future
expansion hook). For Stage 9 we ship per-restaurant only — diners
opt in to a specific restaurant and the deal-approval trigger sends
them a push.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    __table_args__ = (
        # A given (user, restaurant) can have multiple devices subscribed
        # (phone + laptop + tablet). The endpoint URL is the unique key.
        Index("ix_push_sub_user_restaurant", "user_id", "restaurant_id"),
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
    # The push service endpoint URL the browser registered. Unique per
    # subscription; the unsubscribe endpoint uses it to remove the row.
    endpoint: Mapped[str] = mapped_column(String(2000), nullable=False, unique=True)
    # Per-subscription keys (VAPID spec).
    p256dh: Mapped[str] = mapped_column(String(200), nullable=False)
    auth: Mapped[str] = mapped_column(String(100), nullable=False)
    # Optional browser hint — used in the VAPID "urgency" header and
    # for debugging in admin dashboards. Not used for routing.
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
