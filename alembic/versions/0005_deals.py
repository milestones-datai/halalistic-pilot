"""deals + restaurant_subscriptions (Stage 6)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deals",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("curator_created", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("deal_type", sa.String(30), nullable=False),
        sa.Column("discount_value", sa.Numeric(10, 2), nullable=True),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("target_audience", sa.String(20), nullable=False, server_default="public"),
        sa.Column("reviewed_by_curator_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("end_date >= start_date", name="ck_deal_dates_order"),
        sa.CheckConstraint(
            "status IN ('draft','pending_review','approved','rejected','expired')",
            name="ck_deal_status_enum",
        ),
    )
    op.create_index("ix_deals_restaurant_status_end", "deals",
                    ["restaurant_id", "status", "end_date"])
    op.create_index("ix_deals_status_created", "deals", ["status", "created_at"])

    op.create_table(
        "restaurant_subscriptions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "restaurant_id", name="uq_subscription_user_restaurant"),
    )
    op.create_index("ix_subscriptions_user", "restaurant_subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_restaurant", "restaurant_subscriptions", ["restaurant_id"])


def downgrade() -> None:
    op.drop_index("ix_subscriptions_restaurant", table_name="restaurant_subscriptions")
    op.drop_index("ix_subscriptions_user", table_name="restaurant_subscriptions")
    op.drop_table("restaurant_subscriptions")
    op.drop_index("ix_deals_status_created", table_name="deals")
    op.drop_index("ix_deals_restaurant_status_end", table_name="deals")
    op.drop_table("deals")
