"""Alembic migration — فاز ۲.۲ گام ۴: provenance فیلد import bridge

یک ستون nullable به data_sources اضافه می‌شود:
  - database_connection_id: FK به database_connections.id با ON DELETE SET NULL
    → حذف اتصال، منابع داده import شده و fact_rows آن‌ها را دست‌نخورده می‌گذارد
      (فقط خط منشأ NULL می‌شود) — همان رفتار §7 طرح.

بدون تغییر در هیچ جدول/ستون دیگری و بدون پالیسی RLS (طبق قاعده ریپو:
RLS در runtime توسط app/main.py اعمال می‌شود — ENABLE + FORCE + Fail-Closed).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0005_data_source_provenance"
down_revision = "0004_database_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_sources",
        sa.Column(
            "database_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("database_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_data_sources_database_connection_id", "data_sources", ["database_connection_id"])


def downgrade() -> None:
    op.drop_index("ix_data_sources_database_connection_id", table_name="data_sources")
    op.drop_column("data_sources", "database_connection_id")
