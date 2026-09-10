"""تست‌های unit ارائه‌دهنده LLM — فاز ۲.۵ گام ۲ (docs/PHASE5_AI_PLAN.md §8.2)

بدون هیچ فراخوانی شبکه واقعی — SambaNovaProvider فقط با httpx.MockTransport
آزموده می‌شود؛ factory بدون کلید → None؛ نبودِ کلید در production → RuntimeError
(الگوی JWT_SECRET/ENCRYPTION_KEY).
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.ai import (
    AiBadResponseError,
    AiProviderError,
    AiTimeoutError,
    LlmProvider,
    NarrationResult,
    SambaNovaProvider,
    SYSTEM_PROMPT,
    get_llm_provider,
    validate_narration,
)

VALID_CONTENT = json.dumps({"summary": "درآمد رشد کرده است", "highlights": ["کدام KPI؟ فروش"]}, ensure_ascii=False)


def _ok_body(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _provider(handler) -> SambaNovaProvider:
    transport = httpx.MockTransport(handler)
    return SambaNovaProvider("sk-test-key-123", transport=transport)


def _packet() -> dict:
    return {
        "kpi_id": "00000000-0000-0000-0000-000000000001",
        "kpi_name": "فروش",
        "direction": "up",
        "change_pct": "0.2500",
        "top_movers": [],
    }


# ---------- request shape (§7) ----------


def test_request_shape_url_headers_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["content_type"] = request.headers.get("Content-Type")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body(VALID_CONTENT))

    provider = _provider(handler)
    result = provider.narrate(_packet())

    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer sk-test-key-123"
    assert captured["content_type"] == "application/json"
    body = captured["payload"]
    assert body["model"] == "Meta-Llama-3.3-70B-Instruct"
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == SYSTEM_PROMPT  # سیستم prompt ثابت نسخه‌دار
    assert body["messages"][1]["role"] == "user"
    assert json.loads(body["messages"][1]["content"]) == _packet()  # دقیقاً packet — بدون پیوست
    assert result == NarrationResult(summary="درآمد رشد کرده است", highlights=["کدام KPI؟ فروش"])


# ---------- success paths ----------


def test_success_returns_narration_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body(VALID_CONTENT))

    result = _provider(handler).narrate(_packet())
    assert isinstance(result, NarrationResult)
    assert result.summary == "درآمد رشد کرده است"
    assert result.highlights == ["کدام KPI؟ فروش"]


# ---------- error paths (sanitized) ----------


def test_non_200_is_sanitized_provider_error():
    raw_body = '{"error": "sk-test-key-123 leaked"}'  # کلید در بدنه — هرگز نباید بیرون بیاید

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=raw_body)

    with pytest.raises(AiProviderError) as excinfo:
        _provider(handler).narrate(_packet())
    assert "sk-test-key-123" not in str(excinfo.value)  # کلید هرگز در خطا نیست
    assert "leaked" not in str(excinfo.value)  # بدنه خام هرگز در خطا نیست


def test_malformed_json_body_is_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json{{{")

    with pytest.raises(AiBadResponseError):
        _provider(handler).narrate(_packet())


def test_missing_assistant_content_is_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(AiBadResponseError):
        _provider(handler).narrate(_packet())


def test_content_not_json_is_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body("I am prose, not JSON"))

    with pytest.raises(AiBadResponseError):
        _provider(handler).narrate(_packet())


def test_schema_violation_is_bad_response():
    bad_payloads = [
        {"summary": 42, "highlights": []},                      # summary غیر رشته‌ای
        {"summary": "s", "highlights": "not-a-list"},           # highlights آرایه نیست
        {"summary": "s"},                                       # کلید gمفقود
        {"summary": "s", "highlights": [], "extra": 1},         # کلید اضافه
        {"summary": "s", "highlights": [1, 2]},                 # عضو غیر رشته‌ای
        {"summary": "s", "highlights": [f"h{i}" for i in range(11)]},  # > 10 highlights
    ]
    for bad in bad_payloads:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body(json.dumps(bad)))

        with pytest.raises(AiBadResponseError):
            _provider(handler).narrate(_packet())


def test_timeout_is_typed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(AiTimeoutError):
        _provider(handler).narrate(_packet())


def test_connect_error_is_typed_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(AiProviderError) as excinfo:
        _provider(handler).narrate(_packet())
    assert excinfo.value.code == "provider_error"
    assert "connection refused" not in str(excinfo.value)  # sanitized


# ---------- validate_narration length caps (§2.4) ----------


def test_narration_length_caps_trim():
    payload = {"summary": "s" * 3000, "highlights": ["h" * 700] * 3}
    result = validate_narration(payload)
    assert len(result.summary) == 2000
    assert len(result.highlights) == 3 and all(len(h) == 500 for h in result.highlights)


# ---------- factory: None-when-unconfigured + production fail-fast ----------


def test_factory_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("SAMBANOVA_API_KEY", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    # settings.AI_API_KEY در dev sentinel است (پیکربندی‌نشده) → None
    assert get_llm_provider() is None


def test_factory_returns_provider_with_env_key(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "sk-env-key")
    monkeypatch.delenv("ENV", raising=False)
    provider = get_llm_provider()
    assert isinstance(provider, SambaNovaProvider)


def test_factory_resolves_sambanova_api_key_alias(monkeypatch):
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.setenv("SAMBANOVA_API_KEY", "sk-alias-key")
    monkeypatch.delenv("ENV", raising=False)
    assert isinstance(get_llm_provider(), SambaNovaProvider)


def test_factory_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai")
    with pytest.raises(AiProviderError):
        get_llm_provider()


def test_production_without_key_raises_runtime_error(monkeypatch):
    """قرارداد production (§7): ENV=production + بدون کلید → RuntimeError هنگام resolve."""
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("SAMBANOVA_API_KEY", raising=False)
    from app.core.config import settings as app_settings
    from app.services.ai import factory

    # settings.AI_API_KEY = sentinel dev (پیکربندی‌نشده) — fallback دیتابیس واقعی هم نیست
    monkeypatch.setattr(app_settings, "AI_API_KEY", "dev-only-ai-key-do-not-use-in-prod")
    with pytest.raises(RuntimeError):
        factory.get_llm_provider()


def test_production_with_key_returns_provider(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SAMBANOVA_API_KEY", "sk-prod-key")
    from app.services.ai import factory

    assert isinstance(factory.get_llm_provider(), SambaNovaProvider)


# ---------- ABC contract ----------


def test_abc_narrate_contract():
    class Dummy(LlmProvider):
        pass

    with pytest.raises(NotImplementedError):
        Dummy().narrate(_packet())
