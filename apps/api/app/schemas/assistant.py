"""اسکیماهای API لایه AI — فاز ۲.۵ گام ۳ (docs/PHASE5_AI_PLAN.md §3)

قراردادها:
  - شکل دقیق §3: مقادیر عددی decimal-string (هرگز float — قرارداد فاز ۲.۳)؛
    narrative فقط {summary, highlights} یا null؛ narrative_error فقط کدهای
    typed لایه provider («provider_unavailable» / «provider_error» / «bad_response»)
  - این اسکیما هیچ قاعده تجمیع/آستانه‌ای تکرار نمی‌کند — اعداد از موتور pure
    گام ۱ (app/services/insights.py) می‌آیند؛ اینجا فقط نمای wire است
  - organization_id در هیچ پاسخی وجود ندارد (هرگز از کلاینت هم پذیرفته نمی‌شود)
"""

from typing import Optional

from pydantic import BaseModel, Field


class NarrativeOut(BaseModel):
    """روایت validated مدل (فاز ۲.۵ گام ۲ — NarrationResult)."""

    summary: str
    highlights: list[str] = []


class MoverOut(BaseModel):
    """یک mover بُعدی — key/value/contribution decimal-string (§2.1)."""

    key: str
    value: str
    contribution: str


class KpiInsightOut(BaseModel):
    """خروجی GET /kpis/{id}/insight — شکل دقیق §3 طرح."""

    kpi_id: str
    kpi_name: str
    window_days: int
    current_period: Optional[dict] = None   # {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}
    previous_period: Optional[dict] = None
    current_value: Optional[str] = None     # decimal-string ۴رقم — هرگز float
    previous_value: Optional[str] = None
    change: Optional[str] = None
    change_pct: Optional[str] = None
    direction: str
    flagged: bool
    top_movers: list[MoverOut] = []
    narrative: Optional[NarrativeOut] = None
    narrative_error: Optional[str] = None


class BriefingKpiOut(BaseModel):
    """packet یک KPI داخل بریفینگ — همان ChangePacket JSON گام ۱، بدون فیلدهای narrative (§3)."""

    kpi_id: str
    kpi_name: str
    aggregation: str
    current_value: Optional[str] = None
    previous_value: Optional[str] = None
    change: Optional[str] = None
    change_pct: Optional[str] = None
    direction: str
    flagged: bool
    top_movers: list[MoverOut] = []


class BriefingOut(BaseModel):
    """خروجی GET /assistant/briefing — بریفینگ سازمانی bounded (§3)."""

    generated_at: str                      # ISO-8601 UTC
    window_days: int
    kpis: list[BriefingKpiOut] = []
    flagged_count: int = 0
    narrative: Optional[NarrativeOut] = None
    narrative_error: Optional[str] = None


__all__ = [
    "NarrativeOut",
    "MoverOut",
    "KpiInsightOut",
    "BriefingKpiOut",
    "BriefingOut",
]
