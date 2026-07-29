"""Review + ReviewPhoto models + M2M to Tag (Stage 5).

Per BRD §3.3:
  - Pre-moderation: every review enters PENDING and does NOT go live until an
    admin approves it. Even unflagged reviews wait.
  - Auto-flag: a separate informational boolean. The admin queue shows
    flagged reviews distinctly from unflagged-but-still-pending.
  - Aggregate rating on the restaurant profile = average of APPROVED reviews
    only. Pending/rejected reviews are excluded.

Schema notes:
  - `reviewer_id` is NOT NULL — Stage 5 only supports the DINER role writing
    reviews. Anonymous reviews are not in scope.
  - rating CHECK 1..5 enforced both in DB and in the Pydantic schema.
  - tags via M2M association table `review_tags`; max 3 enforced in service.
  - photos in a separate table for clean queries; max 3 enforced in service.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ---- M2M: review <-> tag ----
review_tags_table = Table(
    "review_tags",
    Base.metadata,
    Column(
        "review_id",
        PG_UUID(as_uuid=True),
        ForeignKey("reviews.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        Integer,
        ForeignKey("tags.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    ),
)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        # One review per (reviewer, restaurant) — prevents star-bombing.
        UniqueConstraint("restaurant_id", "reviewer_id", name="uq_review_per_user_restaurant"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_review_rating_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    instagram_embed_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # ---- Moderation state ----
    # Pre-moderation: every review starts PENDING. Stays PENDING until an
    # admin approves or rejects it. Even unflagged reviews wait.
    moderation_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    # Informational auto-flag. The flag does NOT bypass the admin — it just
    # surfaces a row in the queue with `flagged=true` so the admin sees it
    # first.
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Free-text explanation of WHY the auto-flag fired (e.g. "profanity:
    # 'slur' (1)", "duplicate of review ... by same user"). Visible to admin.
    flag_reasons: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ---- Admin review ----
    reviewed_by_admin_id: Mapped[Optional[uuid.UUID]] = mapped_column(
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

    # Relationships
    tags: Mapped[list["Tag"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Tag", secondary=review_tags_table, lazy="selectin"
    )
    photos: Mapped[list["ReviewPhoto"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="review",
        cascade="all, delete-orphan",
        order_by="ReviewPhoto.sort_order",
        lazy="selectin",
    )
    reviewer: Mapped["User"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "User", lazy="selectin", foreign_keys=[reviewer_id]
    )


class ReviewPhoto(Base):
    __tablename__ = "review_photos"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reviews.id", ondelete="CASCADE"),
        nullable=False,
    )
    blob_name: Mapped[str] = mapped_column(String(500), nullable=False)
    blob_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    review: Mapped["Review"] = relationship(back_populates="photos")  # type: ignore[name-defined]  # noqa: F821


# Composite index that makes the aggregate-rating query cheap:
#   WHERE restaurant_id = ? AND moderation_status = 'approved'
Index(
    "ix_reviews_restaurant_status",
    Review.restaurant_id,
    Review.moderation_status,
)
# Index for the admin queue (status=pending, ordered by created_at).
Index("ix_reviews_status_created", Review.moderation_status, Review.created_at)
