import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DatabaseConnection(Base):
    """اتصال دیتابیس خارجی یک سازمان — فاز ۲.۲ گام ۱ (docs/PHASE2_PLAN.md §2).

    Tenant isolation سه‌لایه (مانند DataSourceFile / DataSourceColumn):
      ۱) ستون اجباری organization_id (FK با CASCADE)
      ۲) RLS ENABLE + FORCE + Fail-Closed در runtime (app/main.py)
      ۳) فیلتر organization_id در همه کوئری‌های لایه سرویس (گام‌های بعدی)

    امنیت اعتبارنامه:
      - `encrypted_password` فقط ciphertext است (app/core/credentials.py)؛
        plaintext هرگز persisted نمی‌شود و هیچ ستون/خاصیتی به نام password وجود ندارد
      - `last_error` sanitized است (بدون DSN/رمز) — توسط endpointها در گام‌های بعدی
    """

    __tablename__ = "database_connections"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_db_conn_org_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    engine: Mapped[str] = mapped_column(String(20), nullable=False, default="postgresql")  # فاز ۲.۲: فقط postgresql
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=5432)
    database_name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_password: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # فقط ciphertext
    ssl_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # مثلا require / verify-full
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")  # unknown / ok / failed
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # sanitized — بدون رمز/DSN
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
