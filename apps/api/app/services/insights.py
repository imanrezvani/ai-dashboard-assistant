"""موتور insight خالص — فاز ۲.۵ گام ۱ (docs/PHASE5_AI_PLAN.md §2.1/§2.2)

مرزهای طراحی (غیرقابل نقض):
  - هیچ SQL، هیچ دسترسی به دیتابیس، هیچ فراخوانی LLM/شبکه — ورودی فقط
    «مصنوعات از-پیش-محاسبه‌شده» موتور pure فاز ۲.۳ است (KpiResult / GroupedResult)
  - حساب Decimal خالص؛ خروجی quantize به ۴ رقم با ROUND_HALF_UP (Numeric(18,4))
  - deterministic: ورودی یکسان → خروجی یکسان؛ ترتیب bucketهای ورودی بی‌اثر است
  - tenant-agnostic: organization_id نمی‌شناسد؛ ایزولاسیون مسئول لایه بالادستی است
  - این «تشخیص تغییر با آستانه» است، نه علم ناهنجاری (§2.2) — بدون آمار/ML

قرارداد (§2.1):
  - empty/missing → direction="unknown"، flagged=False، مقادیر None — موفقیت است
    نه خطا؛ هرگز عدد ساختگی تولید نمی‌شود
  - تقسیم فقط برای change_pct؛ previous=0 → change_pct=None (تقسیم بر صفر وجود
    ندارد)؛ مبنای منفی با قدرمطلق مبنا تقسیم می‌شود تا علامت change_pct همیشه
    با علامت change هم‌علامت بماند
  - flagged: |change_pct| >= ANOMALY_THRESHOLD (±۲۵٪) و هر دو پنجره مقدار دارند
  - flat: change == 0 (مقایسه exact روی Decimal)
  - movers: فقط bucketهای مشاهده‌شده در هر دو پنجره و دارای مقدار non-None در
    هر دو سمت (بدون zero-fill، بدون عدد ساختگی)؛ مرتب‌سازی نزولی |contribution|
    با تای‌بریک key صعودی؛ سقف MAX_MOVERS=5

نمای عمومی:
  - Mover / ChangePacket — ساختارهای frozen خروجی؛ گام ۱ همین‌جا تمام می‌شود
    (روایت LLM گام ۲ و API گام ۳ فقط همین قرارداد را مصرف می‌کنند)
  - build_change_packet(...) — یک packet برای یک KPI از دو پنجره برابر مجاور
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.services.kpi import GroupedResult, KpiResult

# هم‌قرارداد با Numeric(18,4) — همان مقیاس/گردی موتور KPI (app/services/kpi.py)
VALUE_QUANT = Decimal("0.0001")

# §2.2 — آستانه تشخیص تغییر ±۲۵٪ (change detection؛ ناهنجاری آماری non-goal است)
ANOMALY_THRESHOLD = Decimal("0.25")

# سقف تعداد movers در هر packet (§2.1)
MAX_MOVERS = 5

# مقادیر مجاز direction (§2.1) — برای مصرف لایه‌های بالادستی (اسکیمای گام ۳)
DIRECTIONS = ("up", "down", "flat", "unknown")


class InsightInputError(ValueError):
    """ورودی موتور insight نامعتبر/غیرقابل‌استفاده است (خطای typed — هرگز نتیجه اشتباه خاموش)."""


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(VALUE_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Mover:
    """یک mover بُعدی — contribution = مقدار bucket جاری − همان bucket در پنجره قبلی."""

    key: str          # کلید bucket (مقدار category/label یا کلید ISO تاریخ)
    value: str        # decimal-string مقدار bucket در پنجره جاری
    contribution: str  # decimal-string تغییر نسبت به همان bucket پنجره قبلی

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "contribution": self.contribution}


@dataclass(frozen=True)
class ChangePacket:
    """packet تغییر یک KPI — واحد بریفینگ سازمانی (§2.1).

    همه مقدارهای عددی decimal-string هستند (قرارداد فاز ۲.۳ — float هرگز از
    مرز عبور نمی‌کند)؛ None یعنی «داده کافی نیست» و هرگز عدد ساختگی نیست.
    """

    kpi_id: uuid.UUID
    kpi_name: str
    aggregation: str
    current_value: str | None
    previous_value: str | None
    change: str | None      # current − previous
    change_pct: str | None  # تغییر نسبی ("0.1834" = +18.34%)؛ None وقتی previous صفر/None است
    direction: str          # up | down | flat | unknown
    flagged: bool           # |change_pct| >= ANOMALY_THRESHOLD
    top_movers: list[Mover]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kpi_id": str(self.kpi_id),
            "kpi_name": self.kpi_name,
            "aggregation": self.aggregation,
            "current_value": self.current_value,
            "previous_value": self.previous_value,
            "change": self.change,
            "change_pct": self.change_pct,
            "direction": self.direction,
            "flagged": self.flagged,
            "top_movers": [m.to_dict() for m in self.top_movers],
        }


# ---------- اعتبارسنجی دفاعی ورودی (خطای typed — نه نتیجه اشتباه خاموش) ----------

def _require_result(value: Any, name: str) -> KpiResult:
    if not isinstance(value, KpiResult):
        raise InsightInputError(f"{name} must be KpiResult, got {type(value).__name__}")
    return value


def _require_series(value: Any, name: str) -> GroupedResult:
    if not isinstance(value, GroupedResult):
        raise InsightInputError(f"{name} must be GroupedResult, got {type(value).__name__}")
    return value


# ---------- movers (§2.1) ----------

def _build_movers(current: GroupedResult, previous: GroupedResult) -> list[Mover]:
    """movers فقط از bucketهای مشترک دو پنجره — deterministic مستقل از ترتیب ورودی.

    - bucket فقط در یک پنجره → حذف (بدون zero-fill — صفر ساختگی ممنوع است)
    - مقدار None در هر سمت (مثلاً avg روی bucket خالی) → حذف (عدد ساختگی ممنوع)
    - مرتب‌سازی: نزولی |contribution|، تای‌بریک key صعودی؛ سقف MAX_MOVERS
    """
    prev_by_key = {b.key: b.value for b in previous.buckets}
    scored: list[tuple[Decimal, str, Decimal]] = []  # (contribution, key, current value)
    for b in current.buckets:
        if b.key not in prev_by_key:
            continue  # فقط bucketهای مشاهده‌شده در هر دو پنجره (§2.1 — بدون zero-fill)
        prev_val = prev_by_key[b.key]
        if b.value is None or prev_val is None:
            continue  # bucket بدون مقدار قابل‌مقایسه — هرگز عدد ساختگی (§2.1)
        contribution = _quantize(b.value - prev_val)
        scored.append((contribution, b.key, _quantize(b.value)))
    scored.sort(key=lambda t: (-abs(t[0]), t[1]))  # نزولی |contribution|؛ تای‌بریک key صعودی
    return [Mover(key=k, value=str(v), contribution=str(c)) for (c, k, v) in scored[:MAX_MOVERS]]


# ---------- API موتور ----------

def build_change_packet(
    *,
    kpi_id: uuid.UUID,
    kpi_name: str,
    aggregation: str,
    current: KpiResult,
    previous: KpiResult,
    current_series: GroupedResult | None = None,
    previous_series: GroupedResult | None = None,
) -> ChangePacket:
    """packet تغییر deterministic برای یک KPI از دو پنجره برابر مجاور (§2.1).

    ورودی‌ها مصنوعات از-پیش-محاسبه‌شده موتور pure فاز ۲.۳ هستند (هیچ SQL/DB/LLM):
      - current/previous: خروجی compute_kpi روی پنجره‌های هم‌طول مجاور (کل KPI)
      - current_series/previous_series: خروجی compute_kpi_series روی همان
        پنجره‌ها — فقط برای KPI گروه‌بندی‌دار؛ هر دو با هم یا هیچ‌کدام
    """
    if not isinstance(kpi_id, uuid.UUID):
        raise InsightInputError(f"kpi_id must be uuid.UUID, got {type(kpi_id).__name__}")
    if not isinstance(kpi_name, str) or not kpi_name.strip():
        raise InsightInputError("kpi_name must be a non-empty string")
    if not isinstance(aggregation, str) or not aggregation:
        raise InsightInputError("aggregation must be a non-empty string")
    current = _require_result(current, "current")
    previous = _require_result(previous, "previous")
    if (current_series is None) != (previous_series is None):
        raise InsightInputError("current_series and previous_series must be provided together")
    if current_series is not None:
        current_series = _require_series(current_series, "current_series")
        previous_series = _require_series(previous_series, "previous_series")
        if current_series.group_by != previous_series.group_by:
            raise InsightInputError(
                f"series group_by mismatch: {current_series.group_by!r} != {previous_series.group_by!r}"
            )

    # دفاعی: quantize ورودی‌ها به ۴ رقم — مقادیر موتور KPI از قبل quantize هستند،
    # اما قرارداد decimal-string ۴رقم باید مستقل از منبع ورودی برقرار بماند
    cur = _quantize(current.value) if current.value is not None else None
    prev = _quantize(previous.value) if previous.value is not None else None

    change: Decimal | None = None
    if cur is not None and prev is not None:
        change = _quantize(cur - prev)

    # تقسیم فقط برای change_pct؛ previous=0 → None (تقسیم بر صفر وجود ندارد §2.1)؛
    # مبنای منفی با قدرمطلق — تا علامت change_pct همیشه با علامت change یکی باشد
    change_pct: Decimal | None = None
    if change is not None and prev != 0:
        change_pct = _quantize(change / abs(prev))

    if change is None:
        direction = "unknown"  # empty/missing — موفقیت است نه خطا (§2.1)
    elif change == 0:
        direction = "flat"  # مقایسه exact روی Decimal
    elif change > 0:
        direction = "up"
    else:
        direction = "down"

    flagged = change_pct is not None and abs(change_pct) >= ANOMALY_THRESHOLD

    movers = _build_movers(current_series, previous_series) if current_series is not None else []

    return ChangePacket(
        kpi_id=kpi_id,
        kpi_name=kpi_name,
        aggregation=aggregation,
        current_value=str(cur) if cur is not None else None,
        previous_value=str(prev) if prev is not None else None,
        change=str(change) if change is not None else None,
        change_pct=str(change_pct) if change_pct is not None else None,
        direction=direction,
        flagged=flagged,
        top_movers=movers,
    )


__all__ = [
    "ANOMALY_THRESHOLD",
    "MAX_MOVERS",
    "DIRECTIONS",
    "VALUE_QUANT",
    "InsightInputError",
    "Mover",
    "ChangePacket",
    "build_change_packet",
]
