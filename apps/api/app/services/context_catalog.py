"""Context Catalog — builder خواندن-محور، deterministic و tenant-scoped (فاز ۲.۴ گام ۱ — docs/PHASE4_CONTEXT_CATALOG_PLAN.md §4/§5)

هدف: لایه زمینه ساختاریافته برای دستیار AI آینده — نه خود AI. پاسخ به
«چه داده‌ای داریم، هر منبع چه معنایی دارد، چه KPIهایی تعریف شده‌اند،
چه گروه‌بندی‌هایی ممکن است» — بدون هیچ عدد KPI محاسبه‌شده در کاتالوگ (§4؛
اعداد از compute/series موجود می‌آیند).

مرزهای طراحی (غیرقابل نقض):
  - **بدون جدول جدید و بدون migration** — همه متادیتا از جداول موجود
    RLS-protected خوانده می‌شود (data_sources/data_source_columns/
    kpi_definitions/fact_rows)؛ §2/§3
  - **محاسبه-on-read** (§3) — بدون cache/snapshot؛ همیشه به‌روز
  - **تنها ORM، هیچ SQL خامی** — همان قاعده فاز ۲.۳ §3.1 (فقط تجمیع‌های
    func.min/max/count — هیچ رشته SQL وجود ندارد)
  - **deterministic** — ترتیب ثابت، ابعاد صعودی (مرتب‌سازی در Python، مستقل از
    ترتیب DB)؛ ورودی DB یکسان → خروجی یکسان (فقط generated_at متغیر است و
    خارج از checkهای determinism؛ §4 rule 1)
  - **bounded** — هر لیست سقف صریح دارد (§8)؛ countها همیشه true total را می‌گویند
  - **بدون هیچ رازی** — از database_connections فقط provenance UUID روی منبع
    خوانده می‌شود؛ host/port/database_name/username/ciphertext هرگز وارد
    payload نمی‌شوند (§7.4؛ تست non-disclosure در گام ۲)
  - **tenant isolation سه‌لایه** — RLS ENABLE+FORCE+Fail-Closed (لایه ۱) +
    فیلتر صریح organization_id در همه کوئری‌ها (لایه ۳)؛ organization_id
    keyword-only و اجباری است — هرگز از کلاینت نمی‌آید (گام ۲)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import DataSource, DataSourceColumn, FactRow, KpiDefinition

# سقف‌های صریح (§8) — payload با construction محدود است
MAX_SOURCES = 200
MAX_KPIS = 200
MAX_COLUMNS_PER_SOURCE = 200
MAX_DIMENSION_VALUES = 50

SCHEMA_VERSION = 1


def _iso(dt: datetime | None) -> str | None:
    """datetime → ISO-8601 UTC با پسوند Z (قرارداد §4)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_date(d: date | None) -> str | None:
    """date → 'YYYY-MM-DD' یا None (قرارداد §4)."""
    if d is None:
        return None
    return d.isoformat()


def _coerce_org_uuid(organization_id: Any) -> uuid.UUID:
    """پذیرش UUID (یا رشته UUID) — هرگز None. fail-closed، نه سکوت."""
    if organization_id is None:
        raise ValueError("organization_id is required")
    if isinstance(organization_id, uuid.UUID):
        return organization_id
    return uuid.UUID(str(organization_id))


def _dimension_vocab(
    db: Session,
    *,
    organization_id: uuid.UUID,
    data_source_id: uuid.UUID,
    column,
) -> tuple[list[str], int]:
    """واژه‌نامه یک بعد — distinct non-NULL، مرتب صعودی، سقف MAX_DIMENSION_VALUES.

    - LIMIT MAX+1 فقط sentinel است (هزینه bounded مستقل از کاردینالیتی؛ §8)
    - true total با COUNT(DISTINCT) جداگانه می‌آید — countها دقیق می‌مانند
      حتی وقتی لیست truncate شده (§4: count = true total)
    """
    rows = (
        db.query(column)
        .filter(
            FactRow.organization_id == organization_id,
            FactRow.data_source_id == data_source_id,
            column.isnot(None),
        )
        .distinct()
        .limit(MAX_DIMENSION_VALUES + 1)
        .all()
    )
    listed = sorted({row[0] for row in rows})[:MAX_DIMENSION_VALUES]
    true_total = (
        db.query(func.count(func.distinct(column)))
        .filter(
            FactRow.organization_id == organization_id,
            FactRow.data_source_id == data_source_id,
            column.isnot(None),
        )
        .scalar()
    )
    return listed, int(true_total or 0)


def _build_coverage(db: Session, *, data_source_id: uuid.UUID, organization_id: uuid.UUID) -> dict[str, Any]:
    """پوشش fact-layer یک منبع — تجمیع‌های bounded org-scoped (§2.2/§5.4).

    هیچ کوئری‌ای fact_rows را بدون هر دو فیلتر (organization_id + data_source_id)
    اسکن نمی‌کند (§8). برای منبع بدون fact row → همه null/0/[] (قرارداد §4).
    """
    base_filters = (
        FactRow.organization_id == organization_id,
        FactRow.data_source_id == data_source_id,
    )

    fact_row_count = db.query(FactRow).filter(*base_filters).count()

    # بازه تاریخ — min/max روی ستون Date؛ NULL بودن هر دو یعنی هیچ تاریخ دار نیست
    date_min, date_max = (
        db.query(func.min(FactRow.dimension_date), func.max(FactRow.dimension_date))
        .filter(*base_filters)
        .first()
    )

    # freshness — max(created_at) = آخرین map موفق (map همیشه delete+re-insert است؛ §2.2)
    last_mapped_at = db.query(func.max(FactRow.created_at)).filter(*base_filters).scalar()

    category_values, category_total = _dimension_vocab(
        db, organization_id=organization_id, data_source_id=data_source_id, column=FactRow.dimension_category
    )
    label_values, label_total = _dimension_vocab(
        db, organization_id=organization_id, data_source_id=data_source_id, column=FactRow.dimension_label
    )

    return {
        "fact_row_count": fact_row_count,
        "last_mapped_at": _iso(last_mapped_at),
        "date_min": _iso_date(date_min),
        "date_max": _iso_date(date_max),
        "has_date_dimension": date_min is not None,
        "category_values": category_values,
        "category_value_count": category_total,
        "label_values": label_values,
        "label_value_count": label_total,
    }


def _build_source(db: Session, *, source: DataSource, organization_id: uuid.UUID) -> dict[str, Any]:
    """بلوک یک منبع داده — متادیتای ذخیره‌شده (§2.1) + پوشش مشتق (§2.2).

    ترتیب ستون‌ها بر اساس position؛ سقف MAX_COLUMNS_PER_SOURCE (§8).
    """
    columns = (
        db.query(DataSourceColumn)
        .filter(
            DataSourceColumn.organization_id == organization_id,
            DataSourceColumn.data_source_id == source.id,
        )
        .order_by(DataSourceColumn.position.asc(), DataSourceColumn.name.asc())
        .limit(MAX_COLUMNS_PER_SOURCE)
        .all()
    )

    return {
        "id": source.id,
        "name": source.name,
        "file_type": source.file_type,
        "status": source.status,
        "row_count": int(source.row_count or 0),
        "uploaded_at": _iso(source.uploaded_at),
        "database_connection_id": source.database_connection_id,  # فقط provenance — نه بیشتر (§2.3)
        "columns": [
            {
                "name": c.name,
                "position": int(c.position),
                "dtype": c.dtype,
                "mapped_role": c.mapped_role,
            }
            for c in columns
        ],
        "coverage": _build_coverage(db, data_source_id=source.id, organization_id=organization_id),
    }


def build_context_catalog(db: Session, *, organization_id: uuid.UUID) -> dict[str, Any]:
    """ساخت کاتالوگ زمینه سازمان — نقطه تماس اصلی لایه AI آینده (§5).

    - همه کوئری‌ها org-scoped هستند و تحت RLS context درخواست اجرا می‌شوند
    - ترتیب منابع: uploaded_at desc سپس name asc (ترتیب GET /data-sources +
      tiebreaker برای determinism کامل — uploaded_at به‌تنهایی یکتا نیست)
    - ترتیب KPIها: created_at desc سپس name asc (دقیقاً ترتیب GET /kpis)
    - summary.data_source_count / summary.kpi_count همیشه true total است —
      حتی وقتی لیست‌ها به سقف §8 truncate شده‌اند
    """
    org_uuid = _coerce_org_uuid(organization_id)

    sources = (
        db.query(DataSource)
        .filter(DataSource.organization_id == org_uuid)
        .order_by(DataSource.uploaded_at.desc(), DataSource.name.asc())
        .all()
    )
    kpis = (
        db.query(KpiDefinition)
        .filter(KpiDefinition.organization_id == org_uuid)
        .order_by(KpiDefinition.created_at.desc(), KpiDefinition.name.asc())
        .all()
    )

    data_sources_out = [
        _build_source(db, source=s, organization_id=org_uuid) for s in sources[:MAX_SOURCES]
    ]

    kpis_out = [
        {
            "id": k.id,
            "name": k.name,
            "description": k.description,
            "data_source_id": k.data_source_id,
            "aggregation": k.aggregation,
            "group_by": k.group_by,
            "granularity": k.granularity,
            "filters": k.filters or [],
            "date_from": _iso_date(k.date_from),
            "date_to": _iso_date(k.date_to),
            "enabled": bool(k.enabled),
        }
        for k in kpis[:MAX_KPIS]
    ]

    # date_span سراسری — روی منابع فهرست‌شده (bounded؛ §8)؛ مقایسه ISO
    # YYYY-MM-DD همان ترتیب زمانی است (zero-padded)
    date_mins = [
        s["coverage"]["date_min"] for s in data_sources_out if s["coverage"]["date_min"] is not None
    ]
    date_maxs = [
        s["coverage"]["date_max"] for s in data_sources_out if s["coverage"]["date_max"] is not None
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "organization_id": org_uuid,
        "summary": {
            "data_source_count": len(sources),
            "kpi_count": len(kpis),
            "date_span": {
                "min": min(date_mins) if date_mins else None,
                "max": max(date_maxs) if date_maxs else None,
            },
        },
        "data_sources": data_sources_out,
        "kpis": kpis_out,
    }
