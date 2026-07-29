"""Tag model — admin-managed controlled vocabulary reviews select from (Stage 5).

Per BRD §3.3, reviews may reference up to 3 tags. The tags themselves are
created and maintained by admins (not by diners) so the vocabulary stays
curated. Soft-delete via `is_active`: deactivated tags are hidden from the
new-review picker but are still rendered on historical reviews so links and
read-context don't break.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("slug", name="uq_tags_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Short, human label e.g. "Family-friendly", "Spicy", "Late-night"
    name: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    # URL-safe version of the name; admin-supplied so we don't auto-generate
    # and surprise the user with a slug they didn't see.
    slug: Mapped[str] = mapped_column(String(60), nullable=False)
    # Optional category hint (cuisine / vibe / dietary / access) so the admin
    # UI can group them. Optional so we don't bloat the seed.
    category: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
