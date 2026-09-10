"""ارائه‌دهنده SambaNova — فاز ۲.۵ گام ۲ (docs/PHASE5_AI_PLAN.md §7)

چرا SambaNova (طرح §7): API سازگار با OpenAI chat-completions — با httpx موجود در
requirements.txt فراخوانی می‌شود (بدون SDK جدید)، کلید bearer سمت-سرور، حالت
`response_format: {"type": "json_object"}` با طراحی narrator-only جور است و
هزینه per-request برای بریفینگ کم-حجم مناسب است.

مرزهای امنیتی:
  - کلید فقط در این ماژول و فقط در هدر Authorization استفاده می‌شود؛ هرگز در
    لاگ/خطا/پاسخ نمی‌آید
  - timeout سخت ۲۰ ثانیه؛ خطاها typed و sanitized (بدون کلید/URL کامل/بدنه خام)
  - temperature=0 برای determinism-of-phrasing؛ دقیقاً یک فراخوانی per request
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.services.ai.base import (
    AiBadResponseError,
    AiProviderError,
    AiTimeoutError,
    LlmProvider,
    NarrationResult,
    validate_narration,
)

# ثابت‌های §7 — با settings قابل override (گام ۳ آنها را وصل می‌کند)
DEFAULT_BASE_URL = "https://api.sambanova.ai/v1"
DEFAULT_MODEL = "Meta-Llama-3.3-70B-Instruct"
REQUEST_TIMEOUT_SECONDS = 20.0


class SambaNovaProvider(LlmProvider):
    """OpenAI-compatible chat-completions با httpx — کلید هرگز از ماژول خارج نمی‌شود."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        transport: httpx.BaseTransport | None = None,
    ):
        if not isinstance(api_key, str) or not api_key.strip():
            raise AiProviderError("api key must be a non-empty string")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,  # فقط برای تست (MockTransport) — production همیشه None
        )

    def narrate(self, packet: dict) -> NarrationResult:
        """یک فراخوانی chat-completions → NarrationResult؛ خطاها typed و sanitized."""
        from app.services.ai.prompts import SYSTEM_PROMPT, build_user_message

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(packet)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        try:
            response = self._client.post(
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.TimeoutException as exc:
            raise AiTimeoutError("provider timeout") from exc
        except httpx.HTTPError as exc:
            raise AiProviderError("provider unavailable") from exc

        if response.status_code != 200:
            # sanitized: هیچ بدنه‌ای — نه کلید، نه URL با query، نه echo مدل
            raise AiProviderError(f"provider returned status {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise AiBadResponseError("provider response is not valid JSON") from exc

        content = self._extract_content(body)
        if content is None:
            raise AiBadResponseError("provider response has no assistant content")

        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise AiBadResponseError("assistant content is not valid JSON") from exc

        return validate_narration(parsed)

    @staticmethod
    def _extract_content(body: Any) -> str | None:
        """استخراج محتوای پیام assistant از ساختار OpenAI-compatible — دفاعی."""
        if not isinstance(body, dict):
            return None
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        message = first.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        return content if isinstance(content, str) else None


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "REQUEST_TIMEOUT_SECONDS",
    "SambaNovaProvider",
]
