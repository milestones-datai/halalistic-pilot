"""tags + reviews + review_photos + review_tags (Stage 5)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- tags ----
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(60), nullable=False, unique=True),
        sa.Column("slug", sa.String(60), nullable=False, unique=True),
        sa.Column("category", sa.String(40), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )

    # ---- reviews ----
    op.create_table(
        "reviews",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reviewer_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("instagram_embed_url", sa.String(500), nullable=True),
        sa.Column("moderation_status", sa.String(20),
                  nullable=False, server_default="pending"),
        sa.Column("flagged", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("flag_reasons", sa.Text, nullable=True),
        sa.Column("reviewed_by_admin_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_review_rating_range"),
        sa.UniqueConstraint("restaurant_id", "reviewer_id", name="uq_review_per_user_restaurant"),
    )
    op.create_index("ix_reviews_restaurant_status", "reviews",
                    ["restaurant_id", "moderation_status"])
    op.create_index("ix_reviews_status_created", "reviews",
                    ["moderation_status", "created_at"])

    # ---- review_photos ----
    op.create_table(
        "review_photos",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False),
        sa.Column("blob_name", sa.String(500), nullable=False),
        sa.Column("blob_url", sa.String(1000), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_review_photos_review_id", "review_photos", ["review_id"])

    # ---- review_tags M2M ----
    op.create_table(
        "review_tags",
        sa.Column("review_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("reviews.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tag_id", sa.Integer,
                  sa.ForeignKey("tags.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("review_tags")
    op.drop_index("ix_review_photos_review_id", table_name="review_photos")
    op.drop_table("review_photos")
    op.drop_index("ix_reviews_status_created", table_name="reviews")
    op.drop_index("ix_reviews_restaurant_status", table_name="reviews")
    op.drop_table("reviews")
    op.drop_table("tags")
