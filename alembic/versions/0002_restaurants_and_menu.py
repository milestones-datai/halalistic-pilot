"""restaurants, cuisines, photos, menu tables (Stage 3) + tsvector search index

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- Enums (use postgresql.ENUM so create_type=False + checkfirst works) ----
    price_range = PG_ENUM("1", "2", "3", "4", name="price_range", create_type=False)
    price_range.create(op.get_bind(), checkfirst=True)

    restaurant_tier = PG_ENUM("free", "verified", "premium", name="restaurant_tier", create_type=False)
    restaurant_tier.create(op.get_bind(), checkfirst=True)

    halal_status = PG_ENUM(
        "unverified", "pending", "verified", "revoked",
        name="halal_status", create_type=False,
    )
    halal_status.create(op.get_bind(), checkfirst=True)

    halal_verification_source = PG_ENUM(
        "certified", "self_reported", "crowd_verified",
        name="halal_verification_source", create_type=False,
    )
    halal_verification_source.create(op.get_bind(), checkfirst=True)

    # ---- cuisines ----
    op.create_table(
        "cuisines",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
    )
    op.create_index("ix_cuisines_slug", "cuisines", ["slug"], unique=True)

    # ---- restaurants ----
    op.create_table(
        "restaurants",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(220), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("address_line", sa.String(255), nullable=False),
        sa.Column("city", sa.String(100), nullable=False, server_default="Houston"),
        sa.Column("state", sa.String(50), nullable=False, server_default="TX"),
        sa.Column("postal_code", sa.String(20), nullable=False),
        sa.Column("country", sa.String(50), nullable=False, server_default="US"),
        sa.Column("latitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("longitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("geocoded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("google_place_id", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("website", sa.String(255), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("price_range", price_range, nullable=False, server_default="2"),
        sa.Column("tier", restaurant_tier, nullable=False, server_default="free"),
        sa.Column("halal_status", halal_status, nullable=False, server_default="unverified"),
        sa.Column("halal_verification_source", halal_verification_source,
                  nullable=False, server_default="self_reported"),
        sa.Column("halal_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("halal_verified_by_admin_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("search_vector", TSVECTOR, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("slug", name="uq_restaurants_slug"),
    )
    op.create_index("ix_restaurants_owner_id", "restaurants", ["owner_id"])
    op.create_index("ix_restaurants_name", "restaurants", ["name"])
    op.create_index("ix_restaurants_latitude", "restaurants", ["latitude"])
    op.create_index("ix_restaurants_longitude", "restaurants", ["longitude"])
    # GIN index for tsvector full-text search (per BRD §5.2 — PG native, no Elasticsearch)
    op.execute("CREATE INDEX ix_restaurants_search_vector ON restaurants USING GIN (search_vector)")

    # ---- restaurant_cuisines (M2M) ----
    op.create_table(
        "restaurant_cuisines",
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("cuisine_id", sa.Integer,
                  sa.ForeignKey("cuisines.id", ondelete="CASCADE"), primary_key=True),
        sa.UniqueConstraint("restaurant_id", "cuisine_id", name="uq_restaurant_cuisine"),
    )

    # ---- photos ----
    op.create_table(
        "photos",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("uploaded_by", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("blob_name", sa.String(500), nullable=False),
        sa.Column("blob_url", sa.String(1000), nullable=False),
        sa.Column("content_type", sa.String(50), nullable=False, server_default="image/jpeg"),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("width", sa.Integer, nullable=True),
        sa.Column("height", sa.Integer, nullable=True),
        sa.Column("caption", sa.String(500), nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_photos_restaurant_id", "photos", ["restaurant_id"])

    # ---- menu_categories ----
    op.create_table(
        "menu_categories",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_menu_categories_restaurant_id", "menu_categories", ["restaurant_id"])

    # ---- menu_subcategories ----
    op.create_table(
        "menu_subcategories",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("category_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("menu_categories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("category_id", "name", name="uq_subcategory_name_per_category"),
    )
    op.create_index("ix_menu_subcategories_category_id", "menu_subcategories", ["category_id"])
    op.create_index("ix_menu_subcategories_restaurant_id", "menu_subcategories", ["restaurant_id"])

    # ---- menu_items ----
    op.create_table(
        "menu_items",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("category_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("menu_categories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subcategory_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("menu_subcategories.id", ondelete="SET NULL"), nullable=True),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("base_price_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("photo_url", sa.String(1000), nullable=True),
        sa.Column("allergens", ARRAY(sa.String(50)), nullable=True),
        sa.Column("calories", sa.Integer, nullable=True),
        sa.Column("prep_time_minutes", sa.Integer, nullable=True),
        sa.Column("is_available", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_menu_items_category_id", "menu_items", ["category_id"])
    op.create_index("ix_menu_items_subcategory_id", "menu_items", ["subcategory_id"])
    op.create_index("ix_menu_items_restaurant_id", "menu_items", ["restaurant_id"])

    # ---- menu_item_variants ----
    op.create_table(
        "menu_item_variants",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("item_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("price_cents", sa.Integer, nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_available", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_menu_item_variants_item_id", "menu_item_variants", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_menu_item_variants_item_id", table_name="menu_item_variants")
    op.drop_table("menu_item_variants")
    op.drop_index("ix_menu_items_restaurant_id", table_name="menu_items")
    op.drop_index("ix_menu_items_subcategory_id", table_name="menu_items")
    op.drop_index("ix_menu_items_category_id", table_name="menu_items")
    op.drop_table("menu_items")
    op.drop_index("ix_menu_subcategories_restaurant_id", table_name="menu_subcategories")
    op.drop_index("ix_menu_subcategories_category_id", table_name="menu_subcategories")
    op.drop_table("menu_subcategories")
    op.drop_index("ix_menu_categories_restaurant_id", table_name="menu_categories")
    op.drop_table("menu_categories")
    op.drop_index("ix_photos_restaurant_id", table_name="photos")
    op.drop_table("photos")
    op.drop_table("restaurant_cuisines")
    op.execute("DROP INDEX IF EXISTS ix_restaurants_search_vector")
    op.drop_index("ix_restaurants_longitude", table_name="restaurants")
    op.drop_index("ix_restaurants_latitude", table_name="restaurants")
    op.drop_index("ix_restaurants_name", table_name="restaurants")
    op.drop_index("ix_restaurants_owner_id", table_name="restaurants")
    op.drop_table("restaurants")
    op.drop_index("ix_cuisines_slug", table_name="cuisines")
    op.drop_table("cuisines")
    sa.Enum(name="halal_verification_source").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="halal_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="restaurant_tier").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="price_range").drop(op.get_bind(), checkfirst=True)
