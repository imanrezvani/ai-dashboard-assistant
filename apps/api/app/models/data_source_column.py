import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DataSourceColumn(Base):
    """متادیتای ستون‌های هر منبع داده — ماندگار (فاز ۲.۱، جایگزین PENDING_UPLOADS).

    - هر ردیف یک ستون از فایل آپلودی (نام، ترتیب، نوع استنتاج‌شده از داده)
    - Mapped در map کردن، نقش ستون‌ها (measure/date/category/label) را ثبت می‌کند
    - Tenant isolation سه‌لایه (مانند DataSourceFile):
        ۱) ستون اجباری organization_id (FK با CASCADE)
        ۲) RLS ENABLE + FORCE + Fail-Closed در runtime (app/main.py)
        ۳) فیلتر organization_id در همه کوئری‌های این مدل
    """

    __tablename__ = "data_source_columns"
    __table_args__ = (
        UniqueConstraint("data_source_id", "name", name="uq_data_source_columns_ds_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # ترتیب ستون در فایل (۰-based)
    dtype: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # int64, float64, object, datetime64[ns], ...
    mapped_role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # measure, date, category, label
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
