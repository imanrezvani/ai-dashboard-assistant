"""قرارداد پنجره + لایه چسبان داده/روایت — فاز ۲.۵ گام ۳ (docs/PHASE5_AI_PLAN.md §2.1/§2.3/§3/§4)

جریان طرح §3:
  Authenticated request → Tenant/role authorization → Load KPI / eligible KPIs
  → Compute existing KPI series/results → Pure Insight Engine → Optional LLM narrator
  → Structured API response

این ماژول نقطه «محاسبه» و «روایت» آن جریان است تا راسترها لاغر بمانند و هیچ منطقی
دوباره نوشته نشود:

  - پنجره‌ها: درخواست analyst فقط window_days می‌دهد (clamp ۷..۳۶۵)؛ پنجره جاری
    [today−(n−1) .. today] و پنجره قبلی n روز مجاور آن است (دو پنجره برابر مجاور §2.1).
  - پنجره تعریف KPI: وقتی date_from/date_to ست شده، هر دو پنجره محاسباتی با آن
    intersect می‌شوند (طرح §4: «پنجره تعریف KPI با پنجره‌های محاسباتی intersect
    می‌شود»). اگر بعد از intersect فضای دو پنجره کامل نماند → WindowError
    (لایه API → 422 «invalid window» طبق §8.3؛ بریفینگ آن KPI را حذف می‌کند).
  - دسترسی داده: فقط ORM با فیلتر صریح organization_id + data_source_id (لایه ۳
    دفاع کنار RLS) — هیچ رشته SQL در این ماژول نیست؛ موتور pure گام ۱/۲ همچنان
    هیچ DBی نمی‌بیند (insights.py/kpi.py دست‌نخورده‌اند).
  - روایت: دقیقاً یک فراخوانی provider per request (§2.3)؛ خطای typed → کد
    sanitized؛ factory که None برگرداند → «provider_unavailable» (فروپاشی نرم:
    narrative null، پاسخ deterministic سالم می‌ماند).

tenant isolation مسئول لایه بالادستی است — توابع داده فقط با organization_id
تأییدشده صدا زده می‌شوند و هرگز آن را از کلاینت نمی‌گیرند.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.fact_row import FactRow
from app.models.kpi_definition import KpiDefinition
from app.services.ai.base import AiProviderError, LlmProvider, NarrationResult
from app.services.insights import ChangePacket, build_change_packet
from app.services.kpi import (
    FactRecord,
    GroupedResult,
    KpiComputationError,
    KpiResult,
    compute_kpi,
    compute_kpi_series,
)

# پنجره‌های طرح §3 — مقادیر documented، testable، هرگز از LLM نمی‌آیند
DEFAULT_WINDOW_DAYS = 28
MIN_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 365

# سقف bounded بریفینگ سازمانی (§3: «تعداد KPI/insight محدود»)
MAX_BRIEFING_KPIS = 20

# کد narrative_error وقتی AI پیکربندی نشده است (§3)
NARRATIVE_ERROR_UNAVAILABLE = "provider_unavailable"


class WindowError(ValueError):
    """window درخواستی با پنجره تعریف KPI جمع‌شدنی نیست (کد 422 لایه API)."""


@dataclass(frozen=True)
class ComparisonWindows:
    """دو پنجره برابر مجاور — از/تا inclusive؛ هر یک می‌تواند None باشد."""

    current: tuple[date, date] | None
    previous: tuple[date, date] | None
    window_days: int


# ---------- پنجره‌ها ----------

def clamp_window_days(window_days: int | None) -> int:
    """window_days → DEFAULT وقتی None؛ clamp به ۷..۳۶۵ (§3: window کلیپ می‌شود)."""
    if window_days is None:
        return DEFAULT_WINDOW_DAYS
    if window_days < MIN_WINDOW_DAYS:
        return MIN_WINDOW_DAYS
    if window_days > MAX_WINDOW_DAYS:
        return MAX_WINDOW_DAYS
    return window_days


def _intersect(
    window: tuple[date, date], start: date | None, end: date | None
) -> tuple[date, date] | None:
    """intersect پنجره محاسباتی با بازه [start..end]؛ نامعتبر → None."""
    lo = max(window[0], start) if start is not None else window[0]
    hi = min(window[1], end) if end is not None else window[1]
    if lo > hi:
        return None
    return (lo, hi)


def _is_complete(win: tuple[date, date] | None, n: int) -> bool:
    """پنجره intersect‌شده هنوز کامل n روزه است؟"""
    return win is not None and (win[1] - win[0]).days + 1 == n


def effective_windows(
    *, today: date, window_days: int, start: date | None = None, end: date | None = None
) -> ComparisonWindows:
    """دو پنجره برابر مجاور برای مقایسه §2.1 — با intersect پنجره تعریف KPI.

    - پنجره جاری [today−(n−1) .. today]؛ پنجره قبلی n روز مجاور آن
    - تعریف window (start/end از date_from/date_to KPI) هر دو پنجره را محدود می‌کند
    - بعد از intersect فضای کافی برای دو پنجره کامل نباشد → WindowError (422)
    """
    n = clamp_window_days(window_days)
    if n < 1:
        raise WindowError("window must be at least 1 day")

    cur = (today - timedelta(days=n - 1), today)
    prev = (cur[0] - timedelta(days=n), cur[0] - timedelta(days=1))

    cur_i = _intersect(cur, start, end)
    prev_i = _intersect(prev, start, end)

    # هر دو پنجره باید کامل n روزه بمانند — مقایسه ناعادلانه/پنجره ناقص هرگز عدد
    # نمی‌سازد (§8.3: invalid window → 422؛ بریفینگ چنین KPIای را حذف می‌کند)
    if not (_is_complete(cur_i, n) and _is_complete(prev_i, n)):
        raise WindowError(
            f"KPI date window ({start}..{end}) does not allow two complete "
            f"adjacent {n}-day periods ending today"
        )
    return ComparisonWindows(current=cur_i, previous=prev_i, window_days=n)


def build_period_definition(
    kpi: KpiDefinition, *, date_from: date, date_to: date
) -> KpiDefinition:
    """instance گذرای KpiDefinition با پنجره جایگزین‌شده — ورودی compute پنجره‌ای.

    همان تعریف validated KPI با فقط date_from/date_to پنجره‌ای؛ هیچ تغییری روی
    ردیف DB انجام نمی‌شود — instance هرگز add/merge نمی‌شود.
    """
    return KpiDefinition(
        id=kpi.id,
        organization_id=kpi.organization_id,
        data_source_id=kpi.data_source_id,
        name=kpi.name,
        description=kpi.description,
        aggregation=kpi.aggregation,
        filters=list(kpi.filters or []),
        group_by=kpi.group_by,
        granularity=kpi.granularity,
        date_from=date_from,
        date_to=date_to,
        enabled=kpi.enabled,
        created_at=kpi.created_at,
        updated_at=kpi.updated_at,
    )


# ---------- لایه داده (فقط ORM — همان الگوی compute/series) ----------

def load_org_fact_records(
    db: Session, *, organization_id, data_source_id
) -> list[FactRecord]:
    """fact_rows تنانت-scoped (RLS + فیلتر صریح) → FactRecord — بدون هیچ SQL خام."""
    rows = (
        db.query(FactRow)
        .filter(
            FactRow.organization_id == organization_id,
            FactRow.data_source_id == data_source_id,
        )
        .all()
    )
    return [FactRecord.from_fact_row(r) for r in rows]


def compute_period_artifacts(
    records: list[FactRecord], kpi: KpiDefinition, *, date_from: date, date_to: date
) -> tuple[KpiResult, GroupedResult | None]:
    """محاسبه یک پنجره با تعریف پنجره‌ای — فقط موتور pure گام ۲ (هیچ منطق تکراری).

    KPI گروه‌بندی‌دار → سری همان پنجره هم برمی‌گردد (برای movers گام ۱)؛
    KPI بدون group_by → سری None. خطاها typed موتور هستند (KpiDefinitionError/
    KpiComputationError) و لایه API آنها را به 422 نگاشت می‌کند.
    """
    period_def = build_period_definition(kpi, date_from=date_from, date_to=date_to)
    scalar = compute_kpi(records, period_def)
    series = (
        compute_kpi_series(records, period_def) if kpi.group_by is not None else None
    )
    return scalar, series


def build_packet_for_windows(
    db: Session,
    *,
    kpi: KpiDefinition,
    organization_id,
    windows: ComparisonWindows,
) -> ChangePacket:
    """packet تغییر §2.1 برای یک KPI روی دو پنجره برابر مجاور.

    تمام دسترسی DB و فراخوانی موتور همین‌جاست (routers فقط این را صدا می‌زنند)؛
    خطاهای typed موتور propagate می‌شوند تا لایه API آنها را نگاشت کند.
    """
    records = load_org_fact_records(
        db, organization_id=organization_id, data_source_id=kpi.data_source_id
    )
    assert windows.current is not None and windows.previous is not None  # دفاعی
    cur_scalar, cur_series = compute_period_artifacts(
        records, kpi, date_from=windows.current[0], date_to=windows.current[1]
    )
    prev_scalar, prev_series = compute_period_artifacts(
        records, kpi, date_from=windows.previous[0], date_to=windows.previous[1]
    )
    return build_change_packet(
        kpi_id=kpi.id,
        kpi_name=kpi.name,
        aggregation=kpi.aggregation,
        current=cur_scalar,
        previous=prev_scalar,
        current_series=cur_series,
        previous_series=prev_series,
    )


# ---------- روایت (حداکثر یک فراخوانی provider per request — §2.3) ----------

def packet_from_packet(packet: ChangePacket) -> dict[str, Any]:
    """packet گام ۱ → JSON خالص برای روایت (فقط packet — بدون credential/SQL/row خام §2.4)."""
    return packet.to_dict()


def briefing_narration_packet(
    packets: list[ChangePacket], *, window_days: int, flagged_count: int
) -> dict[str, Any]:
    """packet روایت بریفینگ — همه packetها در «یک» فراخوانی (بدون fan-out per KPI §2.3)."""
    return {
        "kind": "briefing",
        "window_days": window_days,
        "flagged_count": flagged_count,
        "kpis": [p.to_dict() for p in packets],
    }


def narrate_packet(
    provider: LlmProvider | None, packet: dict[str, Any]
) -> tuple[NarrationResult | None, str | None]:
    """یک فراخوانی روایت — خطاها typed → کد sanitized (§3).

    - provider None (AI پیکربندی نشده) → (None, "provider_unavailable")
    - موفقیت → (NarrationResult, None)
    - خطای typed → (None, code خطا — هرگز جزئیات/کلید/بدنه خام)
    """
    if provider is None:
        return None, NARRATIVE_ERROR_UNAVAILABLE
    try:
        result = provider.narrate(packet)
    except AiProviderError as exc:
        return None, getattr(exc, "code", "provider_error")
    return result, None


def narrate_with_factory(packet: dict[str, Any]) -> tuple[NarrationResult | None, str | None]:
    """narrate_packet با factory واقعی — خطای پیکربندی provider هرگز 500 نمی‌شود.

    نکته تست-seam: factory به‌صورت ماژول-سطح صدا زده می‌شود، پس override تست‌ها
    (`monkeypatch.setattr(app.services.ai.factory, "get_llm_provider", ...)`)
    بدون هیچ تغییری اینجا اثر می‌گذارد (الگوی established گام ۲).
    """
    from app.services.ai.factory import get_llm_provider  # lazy — پرهیز از import cycle

    try:
        provider = get_llm_provider()
    except AiProviderError:
        return None, "provider_error"
    return narrate_packet(provider, packet)


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MIN_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "MAX_BRIEFING_KPIS",
    "NARRATIVE_ERROR_UNAVAILABLE",
    "ComparisonWindows",
    "WindowError",
    "clamp_window_days",
    "effective_windows",
    "build_period_definition",
    "load_org_fact_records",
    "compute_period_artifacts",
    "build_packet_for_windows",
    "packet_from_packet",
    "briefing_narration_packet",
    "narrate_packet",
    "narrate_with_factory",
]
