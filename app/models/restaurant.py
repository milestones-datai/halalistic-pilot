"""Restaurant, Cuisine, Photo ORM models."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    HalalStatus,
    HalalVerificationSource,
    PriceRange,
    RestaurantTier,
)


class Cuisine(Base):
    """Lookup table for cuisine types (Pakistani, Indian, Mediterranean, etc.)."""
    __tablename__ = "cuisines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)


class Restaurant(Base):
    """A halal restaurant listing."""
    __tablename__ = "restaurants"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_restaurants_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(220), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Address
    address_line: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False, default="Houston")
    state: Mapped[str] = mapped_column(String(50), nullable=False, default="TX")
    postal_code: Mapped[str] = mapped_column(String(20), nullable=False)
    country: Mapped[str] = mapped_column(String(50), nullable=False, default="US")

    # Geocoded
    latitude: Mapped[Optional[float]] = mapped_column(Numeric(10, 7), nullable=True, index=True)
    longitude: Mapped[Optional[float]] = mapped_column(Numeric(10, 7), nullable=True, index=True)
    geocoded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    google_place_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Contact
    phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    website: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Categorisation
    price_range: Mapped[PriceRange] = mapped_column(
        Enum(PriceRange, name="price_range", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=PriceRange.MODERATE,
    )
    tier: Mapped[RestaurantTier] = mapped_column(
        Enum(RestaurantTier, name="restaurant_tier", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=RestaurantTier.FREE,
    )

    # Halal (per BRD §3.2)
    halal_status: Mapped[HalalStatus] = mapped_column(
        Enum(HalalStatus, name="halal_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=HalalStatus.UNVERIFIED,
    )
    halal_verification_source: Mapped[HalalVerificationSource] = mapped_column(
        Enum(
            HalalVerificationSource,
            name="halal_verification_source",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=HalalVerificationSource.SELF_REPORTED,
    )
    halal_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    halal_verified_by_admin_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Per BRD §5.2 — PostgreSQL tsvector for full-text search (NOT Elasticsearch).
    # Populated by app/services/restaurant_service._update_search_vector on every write.
    search_vector: Mapped[Optional[str]] = mapped_column(TSVECTOR, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    cuisines: Mapped[list["Cuisine"]] = relationship(
        secondary="restaurant_cuisines", lazy="selectin"
    )
    photos: Mapped[list["Photo"]] = relationship(
        back_populates="restaurant",
        cascade="all, delete-orphan",
        order_by="Photo.sort_order",
        lazy="selectin",
    )
    menu_categories: Mapped[list["MenuCategory"]] = relationship(
        back_populates="restaurant",
        cascade="all, delete-orphan",
        order_by="MenuCategory.sort_order",
        lazy="selectin",
    )


class RestaurantCuisine(Base):
    """M2M between Restaurant and Cuisine."""
    __tablename__ = "restaurant_cuisines"
    __table_args__ = (
        UniqueConstraint("restaurant_id", "cuisine_id", name="uq_restaurant_cuisine"),
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    cuisine_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cuisines.id", ondelete="CASCADE"), primary_key=True
    )


class Photo(Base):
    """A photo attached to a Restaurant. Stored in Azure Blob (per BRD §3.1)."""
    __tablename__ = "photos"

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
    blob_name: Mapped[str] = mapped_column(String(500), nullable=False)
    blob_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False, default="image/jpeg")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    caption: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    restaurant: Mapped["Restaurant"] = relationship(back_populates="photos")
