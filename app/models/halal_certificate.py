"""HalalCertificate model — per BRD §3.2 + §6.

Each certificate is a real document (PDF or image) uploaded by the owner
into Azure Blob Storage. The Azure SSE (server-side encryption, AES-256, on
by default for every Storage account since 2017) satisfies BRD §7's
"encrypted at rest" requirement — no client-side encryption layer needed.

A restaurant may have multiple certificates over time (renewals); only one
is "active" at a time. The active one is determined by:
  1. status = APPROVED, and
  2. (expiry_date IS NULL OR expiry_date > now()), and
  3. the most recent approved one
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import CertificateStatus


class HalalCertificate(Base):
    __tablename__ = "halal_certificates"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Certifying body: either from the lookup table OR free-text "Other".
    # Server enforces: exactly one is non-null.
    certifying_body_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("certifying_bodies.id", ondelete="RESTRICT"), nullable=True,
    )
    custom_certifying_body: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Document location (Azure Blob; SSE on at-rest).
    blob_name: Mapped[str] = mapped_column(String(500), nullable=False)
    blob_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False, default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Nullable: some certs (e.g. older / "evergreen" ones) don't expire.
    expiry_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    status: Mapped[CertificateStatus] = mapped_column(
        # Enum-as-varchar handled by SQLAlchemy
        # (we just store the value, no PG enum type — simpler for additive changes)
        String(20), nullable=False, default=CertificateStatus.PENDING,
    )

    # Admin review
    reviewed_by_admin_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    certifying_body = relationship("CertifyingBody", lazy="joined")
