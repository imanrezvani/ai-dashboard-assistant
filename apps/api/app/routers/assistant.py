"""راستر assistant — فاز ۲.۵ گام ۳ (docs/PHASE5_AI_PLAN.md §3)

تنها endpoint این راستر: GET /assistant/briefing (§3).

مرزهای امنیتی (طرح §3/§5):
  - analyst+ (["owner","admin","manager","analyst"])؛ viewer → 403؛ unauth → 401
  - هیچ path/query parameter ای برای org وجود ندارد — organization_id فقط از
    membership تأییدشده (require_role → require_membership → set_rls_context)
  - بریفینگ فقط KPIهای enabled با منبع usable را می‌بیند؛ ترتیب GET /kpis؛
    سقف bounded MAX_BRIEFING_KPIS؛ هیچ credential/SQL/داده org دیگر در پاسخ نیست
  - روایت = دقیقاً یک فراخوانی provider per request؛ خطای provider هرگز 500
    نمی‌شود (narrative: null + narrative_error) — فروپاشی نرم طبق قرارداد §3
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.middleware.tenant import require_role
from app.models.data_source import DataSource
from app.models.kpi_definition import KpiDefinition, KpiDefinitionError
from app.schemas.assistant import BriefingKpiOut, BriefingOut, MoverOut, NarrativeOut
from app.services.insight_packets import (
    MAX_BRIEFING_KPIS,
    WindowError,
    briefing_narration_packet,
    build_packet_for_windows,
    clamp_window_days,
    effective_windows,
    narrate_with_factory,
)
from app.services.kpi import KpiComputationError

router = APIRouter(prefix="/assistant", tags=["assistant"])

ANALYST_ROLES = ["owner", "admin", "manager", "analyst"]


@router.get("/briefing", response_model=BriefingOut)
def get_briefing(
    window_days: int | None = None,
    membership=Depends(require_role(ANALYST_ROLES)),
    db: Session = Depends(get_db),
):
    """بریفینگ سازمانی deterministic (§3) — همه KPIهای enabled در یک پاسخ bounded.

    - KPIها با ترتیب GET /kpis انتخاب می‌شوند (created_at desc, name asc)؛ سقف
      MAX_BRIEFING_KPIS — بریفینگ هرگز unbounded نیست
    - KPI با منبع map نشده یا پنجره تعریف نامعتبر برای این window → حذف از
      بریفینگ (خطا نیست — empty org رفتار valid §3 را دارد)
    - روایت: یک فراخوانی برای کل بریفینگ (بدون fan-out per KPI §2.3)؛ بدون AI
      پیکربندی‌شده → narrative: null + narrative_error="provider_unavailable"
    """
    organization_id = membership.organization_id
    n = clamp_window_days(window_days)

    # KPIهای enabled سازمان — همان ترتیب deterministic GET /kpis
    kpis = (
        db.query(KpiDefinition)
        .filter(
            KpiDefinition.organization_id == organization_id,
            KpiDefinition.enabled.is_(True),
        )
        .order_by(KpiDefinition.created_at.desc(), KpiDefinition.name)
        .limit(MAX_BRIEFING_KPIS)
        .all()
    )

    # منابع usable فقط (status == mapped) — سمانتیک compute/series؛ هیچ logic
    # تجمیعی اینجا نیست — فقط eligible selection طبق طرح §3
    source_ids = {k.data_source_id for k in kpis}
    usable_ids: set = set()
    if source_ids:
        rows = (
            db.query(DataSource.id)
            .filter(
                DataSource.id.in_(source_ids),
                DataSource.organization_id == organization_id,
                DataSource.status == "mapped",
            )
            .all()
        )
        usable_ids = {r[0] for r in rows}

    packets = []
    for kpi in kpis:
        if kpi.data_source_id not in usable_ids:
            continue  # منبع map نشده — حذف از بریفینگ (خطا نیست)
        try:
            wins = effective_windows(
                today=date.today(), window_days=n, start=kpi.date_from, end=kpi.date_to
            )
        except WindowError:
            continue  # پنجره تعریف KPI دو پنجره کامل نمی‌پذیرد — حذف (خطا نیست)
        try:
            packet = build_packet_for_windows(
                db, kpi=kpi, organization_id=organization_id, windows=wins
            )
        except (KpiDefinitionError, KpiComputationError):
            continue  # تعریف/داده خراب — حذف از بریفینگ (خطا نیست)
        packets.append(packet)

    flagged_count = sum(1 for p in packets if p.flagged)

    narrative, narrative_error = narrate_with_factory(
        briefing_narration_packet(packets, window_days=n, flagged_count=flagged_count)
    )

    return BriefingOut(
        generated_at=datetime.now(timezone.utc).isoformat(),
        window_days=n,
        kpis=[
            BriefingKpiOut(
                kpi_id=str(p.kpi_id),
                kpi_name=p.kpi_name,
                aggregation=p.aggregation,
                current_value=p.current_value,
                previous_value=p.previous_value,
                change=p.change,
                change_pct=p.change_pct,
                direction=p.direction,
                flagged=p.flagged,
                top_movers=[MoverOut(**m.to_dict()) for m in p.top_movers],
            )
            for p in packets
        ],
        flagged_count=flagged_count,
        narrative=(NarrativeOut(summary=narrative.summary, highlights=narrative.highlights) if narrative else None),
        narrative_error=narrative_error,
    )
