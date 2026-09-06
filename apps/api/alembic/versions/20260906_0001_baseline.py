"""Alembic migration baseline — فاز ۲.۰

مدل‌های فاز ۱ (Organization, User, Membership, DataSource, FactRow) را دقیقاً
همان‌طور که `Base.metadata.create_all` می‌ساخت، ایجاد می‌کند. عمداً بدون:
  - پالیسی‌های RLS (طبق محدودیت فاز ۲.۰ — RLS در runtime توسط app/main.py اعمال می‌شود)
  - هیچ جدول یا feature جدید

نکته اجرا:
  - دیتابیس تازه:            alembic upgrade head
  - دیتابیس موجود (dev/prod): alembic stamp head  (schema از قبل وجود دارد)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", postgresql.ENUM("owner", "admin", "manager", "analyst", "viewer", name="roleenum", create_type=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.UniqueConstraint("user_id", "organization_id", name="uq_user_org"),
    )
    op.create_index("ix_memberships_organization_id", "memberships", ["organization_id"])

    op.create_table(
        "data_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("file_type", sa.String(length=20), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
    )
    op.create_index("ix_data_sources_organization_id", "data_sources", ["organization_id"])

    op.create_table(
        "fact_rows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("data_source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("measure_value", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("dimension_date", sa.Date(), nullable=True),
        sa.Column("dimension_category", sa.String(length=255), nullable=True),
        sa.Column("dimension_label", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_fact_rows_organization_id", "fact_rows", ["organization_id"])
    op.create_index("ix_fact_rows_data_source_id", "fact_rows", ["data_source_id"])


def downgrade() -> None:
    op.drop_index("ix_fact_rows_data_source_id", table_name="fact_rows")
    op.drop_index("ix_fact_rows_organization_id", table_name="fact_rows")
    op.drop_table("fact_rows")
    op.drop_index("ix_data_sources_organization_id", table_name="data_sources")
    op.drop_table("data_sources")
    op.drop_index("ix_memberships_organization_id", table_name="memberships")
    op.drop_table("memberships")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_organizations_slug", table_name="organizations")
    op.drop_table("organizations")
    postgresql.ENUM(name="roleenum").drop(op.get_bind(), checkfirst=True)
