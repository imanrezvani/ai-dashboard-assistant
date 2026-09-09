"""موتور محاسباتی KPI — pure و بدون دیتابیس (فاز ۲.۳ گام ۲ — docs/PHASE3_KPI_PLAN.md §3)

مرزهای طراحی (غیرقابل نقض):
  - **هیچ SQL و هیچ دسترسی به دیتابیس** — ورودی مجموعه fact record در-حافظه است
  - حساب **Decimal** خالص؛ هرگز float (§3.2) — خروجی با ROUND_HALF_UP به ۴ رقم
    اعشار (Numeric(18,4)) quantize می‌شود
  - deterministic: ترتیب گروه‌ها صعودی، ورودی یکسان → خروجی یکسان، بدون اثر ترتیب DB
  - فیلترها فقط ساختارهای validated گام ۱ هستند (field/op allow-list) — نه SQL، نه expression
  - قرارداد null/empty (§3.3): empty → sum/count=0، avg/min/max=None، گروه‌ها=[]
    — موفقیت است نه خطا؛ ردیف‌های measure=None به‌طور دفاعی از همه تجمیع‌ها حذف می‌شوند
  - tenant isolation مسئولیت لایه بالادستی (DB/RLS/API) است — موتور هیچ کوئری
    سازمانی نمی‌زند و organization_id را نمی‌شناسد

نمای عمومی:
  - `FactRecord` — رکورد نرمال fact (measure/date/category/label) + adapter
    `FactRecord.from_fact_row` که با نام فیلدهای واقعی fact_rows کار می‌کند
    (measure_value/dimension_date/dimension_category/dimension_label) بدون import مدل
  - `compute_kpi(records, kpi)` — اسکالر بدون گروه‌بندی → KpiResult(value, rows)
  - `compute_kpi_series(records, kpi)` → GroupedResult(group_by, buckets) — فقط
    bucketهای مشاهده‌شده، بدون zero-fill (§3.5)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable

from app.models.kpi_definition import KpiDefinition, KpiDefinitionError, validate_kpi_definition

# مقیاس ستون measure_value در fact_rows: Numeric(18, 4) — خروجی همیشه ۴ رقم اعشار
VALUE_QUANT = Decimal("0.0001")


class KpiComputationError(ValueError):
    """ورودی محاسبه نامعتبر/غیرقابل‌نگاشت است (خطای typed — هرگز نتیجه اشتباه خاموش)."""


@dataclass(frozen=True)
class FactRecord:
    """رکورد نرمال fact برای موتور — فقط فیلدهای واقعی fact_rows.

    measure می‌تواند defensively None باشد (اسکیمای fact_rows آن را NOT NULL می‌کند؛
    §3.3: موتور همچنان None را از تجمیع‌ها حذف می‌کند).
    """

    measure: Decimal | None
    date: date | None
    category: str | None
    label: str | None

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        """تبدیل دقیق به Decimal — float ممنوع (دقت شناور به موتور راه ندارد)."""
        if isinstance(value, Decimal):
            return value
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise KpiComputationError(
                f"unmappable measure type {type(value).__name__}; expected Decimal/int/str"
            )
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise KpiComputationError(f"unmappable measure value: {value!r}") from exc

    @classmethod
    def from_fact_row(cls, row: Any) -> "FactRecord":
        """Adapter از ردیف fact_rows (نام فیلدهای واقعی مدل) به FactRecord.

        عمداً هیچ import مدل/DB ندارد — هر شیئی با این attributeها کار می‌کند
        (ORM instance، namedtuple، dict-like). ورودی نامعتبر → KpiComputationError.
        """
        measure = getattr(row, "measure_value", None)
        if measure is not None:
            measure = cls._to_decimal(measure)
        raw_date = getattr(row, "dimension_date", None)
        if raw_date is not None:
            if isinstance(raw_date, datetime):
                raw_date = raw_date.date()  # datetime → date (بدون timezone بازی)
            if not isinstance(raw_date, date):
                raise KpiComputationError(
                    f"unmappable dimension_date type {type(raw_date).__name__}"
                )
        dims: list[str | None] = []
        for name in ("dimension_category", "dimension_label"):
            v = getattr(row, name, None)
            if v is not None and not isinstance(v, str):
                v = str(v)  # مقادیر غیر-رشته‌ای dimension به رشته نرمال می‌شوند
            dims.append(v)
        return cls(measure=measure, date=raw_date, category=dims[0], label=dims[1])


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(VALUE_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class KpiResult:
    """نتیجه اسکالر بدون گروه‌بندی — value همیشه Decimal ۴رقم یا None."""

    value: Decimal | None
    rows: int

    def to_dict(self) -> dict[str, Any]:
        """نمای wire (§3.2): مقدار به‌صورت decimal-string تا دقت float از بین نرود."""
        return {"value": str(self.value) if self.value is not None else None, "rows": self.rows}


@dataclass(frozen=True)
class KpiBucket:
    """یک bucket گروه/سری — key رشته ISO-compatible، مرتب‌سازی صعودی."""

    key: str
    value: Decimal | None
    rows: int

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": str(self.value) if self.value is not None else None, "rows": self.rows}


@dataclass(frozen=True)
class GroupedResult:
    group_by: str
    buckets: tuple[KpiBucket, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"group_by": self.group_by, "buckets": [b.to_dict() for b in self.buckets]}


# ---------- اعمال پنجره تاریخ و فیلترها ----------

def _window_and_filter(records: list[FactRecord], kpi: KpiDefinition) -> list[FactRecord]:
    """پنجره inclusive تعریف + فیلترهای ساختاریافته — همه AND.

    - پنجره/فیلتر date روی dimension_date (هر دو طرف inclusive)؛ ردیف با تاریخ NULL
      با پنجره/فیلتر تاریخ match نمی‌شود (سمانتیک SQL)
    - category/label: eq/neq؛ NULL هرگز match نمی‌شود (§3.3)
    - فیلترها پیشاپیش validated هستند (گام ۱)؛ اینجا دفاعاً op نامعتبر → خطای typed
    """
    out: list[FactRecord] = []
    filters = list(kpi.filters or [])
    for rec in records:
        if kpi.date_from is not None or kpi.date_to is not None:
            if rec.date is None:
                continue
            if kpi.date_from is not None and rec.date < kpi.date_from:
                continue
            if kpi.date_to is not None and rec.date > kpi.date_to:
                continue
        matched = True
        for f in filters:
            field, op, value = f["field"], f["op"], f["value"]
            if field == "date":
                if rec.date is None:
                    matched = False
                elif op == "gte":
                    matched = rec.date >= date.fromisoformat(value)
                elif op == "lte":
                    matched = rec.date <= date.fromisoformat(value)
                else:
                    raise KpiComputationError(f"invalid date filter op {op!r}")
            else:
                dim = rec.category if field == "category" else rec.label
                if dim is None:
                    matched = False  # NULL هرگز match نمی‌شود
                elif op == "eq":
                    matched = dim == value
                elif op == "neq":
                    matched = dim != value
                else:
                    raise KpiComputationError(f"invalid {field} filter op {op!r}")
            if not matched:
                break
        if matched:
            out.append(rec)
    return out


# ---------- تجمیع ----------

def _aggregate(measures: list[Decimal], aggregation: str) -> Decimal | None:
    """تجمیع روی مقادیر Decimal — خروجی quantize به ۴ رقم (ROUND_HALF_UP)."""
    if aggregation == "count":
        return _quantize(Decimal(len(measures)))  # count هم Decimal ۴رقم — قرارداد یکنواخت
    if not measures:
        # empty contract: sum/count → 0؛ avg/min/max → None (§3.3)
        return _quantize(Decimal(0)) if aggregation == "sum" else None
    if aggregation == "sum":
        return _quantize(sum(measures, Decimal(0)))
    if aggregation == "avg":
        return _quantize(sum(measures, Decimal(0)) / Decimal(len(measures)))
    if aggregation == "min":
        return _quantize(min(measures))
    if aggregation == "max":
        return _quantize(max(measures))
    raise KpiComputationError(f"unsupported aggregation {aggregation!r}")


# ---------- bucket keys ----------

def _date_bucket_key(d: date, granularity: str) -> str:
    """کلید bucket تاریخ — ISO-compatible و deterministic.

    day   → YYYY-MM-DD (خود تاریخ)
    week  → YYYY-Www (هفته ISO — دوشنبه‌شروع؛ year از isocalendar تا مرز سال درست باشد)
    month → YYYY-MM (اول ماه)
    """
    if granularity == "day":
        return d.isoformat()
    if granularity == "week":
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    if granularity == "month":
        return f"{d.year:04d}-{d.month:02d}"
    raise KpiComputationError(f"unsupported granularity {granularity!r}")


def _dimension_value(rec: FactRecord, group_by: str) -> str | None:
    if group_by == "date":
        return rec.date.isoformat() if rec.date is not None else None
    if group_by == "category":
        return rec.category
    if group_by == "label":
        return rec.label
    raise KpiComputationError(f"unsupported group_by {group_by!r}")


# ---------- API موتور ----------

def _validated(kpi: KpiDefinition) -> KpiDefinition:
    """اعتبارسنجی دفاعی تعریف قبل از محاسبه (§3.6 — ردیف DB ممکن است از مسیر دیگری آمده باشد)."""
    try:
        kpi.validate_definition()
    except KpiDefinitionError:
        raise
    except Exception as exc:  # مقادیر DB غیرقابل‌اعتبارسنجی → خطای typed، نه خاموش
        raise KpiComputationError(f"unmappable KPI definition: {exc}") from exc
    return kpi


def _prepare(records: Iterable[FactRecord], kpi: KpiDefinition) -> list[FactRecord]:
    """نرمال‌سازی ورودی: فقط FactRecord، سپس پنجره/فیلتر — ورودی نامعتبر → خطای typed."""
    prepared: list[FactRecord] = []
    for rec in records:
        if not isinstance(rec, FactRecord):
            raise KpiComputationError(
                f"engine input must be FactRecord, got {type(rec).__name__}"
            )
        prepared.append(rec)
    return _window_and_filter(prepared, kpi)


def compute_kpi(records: Iterable[FactRecord], kpi: KpiDefinition) -> KpiResult:
    """اسکالر بدون گروه‌بندی (§4 /compute آینده این را مصرف می‌کند).

    اگر KPI گروه‌بندی داشته باشد، این تابع «کل» همان KPI است (جمع کل روی همه
    bucketها) — deterministic و بدون هیچ رفتار پنهان.
    """
    _validated(kpi)
    filtered = _prepare(records, kpi)
    measures = [r.measure for r in filtered if r.measure is not None]  # §3.3 دفاعی
    return KpiResult(value=_aggregate(measures, kpi.aggregation), rows=len(measures))


def compute_kpi_series(records: Iterable[FactRecord], kpi: KpiDefinition) -> GroupedResult:
    """گروه‌بندی/سری (§3.5) — فقط bucketهای مشاهده‌شده، مرتب‌سازی صعودی، بدون zero-fill.

    - date → bucket های day/week/month از ISO calendar؛ ردیف با تاریخ NULL bucket ندارد
    - category/label → فقط مقادیر non-NULL (bucket «NULL» وجود ندارد — §3.3)
    """
    _validated(kpi)
    if kpi.group_by is None:
        raise KpiDefinitionError("series requires group_by to be set")
    if kpi.group_by == "date" and not kpi.granularity:
        raise KpiDefinitionError("date series requires granularity")  # دفاعی؛ validate هم می‌گیرد

    filtered = _prepare(records, kpi)
    grouped: dict[str, list[FactRecord]] = {}
    for rec in filtered:
        key_val = _dimension_value(rec, kpi.group_by)
        if key_val is None:
            continue  # bucket NULL ساخته نمی‌شود (§3.3)
        if kpi.group_by == "date":
            key_val = _date_bucket_key(rec.date, kpi.granularity)  # type: ignore[arg-type]
        grouped.setdefault(key_val, []).append(rec)

    buckets: list[KpiBucket] = []
    for key in sorted(grouped.keys()):  # مرتب‌سازی صعودی — deterministic (§3.5)
        recs = grouped[key]
        measures = [r.measure for r in recs if r.measure is not None]
        buckets.append(KpiBucket(key=key, value=_aggregate(measures, kpi.aggregation), rows=len(measures)))
    return GroupedResult(group_by=kpi.group_by, buckets=tuple(buckets))


__all__ = [
    "FactRecord",
    "KpiResult",
    "KpiBucket",
    "GroupedResult",
    "KpiComputationError",
    "compute_kpi",
    "compute_kpi_series",
]
