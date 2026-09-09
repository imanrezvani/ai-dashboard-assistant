"""اسکیماهای API تعریف KPI — فاز ۲.۳ گام ۳ (docs/PHASE3_KPI_PLAN.md §4)

قراردادها:
  - اعتبارسنجی فقط از قواعد گام ۱ (app/models/kpi_definition.py) می‌آید —
    این ماژول قاعده تکراری نمی‌سازد؛ فقط همان قواعد را به خطای 422 FastAPI نگاشت می‌کند
  - organization_id هرگز writable نیست — از membership تأییدشده می‌آید (گام ۳)
  - مقدار compute به‌صورت decimal-string برمی‌گردد (قرارداد گام ۲ — بدون float)
"""

import uuid
from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.kpi_definition import AGGREGATIONS, GROUP_BYS, GRANULARITIES

# Literalها از همان ثابت‌های گام ۱ ساخته می‌شوند — یک منبع قاعده، دو لایه مصرف
AggregationLiteral = Literal["sum", "avg", "min", "max", "count"]
assert set(AggregationLiteral.__args__) == set(AGGREGATIONS)  # هم‌خوانی با گام ۱ (import-time guard)
GroupByLiteral = Literal["date", "category", "label"]
GranularityLiteral = Literal["day", "week", "month"]


class KpiFilterIn(BaseModel):
    """یک فیلتر ساختاریافته — شکل دقیق §3.4؛ محتوایش در validate_kpi_definition اعتبارسنجی می‌شود."""

    field: str
    op: str
    value: str


class KpiCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=500)
    data_source_id: uuid.UUID
    aggregation: AggregationLiteral
    filters: list[KpiFilterIn] = Field(default_factory=list)
    group_by: Optional[GroupByLiteral] = None
    granularity: Optional[GranularityLiteral] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    enabled: bool = True


class KpiUpdate(BaseModel):
    """PATCH جزئی — همه فیلدها optional؛ نتیجه نهایی کامل revalidate می‌شود."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=500)
    data_source_id: Optional[uuid.UUID] = None
    aggregation: Optional[AggregationLiteral] = None
    filters: Optional[list[KpiFilterIn]] = None
    group_by: Optional[GroupByLiteral] = None
    granularity: Optional[GranularityLiteral] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    enabled: Optional[bool] = None


class KpiOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    data_source_id: uuid.UUID
    name: str
    description: Optional[str] = None
    aggregation: str
    filters: list[dict[str, Any]] = []
    group_by: Optional[str] = None
    granularity: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    enabled: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class KpiComputeOut(BaseModel):
    """خروجی compute — قرارداد دقیق گام ۲: value به‌صورت decimal-string یا null."""

    kpi_id: uuid.UUID
    value: Optional[str] = None  # decimal-string (۴ رقم) — هرگز float
    rows: int
