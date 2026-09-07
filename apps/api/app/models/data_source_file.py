import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DataSourceFile(Base):
    """بایت‌های فایل آپلودی منبع داده — ماندگار (فاز ۲.۱، جایگزین PENDING_UPLOADS).

    - `content` روی PostgreSQL به‌صورت bytea ذخیره می‌شود (LargeBinary)؛
      روی dialectهای دیگر (SQLite در تست‌ها) معادل BLOB است.
    - ارتباط ۱:۱ با data_sources (هر آپلود یک DataSource جدید می‌سازد).
    - Tenant isolation سه‌لایه:
        ۱) ستون اجباری organization_id (FK با CASCADE)
        ۲) RLS ENABLE + FORCE + Fail-Closed در runtime (app/main.py)
        ۳) فیلتر organization_id در همه کوئری‌های FileStorage (app/core/storage.py)
    - حذف فایل با حذف data_source از طریق FK CASCADE انجام می‌شود.
    """

    __tablename__ = "data_source_files"
    __table_args__ = (
        UniqueConstraint("data_source_id", name="uq_data_source_files_data_source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
