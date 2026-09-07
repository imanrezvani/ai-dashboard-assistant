"""Alembic migration — فاز ۲.۲ گام ۱: جدول database_connections

مدل DatabaseConnection (docs/PHASE2_PLAN.md §2) — بدون هیچ تغییری در جداول موجود
و بدون پالیسی RLS (طبق قاعده ریپو: RLS در runtime توسط app/main.py اعمال می‌شود —
ENABLE + FORCE + Fail-Closed).

نکته nullability (درس گیت فاز ۲.۱): همه ستون‌های Mapped[...] غیر-Optional با
nullable=False ساخته می‌شوند تا alembic check پاک بماند.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0004_database_connections"
down_revision = "0003_align_created_at_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "database_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("engine", sa.String(length=20), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database_name", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("encrypted_password", postgresql.BYTEA(), nullable=False),
        sa.Column("ssl_mode", sa.String(length=20), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("organization_id", "name", name="uq_db_conn_org_name"),
    )
    op.create_index("ix_database_connections_organization_id", "database_connections", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_database_connections_organization_id", table_name="database_connections")
    op.drop_table("database_connections")
