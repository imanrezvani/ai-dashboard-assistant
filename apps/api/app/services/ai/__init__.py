"""بسته لایه AI — فاز ۲.۵ گام ۲ (docs/PHASE5_AI_PLAN.md §2.3)

نمای عمومی: LlmProvider ABC + خطاهای typed sanitized + NarrationResult +
validate_narration + factory با قرارداد None-when-unconfigured.

گام ۲ فقط انتزاع + یک ارائه‌دهنده است؛ هیچ endpoint ای وجود ندارد (گام ۳) و
هیچ فراخوانی شبکه‌ای در تست‌ها انجام نمی‌شود (MockTransport — §8.2).
"""

from app.services.ai.base import (
    AiBadResponseError,
    AiProviderError,
    AiTimeoutError,
    LlmProvider,
    MAX_HIGHLIGHTS,
    MAX_HIGHLIGHT_CHARS,
    MAX_SUMMARY_CHARS,
    NarrationResult,
    validate_narration,
)
from app.services.ai.factory import get_llm_provider
from app.services.ai.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION, build_user_message
from app.services.ai.sambanova import SambaNovaProvider

__all__ = [
    "AiBadResponseError",
    "AiProviderError",
    "AiTimeoutError",
    "LlmProvider",
    "MAX_HIGHLIGHTS",
    "MAX_HIGHLIGHT_CHARS",
    "MAX_SUMMARY_CHARS",
    "NarrationResult",
    "SYSTEM_PROMPT",
    "SYSTEM_PROMPT_VERSION",
    "SambaNovaProvider",
    "build_user_message",
    "get_llm_provider",
    "validate_narration",
]
