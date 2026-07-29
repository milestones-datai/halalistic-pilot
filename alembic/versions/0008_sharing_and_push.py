"""Stage 9: push_subscriptions table.

No new columns on existing tables — the email_verified flag was added
in Stage 8. This migration is just the new push_subscriptions table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint", sa.String(2000), nullable=False, unique=True),
        sa.Column("p256dh", sa.String(200), nullable=False),
        sa.Column("auth", sa.String(100), nullable=False),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_push_sub_user", "push_subscriptions", ["user_id"])
    op.create_index("ix_push_sub_restaurant", "push_subscriptions", ["restaurant_id"])
    op.create_index("ix_push_sub_user_restaurant", "push_subscriptions",
                    ["user_id", "restaurant_id"])


def downgrade() -> None:
    op.drop_index("ix_push_sub_user_restaurant", table_name="push_subscriptions")
    op.drop_index("ix_push_sub_restaurant", table_name="push_subscriptions")
    op.drop_index("ix_push_sub_user", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
