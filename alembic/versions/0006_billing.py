"""Stage 7 billing: tier rename + push-opt-in rename + 2 new billing tables.

Migration plan:
  1. Rename `restaurant_subscriptions` (push-opt-in) → `restaurant_push_subscriptions`.
     The old name will collide with the new "RestaurantBillingSubscription" once
     developers see both — better to disambiguate now.
  2. Drop the `verified` value from `restaurant_tier` (Postgres ENUM). Map
     existing 'verified' rows to 'photo_plus'. This aligns Stage 3's 3-tier
     model with BRD §3.4's 4-tier model.
  3. Add the 2 new billing tables.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rename push-opt-in table + its indexes + unique constraint.
    op.rename_table("restaurant_subscriptions", "restaurant_push_subscriptions")
    op.alter_column("restaurant_push_subscriptions", "created_at",
                    existing_type=sa.DateTime(timezone=True), nullable=False)
    op.execute("ALTER INDEX IF EXISTS uq_subscription_user_restaurant "
               "RENAME TO uq_pushsub_user_restaurant")
    op.execute("ALTER INDEX IF EXISTS ix_subscriptions_user RENAME TO ix_pushsubs_user")
    op.execute("ALTER INDEX IF EXISTS ix_subscriptions_restaurant RENAME TO ix_pushsubs_restaurant")

    # 2. Reshape the restaurant_tier enum: drop 'verified', add 'photo_plus' and 'featured'.
    # Postgres doesn't let you drop a value from an enum in place; the standard
    # pattern is: rename old type, create new type, migrate column, drop old.
    op.execute("ALTER TYPE restaurant_tier RENAME TO restaurant_tier_old")
    op.execute(
        "CREATE TYPE restaurant_tier AS ENUM ('free', 'photo_plus', 'featured', 'premium')"
    )
    op.execute("ALTER TABLE restaurants ALTER COLUMN tier DROP DEFAULT")
    op.execute(
        "ALTER TABLE restaurants "
        "ALTER COLUMN tier TYPE restaurant_tier "
        "USING ("
        "  CASE tier::text "
        "    WHEN 'verified' THEN 'photo_plus' "
        "    WHEN 'premium' THEN 'premium' "
        "    ELSE tier::text "
        "  END::restaurant_tier"
        ")"
    )
    op.execute("ALTER TABLE restaurants ALTER COLUMN tier SET DEFAULT 'free'")
    op.execute("DROP TYPE restaurant_tier_old")

    # 3. New billing tables.
    op.create_table(
        "restaurant_billing_subscriptions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stripe_customer_id", sa.String(80), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(80), nullable=True, unique=True),
        sa.Column("tier", sa.String(20), nullable=False, server_default="free"),
        sa.Column("status", sa.String(30), nullable=False, server_default="incomplete"),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("restaurant_id", name="uq_restaurant_billing_one_active"),
    )
    op.create_index("ix_restaurant_billing_stripe_customer", "restaurant_billing_subscriptions",
                    ["stripe_customer_id"])
    op.create_index("ix_restaurant_billing_status", "restaurant_billing_subscriptions",
                    ["status"])

    op.create_table(
        "user_billing_subscriptions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stripe_customer_id", sa.String(80), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(80), nullable=True, unique=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="incomplete"),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", name="uq_user_billing_one_active"),
    )
    op.create_index("ix_user_billing_stripe_customer", "user_billing_subscriptions",
                    ["stripe_customer_id"])
    op.create_index("ix_user_billing_status", "user_billing_subscriptions",
                    ["status"])


def downgrade() -> None:
    op.drop_index("ix_user_billing_status", table_name="user_billing_subscriptions")
    op.drop_index("ix_user_billing_stripe_customer", table_name="user_billing_subscriptions")
    op.drop_table("user_billing_subscriptions")
    op.drop_index("ix_restaurant_billing_status", table_name="restaurant_billing_subscriptions")
    op.drop_index("ix_restaurant_billing_stripe_customer", table_name="restaurant_billing_subscriptions")
    op.drop_table("restaurant_billing_subscriptions")

    # Restore the 3-tier enum.
    op.execute("ALTER TYPE restaurant_tier RENAME TO restaurant_tier_new")
    op.execute("CREATE TYPE restaurant_tier AS ENUM ('free', 'verified', 'premium')")
    op.execute(
        "ALTER TABLE restaurants "
        "ALTER COLUMN tier TYPE restaurant_tier "
        "USING ("
        "  CASE tier::text "
        "    WHEN 'photo_plus' THEN 'verified' "
        "    WHEN 'featured' THEN 'verified' "
        "    ELSE tier::text "
        "  END::restaurant_tier"
        ")"
    )
    op.execute("ALTER TABLE restaurants ALTER COLUMN tier SET DEFAULT 'free'")
    op.execute("DROP TYPE restaurant_tier_new")

    # Restore the old push-opt-in table name.
    op.execute("ALTER INDEX IF EXISTS uq_pushsub_user_restaurant "
               "RENAME TO uq_subscription_user_restaurant")
    op.execute("ALTER INDEX IF EXISTS ix_pushsubs_user RENAME TO ix_subscriptions_user")
    op.execute("ALTER INDEX IF EXISTS ix_pushsubs_restaurant RENAME TO ix_subscriptions_restaurant")
    op.rename_table("restaurant_push_subscriptions", "restaurant_subscriptions")
