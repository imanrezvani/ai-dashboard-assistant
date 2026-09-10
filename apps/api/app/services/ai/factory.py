"""Factory ارائه‌دهنده LLM — فاز ۲.۵ گام ۲ (docs/PHASE5_AI_PLAN.md §2.3/§7)

قرارداد اصلی: **None وضعیت معتبری است.** بدون کلید (dev) یا با provider ناشناخته،
`get_llm_provider()` مقدار None برمی‌گرداند و لایه API پاسخ deterministic را با
`narrative: null` می‌دهد — خرابی/نبودِ AI هرگز مسیر عددی را نمی‌شکند.

کلید با ترتیب AI_API_KEY → SAMBANOVA_API_KEY resolve می‌شود. نبودِ کلید فقط
production خطا است (الگوی _get_secret)؛ در dev None وضعیت normal است.

این ماژول تنها نقطه override تست‌هاست (seam ماژول-سطح — الگوی established):
`monkeypatch.setattr(app.services.ai.factory, "get_llm_provider", lambda: stub)`.
"""

from __future__ import annotations

import os

from app.services.ai.base import AiProviderError, LlmProvider

# تنها مقدار پیاده‌شده در فاز ۲.۵ (rotation چند-provider non-goal است §9)
SUPPORTED_PROVIDERS = ("sambanova",)

DEFAULT_BASE_URL = "https://api.sambanova.ai/v1"
DEFAULT_MODEL = "Meta-Llama-3.3-70B-Instruct"


def _resolve_api_key() -> str | None:
    """ترتیب AI_API_KEY → SAMBANOVA_API_KEY → settings (که خودش .env را هم می‌خواند).

    sentinel dev مقدار settings برابر «پیکربندی‌نشده» تفسیر می‌شود.
    """
    for name in ("AI_API_KEY", "SAMBANOVA_API_KEY"):
        val = os.getenv(name)
        if val:  # مقدار خالی نادیده گرفته می‌شود
            return val
    from app.core.config import AI_API_KEY_DEV_FALLBACK, settings  # lazy — پرهیز از import cycle

    if settings.AI_API_KEY and settings.AI_API_KEY != AI_API_KEY_DEV_FALLBACK:
        return settings.AI_API_KEY
    return None


def get_llm_provider() -> LlmProvider | None:
    """provider پیکربندی‌شده یا None — None یعنی narrative لایه API خواهد بود null.

    Raises:
        AiProviderError: AI_PROVIDER ناشناخته است (پیکربندی اشتباه — fail-fast حتی در dev).
    """
    # production fail-fast: نبودِ کلید الگوی JWT_SECRET/ENCRYPTION_KEY را دنبال می‌کند
    if os.getenv("ENV") == "production" and _resolve_api_key() is None:
        raise RuntimeError("SAMBANOVA_API_KEY must be set in production when AI narration is enabled")

    provider_name = os.getenv("AI_PROVIDER", "sambanova").strip().lower()
    if provider_name != "sambanova":
        raise AiProviderError(f"unsupported AI_PROVIDER {provider_name!r}; supported: {SUPPORTED_PROVIDERS}")

    api_key = _resolve_api_key()
    if api_key is None:
        return None  # dev بدون کلید — وضعیت معتبر، narrative: null

    from app.services.ai.sambanova import SambaNovaProvider

    return SambaNovaProvider(
        api_key,
        base_url=os.getenv("AI_BASE_URL", DEFAULT_BASE_URL),
        model=os.getenv("AI_MODEL", DEFAULT_MODEL),
    )


__all__ = ["get_llm_provider", "SUPPORTED_PROVIDERS", "DEFAULT_BASE_URL", "DEFAULT_MODEL"]
