"""اسکیماهای API اتصالات دیتابیس خارجی — فاز ۲.۲ گام ۳ (docs/PHASE2_PLAN.md §6)

قرارداد امنیتی §3.6:
  - `password` فقط در ورودی (write-only) است و encrypt می‌شود؛ هرگز در پاسخ برنمی‌گردد
  - پاسخ‌ها `has_stored_credentials: bool` دارند — هیچ فیلد رمز، hint ماسک‌شده یا DSN وجود ندارد
  - `engine` در Phase 2.2 فقط postgresql می‌پذیرد (اعتبارسنجی در اسکیما)
"""

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class DatabaseConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    engine: Literal["postgresql"] = "postgresql"
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=5432, ge=1, le=65535)
    database_name: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=255)
    # write-only — encrypt و ذخیره می‌شود؛ در هیچ پاسخی برنمی‌گردد
    password: str = Field(min_length=1, max_length=1024)
    ssl_mode: Optional[Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]] = None


class DatabaseConnectionOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    engine: str
    host: str
    port: int
    database_name: str
    username: str
    ssl_mode: Optional[str] = None
    enabled: bool
    status: str
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None  # فقط متن sanitized (بدون رمز/DSN)
    has_stored_credentials: bool = True
    created_at: datetime
    updated_at: datetime


class ConnectionTestOut(BaseModel):
    id: uuid.UUID
    status: str  # ok / failed
    ok: bool
    latency_ms: Optional[int] = None
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None


class TableOut(BaseModel):
    schema_name: str
    name: str
    qualified_name: str


class SampleRowsOut(BaseModel):
    table: str
    limit: int
    rows: list[dict[str, Any]]
