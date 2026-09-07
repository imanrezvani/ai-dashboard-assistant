"""Alembic migration — فاز ۲.۱: ماندگاری فایل آپلودی + متادیتای ستون‌ها

دو جدول جدید برای حذف کامل PENDING_UPLOADS (dict حافظه‌ای):
  - data_source_files: بایت‌های فایل (bytea) با organization_id و FK به data_sources
  - data_source_columns: متادیتای ستون‌ها (نام/ترتیب/نوع/نقش map شده)

بدون تغییر در جداول موجود و بدون پالیسی RLS (مطابق قاعده ریپو: RLS در runtime
توسط app/main.py اعمال می‌شود — ENABLE + FORCE + Fail-Closed).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0002_persist_uploads"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_source_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("data_source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", postgresql.BYTEA(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("data_source_id", name="uq_data_source_files_data_source_id"),
    )
    op.create_index("ix_data_source_files_organization_id", "data_source_files", ["organization_id"])

    op.create_table(
        "data_source_columns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("data_source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("dtype", sa.String(length=50), nullable=True),
        sa.Column("mapped_role", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("data_source_id", "name", name="uq_data_source_columns_ds_name"),
    )
    op.create_index("ix_data_source_columns_organization_id", "data_source_columns", ["organization_id"])
    op.create_index("ix_data_source_columns_data_source_id", "data_source_columns", ["data_source_id"])


def downgrade() -> None:
    op.drop_index("ix_data_source_columns_data_source_id", table_name="data_source_columns")
    op.drop_index("ix_data_source_columns_organization_id", table_name="data_source_columns")
    op.drop_table("data_source_columns")
    op.drop_index("ix_data_source_files_organization_id", table_name="data_source_files")
    op.drop_table("data_source_files")
