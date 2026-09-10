"""لایه انتزاع ارائه‌دهنده LLM — فاز ۲.۵ گام ۲ (docs/PHASE5_AI_PLAN.md §2.3)

مرزهای طراحی (غیرقابل نقض):
  - LLM فقط روایت‌گر است — هرگز در مسیر عددی نیست؛ هرگز SQL/credential/row خام نمی‌بیند
  - خطاها typed و sanitized هستند: هرگز کلید API، کوئری URL، یا بدنه خام پاسخ مدل
    (بدنه مدل می‌تواند داده tenant را echo کند — لاگ/خطا عاری از داده tenant می‌ماند)
  - خروجی مدل فقط پس از schema-validation و طول‌سقف پذیرفته می‌شود (§2.4)
  - این لایه هیچ SQL/DB ندارد؛ ورودی packet از موتور pure گام ۱ می‌آید
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any

# سقف‌های §2.4 — خروجی مدل پیش از پذیرش به این سقف‌ها trim می‌شود
MAX_SUMMARY_CHARS = 2000
MAX_HIGHLIGHTS = 10
MAX_HIGHLIGHT_CHARS = 500


@dataclass(frozen=True)
class NarrationResult:
    """روایت validated مدل — «کلمات از مدل، اعداد از کد»."""

    summary: str
    highlights: list[str]


class AiProviderError(Exception):
    """ارائه‌دهنده در دسترس نیست / پاسخ موفق نداد (sanitized — بدون کلید/DSN/بدنه خام)."""

    def __init__(self, message: str = "provider unavailable"):
        super().__init__(message)
        self.code = "provider_error"


class AiTimeoutError(AiProviderError):
    """فراخوانی مدل از سقف زمانی عبور کرد (sanitized)."""

    def __init__(self, message: str = "provider timeout"):
        super().__init__(message)
        self.code = "provider_unavailable"


class AiBadResponseError(AiProviderError):
    """پاسخ 2xx ولی payload مدل معتبر نبود (JSON/schema/طول) — sanitized."""

    def __init__(self, message: str = "bad provider response"):
        super().__init__(message)
        self.code = "bad_response"


class LlmProvider(ABC):
    """ABC ارائه‌دهنده — تنها نقطه توسعه‌پذیری (rotation چند-provider non-goal است)."""

    def narrate(self, packet: dict) -> NarrationResult:
        """packet ساختاریافته گام ۱ → NarrationResult؛ خطاها typed و sanitized."""
        raise NotImplementedError


def _as_text(value: Any) -> str | None:
    """مقدار را به رشته نرمال می‌کند — فقط str یا int/float ساده؛ None/سایر → None."""
    if isinstance(value, str):
        return value
    return None


def validate_narration(payload: Any) -> NarrationResult:
    """اعتبارسنجی خروجی مدل در برابر قرارداد §2.4.

    شکل الزامی: {"summary": str, "highlights": [str, ...]} — مقادیر غیر-رشته‌ای
    رد می‌شوند (هرگز coerce خاموش؛ مدل فقط باید عبارت‌سازی کند)؛ طول‌سقف‌ها:
    summary ≤ 2000، highlights ≤ 10 با هرکدام ≤ 500.

    Raises:
        AiBadResponseError: payload معتبر نیست (شکل/نوع/طول).
    """
    if not isinstance(payload, dict):
        raise AiBadResponseError("narration payload must be a JSON object")
    if set(payload.keys()) != {"summary", "highlights"}:
        raise AiBadResponseError("narration payload must have exactly 'summary' and 'highlights'")
    summary = _as_text(payload["summary"])
    if summary is None:
        raise AiBadResponseError("narration summary must be a string")
    raw_highlights = payload["highlights"]
    if not isinstance(raw_highlights, list):
        raise AiBadResponseError("narration highlights must be an array")
    if len(raw_highlights) > MAX_HIGHLIGHTS:
        raise AiBadResponseError(f"narration highlights must have at most {MAX_HIGHLIGHTS} items")
    highlights: list[str] = []
    for item in raw_highlights:
        text = _as_text(item)
        if text is None:
            raise AiBadResponseError("each narration highlight must be a string")
        highlights.append(text[:MAX_HIGHLIGHT_CHARS])
    return NarrationResult(summary=summary[:MAX_SUMMARY_CHARS], highlights=highlights)


__all__ = [
    "MAX_SUMMARY_CHARS",
    "MAX_HIGHLIGHTS",
    "MAX_HIGHLIGHT_CHARS",
    "NarrationResult",
    "AiProviderError",
    "AiTimeoutError",
    "AiBadResponseError",
    "LlmProvider",
    "validate_narration",
]
