"""Menu data model: Category → Subcategory → Item → Variant.

Per founder scope-lock (Stage 3), this is 4 levels. No alcohol/non-halal items
are modeled — this is a halal platform, every item is halal by definition.
The `verification_status` + `verification_source` on Restaurant convey halal
trust to users per BRD §3.2.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MenuCategory(Base):
    """Top-level menu category (Appetizers, Mains, Drinks, Desserts, ...)."""
    __tablename__ = "menu_categories"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    restaurant: Mapped["Restaurant"] = relationship(back_populates="menu_categories")
    subcategories: Mapped[list["MenuSubcategory"]] = relationship(
        back_populates="category",
        cascade="all, delete-orphan",
        order_by="MenuSubcategory.sort_order",
        lazy="selectin",
    )
    items: Mapped[list["MenuItem"]] = relationship(
        back_populates="category",
        cascade="all, delete-orphan",
        order_by="MenuItem.sort_order",
        lazy="selectin",
    )


class MenuSubcategory(Base):
    """Optional mid-level (e.g. 'Lunch' vs 'Dinner' under Mains)."""
    __tablename__ = "menu_subcategories"
    __table_args__ = (
        UniqueConstraint("category_id", "name", name="uq_subcategory_name_per_category"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("menu_categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    category: Mapped[MenuCategory] = relationship(back_populates="subcategories")
    items: Mapped[list["MenuItem"]] = relationship(
        back_populates="subcategory",
        cascade="all, delete-orphan",
        order_by="MenuItem.sort_order",
        lazy="selectin",
    )


class MenuItem(Base):
    """A dish on the menu (level 3)."""
    __tablename__ = "menu_items"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("menu_categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subcategory_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("menu_subcategories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    base_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    photo_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    allergens: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String(50)), nullable=True)
    calories: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prep_time_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    category: Mapped[MenuCategory] = relationship(back_populates="items")
    subcategory: Mapped[Optional[MenuSubcategory]] = relationship(back_populates="items")
    variants: Mapped[list["MenuItemVariant"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        order_by="MenuItemVariant.sort_order",
        lazy="selectin",
    )


class MenuItemVariant(Base):
    """A variant of an item (Small/Medium/Large, Mild/Medium/Hot, etc.)."""
    __tablename__ = "menu_item_variants"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("menu_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    item: Mapped[MenuItem] = relationship(back_populates="variants")
