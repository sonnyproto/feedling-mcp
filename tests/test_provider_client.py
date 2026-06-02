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


def test_openai_compatible_chat_completion_preserves_image_parts(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "vision ok"}}],
            "usage": {"total_tokens": 9},
        },
    )

    image_part = {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,abcd"},
    }
    result = pc.chat_completion(
        pc.ProviderConfig("openrouter", "openai/gpt-4.1-mini", "sk-or-test"),
        [{"role": "user", "content": [{"type": "text", "text": "look"}, image_part]}],
    )

    assert result["reply"] == "vision ok"
    content = calls[0]["json"]["messages"][0]["content"]
    assert content == [{"type": "text", "text": "look"}, image_part]


def test_anthropic_chat_completion_maps_image_parts(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "id": "msg_test",
            "content": [{"type": "text", "text": "vision ok"}],
            "usage": {"input_tokens": 7, "output_tokens": 2},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("anthropic", "claude-sonnet-4-20250514", "sk-ant-test"),
        [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abcd"}},
        ]}],
    )

    assert result["reply"] == "vision ok"
    content = calls[0]["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "abcd"},
    }


def test_gemini_chat_completion_maps_image_parts(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        {
            "responseId": "gemini_test",
            "candidates": [{"content": {"parts": [{"text": "vision ok"}]}}],
            "usageMetadata": {"totalTokenCount": 8},
        },
    )

    result = pc.chat_completion(
        pc.ProviderConfig("gemini", "gemini-2.5-flash", "AIza-test"),
        [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abcd"}},
        ]}],
    )

    assert result["reply"] == "vision ok"
    assert calls[0]["json"]["contents"] == [{
        "role": "user",
        "parts": [
            {"text": "look"},
            {"inline_data": {"mime_type": "image/jpeg", "data": "abcd"}},
        ],
    }]


# ---------------------------------------------------------------------------
# Thinking/reasoning-model support: the setup self-test must tolerate an empty
# reply (a 2xx where the model spent its whole budget on reasoning), while the
# chat path stays strict and HTTP errors are never swallowed. See
# provider_client.test_provider_key / chat_completion(require_reply=...).
# ---------------------------------------------------------------------------

# Bodies that decode to an EMPTY reply for each provider shape.
_GEMINI_EMPTY = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}
_OPENAI_EMPTY = {"choices": [{"message": {"content": ""}}]}
_ANTHROPIC_EMPTY = {"content": []}

_EMPTY_CASES = [
    (pc.ProviderConfig("gemini", "gemini-2.5-flash", "k"), _GEMINI_EMPTY),
    (pc.ProviderConfig("openai", "gpt-4o-mini", "k"), _OPENAI_EMPTY),
    (pc.ProviderConfig("anthropic", "claude-haiku-4-5", "k"), _ANTHROPIC_EMPTY),
]


def _fake_client_status(monkeypatch, status_code: int, response_body: dict) -> None:
    """Like _fake_client but lets the fake response carry a non-200 status."""

    class FakeClient:
        def __init__(self, timeout: float):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url: str, *, headers=None, json=None):
            return FakeResponse(status_code, response_body)

    monkeypatch.setattr(pc.httpx, "Client", FakeClient)


@pytest.mark.parametrize(("cfg", "body"), _EMPTY_CASES)
def test_require_reply_false_allows_empty_reply(monkeypatch, cfg, body):
    _fake_client(monkeypatch, body)
    out = pc.chat_completion(cfg, [{"role": "user", "content": "Say ok."}], require_reply=False)
    assert out["reply"] == ""


@pytest.mark.parametrize(("cfg", "body"), _EMPTY_CASES)
def test_chat_path_still_requires_a_reply(monkeypatch, cfg, body):
    _fake_client(monkeypatch, body)
    with pytest.raises(pc.ProviderError):
        pc.chat_completion(cfg, [{"role": "user", "content": "Say ok."}])


def test_setup_self_test_passes_for_empty_thinking_reply(monkeypatch):
    # gemini-2.5-* / deepseek-reasoner can return a 2xx with no text when the
    # token budget is consumed by reasoning. That still proves the key works.
    _fake_client(monkeypatch, _GEMINI_EMPTY)
    out = pc.test_provider_key(pc.ProviderConfig("gemini", "gemini-2.5-flash", "k"))
    assert out["reply"] == ""


def test_setup_self_test_still_fails_on_http_error(monkeypatch):
    # An invalid / quota'd key surfaces as an HTTP 4xx and must NOT be swallowed.
    _fake_client_status(monkeypatch, 429, {"error": {"message": "You exceeded your current quota"}})
    with pytest.raises(pc.ProviderError) as ei:
        pc.test_provider_key(pc.ProviderConfig("openai", "gpt-4o-mini", "k"))
    assert ei.value.status_code == 429


# A 2xx whose body is NOT a valid provider success shape (e.g. a gateway that
# answers 200 with `{}` or `{"error": ...}`) must still be rejected even on the
# lenient self-test path — otherwise setup "succeeds" but chat/send later fails
# on the same unusable body. The empty-reply allowance only applies when the
# provider's real success container is present (choices/candidates/content).
_MALFORMED_2XX = [
    (pc.ProviderConfig("gemini", "gemini-2.5-flash", "k"), {}),
    (pc.ProviderConfig("gemini", "gemini-2.5-flash", "k"), {"error": {"message": "boom"}}),
    (pc.ProviderConfig("openai", "gpt-4o-mini", "k"), {}),
    (pc.ProviderConfig("openai", "gpt-4o-mini", "k"), {"error": {"message": "boom"}}),
    (pc.ProviderConfig("anthropic", "claude-haiku-4-5", "k"), {}),
    (pc.ProviderConfig("anthropic", "claude-haiku-4-5", "k"), {"error": {"message": "boom"}}),
]


@pytest.mark.parametrize(("cfg", "body"), _MALFORMED_2XX)
def test_require_reply_false_still_rejects_malformed_2xx(monkeypatch, cfg, body):
    _fake_client(monkeypatch, body)  # HTTP 200, but not a valid provider success shape
    with pytest.raises(pc.ProviderError):
        pc.chat_completion(cfg, [{"role": "user", "content": "Say ok."}], require_reply=False)


def test_setup_self_test_rejects_malformed_2xx(monkeypatch):
    _fake_client(monkeypatch, {"error": {"message": "gateway returned 200 with an error body"}})
    with pytest.raises(pc.ProviderError):
        pc.test_provider_key(pc.ProviderConfig("openai", "gpt-4o-mini", "k"))
