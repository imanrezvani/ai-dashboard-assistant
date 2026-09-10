"""اسکیماهای Context Catalog — فاز ۲.۴ گام ۱ (docs/PHASE4_CONTEXT_CATALOG_PLAN.md §4)

قراردادها (قواعد §4 — همه توسط تست اجرا می‌شوند):
  - خروجی builder (dict) با این اسکیماها سریال می‌شود؛ گام ۲ همین اسکیماها را
    به‌عنوان response_model اندپوینت GET /context/catalog به‌کار می‌برد
  - **هیچ float** در payload وجود ندارد — فقط int/bool/str/None (قاعده §4 rule 4)
  - هیچ credential/secret فیلدی وجود ندارد — از database_connections فقط
    database_connection_id (provenance) منتقل می‌شود (§2.3/§7.4)
  - هیچ مقدار KPI محاسبه‌شده اینجا نیست — اعداد از compute/series موجود می‌آیند
  - countها true total را می‌گویند حتی وقتی لیست‌ها به سقف §8 truncate شده‌اند
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field


class CatalogDateSpan(BaseModel):
    """بازه تاریخ سراسری سازمان — null وقتی هیچ fact تاریخ‌داری وجود ندارد."""

    min: Optional[str] = None  # YYYY-MM-DD
    max: Optional[str] = None  # YYYY-MM-DD


class CatalogSummary(BaseModel):
    """rollup سازمانی — countها true total (نه طول لیست‌های truncate شده)."""

    data_source_count: int
    kpi_count: int
    date_span: CatalogDateSpan


class CatalogColumn(BaseModel):
    """یک ستون منبع — متادیتای ذخیره‌شده data_source_columns (فاز ۲.۱)."""

    name: str
    position: int
    dtype: Optional[str] = None
    mapped_role: Optional[str] = None  # measure|date|category|label|None


class CatalogCoverage(BaseModel):
    """پوشش fact-layer یک منبع — مشتق on-read از fact_rows (§2.2).

    - منبع بدون fact row → همه 0/None/[] (قرارداد §4 — موفقیت، نه خطا)
    - last_mapped_at = max(fact_rows.created_at) = آخرین map موفق
    - لیست‌های واژه‌نامه به MAX_DIMENSION_VALUES محدودند؛
      *_value_count همیشه true total distinct است
    """

    fact_row_count: int
    last_mapped_at: Optional[str] = None  # ISO-8601 UTC (Z)
    date_min: Optional[str] = None  # YYYY-MM-DD
    date_max: Optional[str] = None  # YYYY-MM-DD
    has_date_dimension: bool
    category_values: list[str] = []
    category_value_count: int
    label_values: list[str] = []
    label_value_count: int


class CatalogDataSource(BaseModel):
    """یک منبع داده — متادیتای ذخیره‌شده + پوشش مشتق؛ بدون هیچ secret."""

    id: uuid.UUID
    name: str
    file_type: str  # csv|xlsx|xls|postgres
    status: str  # pending|mapped|failed
    row_count: int
    uploaded_at: Optional[str] = None  # ISO-8601 UTC (Z)
    database_connection_id: Optional[uuid.UUID] = None  # فقط provenance (پاریته DataSourceOut)
    columns: list[CatalogColumn] = []
    coverage: CatalogCoverage


class CatalogKpi(BaseModel):
    """تعریف KPI — دقیقاً همان تعریف ذخیره‌شده؛ بدون هیچ مقدار محاسبه‌شده."""

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    data_source_id: uuid.UUID
    aggregation: str  # sum|avg|min|max|count
    group_by: Optional[str] = None  # date|category|label|None
    granularity: Optional[str] = None  # day|week|month|None
    filters: list[dict[str, Any]] = []  # ساختاریافته، همان‌طور که ذخیره شده — هرگز SQL
    date_from: Optional[str] = None  # YYYY-MM-DD
    date_to: Optional[str] = None  # YYYY-MM-DD
    enabled: bool


class ContextCatalogOut(BaseModel):
    """پاکت نسخه‌دار کاتالوگ — قرارداد کامل §4.

    - schema_version از ۱ شروع می‌شود؛ هر تغییر شکستن‌دهنده bump می‌گیرد تا لایه AI
      آینده بتواند drift شکل را بدون خواندن کد تشخیص دهد
    - generated_at فقط اطلاعاتی است و بخشی از checkهای determinism نیست
    """

    schema_version: int = Field(default=1)
    generated_at: str
    organization_id: uuid.UUID
    summary: CatalogSummary
    data_sources: list[CatalogDataSource] = []
    kpis: list[CatalogKpi] = []
