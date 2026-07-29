"""Stage 8 points, referrals, gift cards: 3 new tables + 4 new user columns.

Migration plan:
  1. Add `email_verified`, `referral_code`, `referred_by_user_id`, `points_balance`
     to the `users` table.
  2. Create `points_transactions` (append-only ledger; CHECK enforces
     sign-by-type).
  3. Create `checkins` with UNIQUE on (user_id, restaurant_id, checkin_date)
     so a diner can't farm the 200-point checkin reward.
  4. Create `gift_card_redemptions` for the pending_fulfillment flow.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. New User columns.
    op.add_column("users", sa.Column(
        "email_verified", sa.Boolean, nullable=False, server_default=sa.false(),
    ))
    op.add_column("users", sa.Column(
        "referral_code", sa.String(20), nullable=True, unique=True,
    ))
    op.create_index("ix_users_referral_code", "users", ["referral_code"], unique=True)
    op.add_column("users", sa.Column(
        "referred_by_user_id", PG_UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    ))
    op.create_index("ix_users_referred_by", "users", ["referred_by_user_id"])
    op.add_column("users", sa.Column(
        "points_balance", sa.Integer, nullable=False, server_default="0",
    ))

    # 2. points_transactions (append-only ledger).
    op.create_table(
        "points_transactions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column("reference_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "(type IN ('referral','review','checkin') AND amount > 0) "
            "OR (type = 'redemption' AND amount < 0)",
            name="ck_points_txn_amount_sign",
        ),
        sa.UniqueConstraint("user_id", "type", "reference_id",
                           name="uq_points_txn_user_type_ref"),
    )
    op.create_index("ix_points_txn_user_created", "points_transactions",
                    ["user_id", "created_at"])
    op.create_index("ix_points_txn_user_type", "points_transactions",
                    ["user_id", "type"])

    # 3. checkins (1/day/restaurant cap enforced by UNIQUE).
    op.create_table(
        "checkins",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("checkin_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("checkin_date", sa.Date, nullable=False),
        sa.UniqueConstraint("user_id", "restaurant_id", "checkin_date",
                            name="uq_checkin_user_rest_date"),
    )
    op.create_index("ix_checkin_user", "checkins", ["user_id"])
    op.create_index("ix_checkin_restaurant_date", "checkins",
                    ["restaurant_id", "checkin_date"])

    # 4. gift_card_redemptions.
    op.create_table(
        "gift_card_redemptions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("points_txn_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("points_spent", sa.Integer, nullable=False),
        sa.Column("external_ref", sa.String(200), nullable=True),
        sa.Column("fulfillment_note", sa.Text, nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending_fulfillment"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_gcr_user", "gift_card_redemptions", ["user_id"])
    op.create_index("ix_gcr_status", "gift_card_redemptions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_gcr_status", table_name="gift_card_redemptions")
    op.drop_index("ix_gcr_user", table_name="gift_card_redemptions")
    op.drop_table("gift_card_redemptions")

    op.drop_index("ix_checkin_restaurant_date", table_name="checkins")
    op.drop_index("ix_checkin_user", table_name="checkins")
    op.drop_table("checkins")

    op.drop_index("ix_points_txn_user_type", table_name="points_transactions")
    op.drop_index("ix_points_txn_user_created", table_name="points_transactions")
    op.drop_table("points_transactions")

    op.drop_index("ix_users_referred_by", table_name="users")
    op.drop_column("users", "points_balance")
    op.drop_index("ix_users_referral_code", table_name="users")
    op.drop_column("users", "referral_code")
    op.drop_column("users", "referred_by_user_id")
    op.drop_column("users", "email_verified")
