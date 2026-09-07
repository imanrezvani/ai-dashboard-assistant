"""Alembic migration — هم‌ترازسازی nullable ستون‌های created_at با مدل‌ها

`alembic check` روی PostgreSQL واقعی (گیت فاز ۲.۱) سه مغایرت پیدا کرد:
  - organizations.created_at
  - users.created_at
  - memberships.created_at
baseline (0001_baseline) این ستون‌ها را با nullable=True ساخته، ولی مدل‌ها
`Mapped[datetime]` غیر-Optional دارند → NOT NULL.

این migration فقط nullability را هم‌تراز می‌کند — بدون هیچ تغییر داده:
هر سه ستون server_default=now() دارند و در عمل همیشه مقدار دارند.
(برای دیتابیس‌های قدیمی که ردیف NULL داشته باشند، ALTER با خطای صریح
متوقف می‌شود — fail-fast به‌جای drift پنهان.)
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_align_created_at_nullable"
down_revision = "0002_persist_uploads"
branch_labels = None
depends_on = None

_TABLES = ("organizations", "users", "memberships")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )
