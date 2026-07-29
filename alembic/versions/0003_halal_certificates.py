"""halal_certificates + certifying_bodies tables (Stage 4)

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- certifying_bodies lookup ----
    op.create_table(
        "certifying_bodies",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("slug", sa.String(120), nullable=False, unique=True),
        sa.Column("country", sa.String(50), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )

    # ---- halal_certificates ----
    op.create_table(
        "halal_certificates",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("restaurant_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("uploaded_by", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("certifying_body_id", sa.Integer,
                  sa.ForeignKey("certifying_bodies.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("custom_certifying_body", sa.String(200), nullable=True),
        sa.Column("blob_name", sa.String(500), nullable=False),
        sa.Column("blob_url", sa.String(1000), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False, server_default="application/pdf"),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("issue_date", sa.Date, nullable=False),
        sa.Column("expiry_date", sa.Date, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("reviewed_by_admin_id", PG_UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text, nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "certifying_body_id IS NOT NULL OR custom_certifying_body IS NOT NULL",
            name="ck_cert_body_required",
        ),
    )
    op.create_index("ix_halal_certificates_restaurant_id", "halal_certificates", ["restaurant_id"])
    op.create_index("ix_halal_certificates_status", "halal_certificates", ["status"])
    op.create_index("ix_halal_certificates_expiry_date", "halal_certificates", ["expiry_date"])


def downgrade() -> None:
    op.drop_index("ix_halal_certificates_expiry_date", table_name="halal_certificates")
    op.drop_index("ix_halal_certificates_status", table_name="halal_certificates")
    op.drop_index("ix_halal_certificates_restaurant_id", table_name="halal_certificates")
    op.drop_table("halal_certificates")
    op.drop_table("certifying_bodies")
