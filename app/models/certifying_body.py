"""CertifyingBody lookup table.

Per BRD §3.2 + Stage 4 spec: the owner picks a standard from a dropdown when
uploading a halal certificate; if the body isn't in the list, they pick
"Other" and type a free-text name (stored on the HalalCertificate as
`custom_certifying_body`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CertifyingBody(Base):
    __tablename__ = "certifying_bodies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    country: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
