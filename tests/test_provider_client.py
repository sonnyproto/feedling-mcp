from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


def _fake_client(monkeypatch, response_body: dict) -> list[dict]:
    calls: list[dict] = []

    class FakeClient:
        def __init__(self, timeout: float):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url: str, *, headers=None, json=None):
            calls.append({"url": url, "headers": headers or {}, "json": json or {}})
            return FakeResponse(200, response_body)

    monkeypatch.setattr(pc.httpx, "Client", FakeClient)
    return calls


@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("anthropic", "claude-sonnet-4-20250514", "https://api.anthropic.com/v1"),
        ("gemini", "gemini-2.5-flash", "https://generativelanguage.googleapis.com/v1beta"),
        ("deepseek", "deepseek-chat", "https://api.deepseek.com"),
        ("custom", "some-model", "https://custom.example/v1"),
    ],
)
def test_validate_config_accepts_direct_providers(provider, model, base_url):
    normalized, out_model, out_base_url = pc.validate_config(provider, model, base_url if provider == "custom" else "")

    assert out_model == model
    assert out_base_url == base_url
    if provider == "custom":
        assert normalized == "openai_compatible"
    else:
        assert normalized == provider


def test_anthropic_chat_completion_uses_messages_api(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "msg_test",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 4, "output_tokens": 1},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("anthropic", "claude-sonnet-4-20250514", "sk-ant-test"),
        [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "Say ok."},
        ],
        response_format={"type": "json_object"},
    )

    assert result["reply"] == "ok"
    assert result["provider"] == "anthropic"
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "sk-ant-test"
    assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert calls[0]["json"]["system"].startswith("system rules")
    assert calls[0]["json"]["messages"] == [{"role": "user", "content": "Say ok."}]


def test_gemini_chat_completion_uses_generate_content(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "responseId": "gemini_test",
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {"totalTokenCount": 5},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("gemini", "gemini-2.5-flash", "AIza-test"),
        [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "Say ok."},
        ],
        response_format={"type": "json_object"},
    )

    assert result["reply"] == "ok"
    assert result["provider"] == "gemini"
    assert calls[0]["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert calls[0]["headers"]["x-goog-api-key"] == "AIza-test"
    assert calls[0]["json"]["systemInstruction"] == {"parts": [{"text": "system rules"}]}
    assert calls[0]["json"]["contents"] == [{"role": "user", "parts": [{"text": "Say ok."}]}]
    assert calls[0]["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_deepseek_chat_completion_uses_openai_compatible_endpoint(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 5},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("deepseek", "deepseek-chat", "sk-ds-test"),
        [{"role": "user", "content": "Say ok."}],
    )

    assert result["reply"] == "ok"
    assert result["provider"] == "deepseek"
    assert calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-ds-test"
    assert calls[0]["json"]["model"] == "deepseek-chat"
