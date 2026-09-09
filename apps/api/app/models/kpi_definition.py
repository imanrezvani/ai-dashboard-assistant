import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# مقادیر مجاز تعریف KPI — منبع واحد قواعد (docs/PHASE3_KPI_PLAN.md §2/§3.4/§3.6)
AGGREGATIONS = ("sum", "avg", "min", "max", "count")
GROUP_BYS = ("date", "category", "label")
GRANULARITIES = ("day", "week", "month")
MAX_FILTERS = 10

# شکل ساختاریافته فیلترها — هرگز SQL نیست (§3.4):
#   {"field": "category" | "label", "op": "eq" | "neq", "value": str}
#   {"field": "date", "op": "gte" | "lte", "value": "YYYY-MM-DD"}
FILTER_FIELDS = ("category", "label", "date")
FILTER_OPS = ("eq", "neq", "gte", "lte")

_FILTER_OP_BY_FIELD: dict[str, tuple[str, ...]] = {
    "category": ("eq", "neq"),
    "label": ("eq", "neq"),
    "date": ("gte", "lte"),
}


class KpiDefinitionError(ValueError):
    """تعریف KPI نامعتبر است (اعتبارسنجی مدل — قبل از محاسبه در گام‌های بعدی)."""


def validate_kpi_definition(
    *,
    aggregation: str,
    group_by: Optional[str],
    granularity: Optional[str],
    filters: Optional[list],
    date_from: Optional[date],
    date_to: Optional[date],
) -> None:
    """اعتبارسنجی ساختاریافته تعریف KPI (فاز ۲.۳ گام ۱ — فقط قواعد تعریف).

    قواعد (docs/PHASE3_KPI_PLAN.md §2/§3.4/§3.6):
      - aggregation ∈ AGGREGATIONS
      - group_by ∈ GROUP_BYS ∪ NULL
      - granularity ∈ GRANULARITIES ∪ NULL؛ **اجباری iff group_by == "date"**
      - filters: لیست JSONB ساختاریافته — حداکثر MAX_FILTERS؛ فیلد/op از
        allow-list؛ هر فیلد فقط op های مجاز خودش؛ هیچ SQL نمی‌پذیرد
      - پنجره تاریخ: date_from <= date_to وقتی هر دو هستند (inclusive)

    Raises:
        KpiDefinitionError: نقض هر قاعده.
    """
    if aggregation not in AGGREGATIONS:
        raise KpiDefinitionError(f"aggregation must be one of {AGGREGATIONS}, got {aggregation!r}")

    if group_by is not None and group_by not in GROUP_BYS:
        raise KpiDefinitionError(f"group_by must be one of {GROUP_BYS} or None, got {group_by!r}")

    if granularity is not None and granularity not in GRANULARITIES:
        raise KpiDefinitionError(f"granularity must be one of {GRANULARITIES} or None, got {granularity!r}")

    if (group_by == "date") != (granularity is not None):
        raise KpiDefinitionError(
            "granularity is required iff group_by == 'date' "
            f"(group_by={group_by!r}, granularity={granularity!r})"
        )

    if filters is not None:
        if not isinstance(filters, list):
            raise KpiDefinitionError("filters must be a JSON array")
        if len(filters) > MAX_FILTERS:
            raise KpiDefinitionError(f"at most {MAX_FILTERS} filters are allowed, got {len(filters)}")
        for f in filters:
            if not isinstance(f, dict):
                raise KpiDefinitionError("each filter must be a JSON object")
            field = f.get("field")
            op = f.get("op")
            value = f.get("value")
            if field not in FILTER_FIELDS:
                raise KpiDefinitionError(f"filter field must be one of {FILTER_FIELDS}, got {field!r}")
            if op not in _FILTER_OP_BY_FIELD[field]:
                raise KpiDefinitionError(
                    f"filter op {op!r} is not allowed for field {field!r} "
                    f"(allowed: {_FILTER_OP_BY_FIELD[field]})"
                )
            if not isinstance(value, str) or not (1 <= len(value) <= 255):
                raise KpiDefinitionError("filter value must be a string of 1..255 characters")
            if field == "date":
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise KpiDefinitionError(f"date filter value must be YYYY-MM-DD, got {value!r}") from exc

    if date_from is not None and date_to is not None and date_from > date_to:
        raise KpiDefinitionError(f"date_from ({date_from}) must be <= date_to ({date_to})")


class KpiDefinition(Base):
    """تعریف KPI یک سازمان — فاز ۲.۳ گام ۱ (docs/PHASE3_KPI_PLAN.md §2).

    - دقیقاً یک data_source_id برای هر KPI (بدون join بین منابع؛ §2/§9)
    - `filters` ساختاریافته است و هرگز SQL نیست (§3.4) — validate در همین ماژول
    - Tenant isolation سه‌لایه (الگوی همه جداول داده):
        ۱) ستون اجباری organization_id (FK با CASCADE)
        ۲) RLS ENABLE + FORCE + Fail-Closed در runtime (app/main.py)
        ۳) فیلتر organization_id در کوئری‌های لایه سرویس (گام‌های بعدی)
    - بدون cache/snapshot، بدون ستون مقایسه، بدون سیستم metric مشتق (§9)
    """

    __tablename__ = "kpi_definitions"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_kpi_org_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    # دقیقاً یک منبع داده برای هر KPI؛ حذف منبع → حذف fact_rows و KPIهای آن (CASCADE)
    data_source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    aggregation: Mapped[str] = mapped_column(String(20), nullable=False)  # sum/avg/min/max/count
    filters: Mapped[list] = mapped_column(JSON().with_variant(postgresql.JSONB, "postgresql"), nullable=False, default=list)  # ساختاریافته — هرگز SQL
    group_by: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # date/category/label
    granularity: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # day/week/month؛ اجباری iff group_by=date
    date_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # پنجره inclusive
    date_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def validate_definition(self) -> None:
        """اعتبارسنجی قواعد تعریف روی مقادیر این ردیف (بدون محاسبه — گام ۲)."""
        validate_kpi_definition(
            aggregation=self.aggregation,
            group_by=self.group_by,
            granularity=self.granularity,
            filters=self.filters,
            date_from=self.date_from,
            date_to=self.date_to,
        )
