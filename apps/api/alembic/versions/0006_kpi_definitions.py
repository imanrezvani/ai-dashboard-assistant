"""Alembic migration — فاز ۲.۳ گام ۱: جدول kpi_definitions

مدل KpiDefinition (docs/PHASE3_KPI_PLAN.md §2) — تعریف ساختاریافته KPI بدون SQL:
  - دقیقاً یک data_source_id برای هر KPI (FK CASCADE؛ حذف منبع KPIهایش را هم حذف می‌کند)
  - filters به‌صورت JSONB ساختاریافته ذخیره می‌شود (اعتبارسنجی در مدل؛ هرگز SQL)
  - unique name per organization (uq_kpi_org_name)

بدون پالیسی RLS (طبق قاعده ریپو: RLS در runtime توسط app/main.py اعمال می‌شود —
ENABLE + FORCE + Fail-Closed) و بدون تغییر در جداول موجود.

نکته parity (درس گیت‌های فاز ۲.۱/۲.۲): هر ستون Mapped[...] غیر-Optional با
nullable=False؛ ستون‌های index=True مدل با index در migration — تا alembic check پاک بماند.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0006_kpi_definitions"
down_revision = "0005_data_source_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kpi_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("data_source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("aggregation", sa.String(length=20), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=False),
        sa.Column("group_by", sa.String(length=20), nullable=True),
        sa.Column("granularity", sa.String(length=20), nullable=True),
        sa.Column("date_from", sa.Date(), nullable=True),
        sa.Column("date_to", sa.Date(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("organization_id", "name", name="uq_kpi_org_name"),
    )
    op.create_index("ix_kpi_definitions_organization_id", "kpi_definitions", ["organization_id"])
    op.create_index("ix_kpi_definitions_data_source_id", "kpi_definitions", ["data_source_id"])


def downgrade() -> None:
    op.drop_index("ix_kpi_definitions_data_source_id", table_name="kpi_definitions")
    op.drop_index("ix_kpi_definitions_organization_id", table_name="kpi_definitions")
    op.drop_table("kpi_definitions")
