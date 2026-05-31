from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class ProviderError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    api_key: str
    base_url: str = ""


_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


def normalize_provider(provider: str) -> str:
    p = (provider or "").strip().lower().replace("-", "_")
    aliases = {
        "open_ai": "openai",
        "openai_compatible": "openai_compatible",
        "openrouter": "openrouter",
    }
    return aliases.get(p, p)


def default_base_url(provider: str) -> str:
    return _DEFAULT_BASE_URLS.get(normalize_provider(provider), "")


def public_config(config: dict) -> dict:
    provider = normalize_provider(str(config.get("provider") or ""))
    key_hint = str(config.get("api_key_hint") or "")
    return {
        "provider": provider,
        "model": str(config.get("model") or ""),
        "base_url": str(config.get("base_url") or ""),
        "api_key_hint": key_hint,
        "test_status": str(config.get("test_status") or "unknown"),
        "last_test_at": str(config.get("last_test_at") or ""),
        "created_at": str(config.get("created_at") or ""),
        "updated_at": str(config.get("updated_at") or ""),
    }


def validate_config(provider: str, model: str, base_url: str = "") -> tuple[str, str, str]:
    provider = normalize_provider(provider)
    model = (model or "").strip()
    base_url = (base_url or "").strip().rstrip("/")

    if provider not in {"openai", "openrouter", "openai_compatible"}:
        raise ProviderError("provider must be openai, openrouter, or openai_compatible")
    if not model or len(model) > 160:
        raise ProviderError("model required")
    if provider == "openai_compatible" and not base_url:
        raise ProviderError("base_url required for openai_compatible")
    if base_url and not (base_url.startswith("https://") or base_url.startswith("http://127.0.0.1")):
        raise ProviderError("base_url must be https:// or local http://127.0.0.1")
    if not base_url:
        base_url = default_base_url(provider)
    if not base_url:
        raise ProviderError("base_url unavailable for provider")
    return provider, model, base_url


def mask_api_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if len(key) <= 10:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def _headers(config: ProviderConfig) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    if normalize_provider(config.provider) == "openrouter":
        headers["HTTP-Referer"] = "https://feedling.app"
        headers["X-Title"] = "Feedling IO Hosted Runtime"
    return headers


def _extract_reply(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
            text = first.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    raise ProviderError("provider response had no usable reply text")


def chat_completion(
    config: ProviderConfig,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 700,
    temperature: float = 0.7,
    timeout: float = 60.0,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider, model, base_url = validate_config(
        config.provider, config.model, config.base_url
    )
    key = (config.api_key or "").strip()
    if not key:
        raise ProviderError("api_key required")

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max(1, min(int(max_tokens), 4096)),
    }
    if response_format:
        payload["response_format"] = response_format

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=_headers(ProviderConfig(provider, model, key, base_url)),
                json=payload,
            )
    except httpx.HTTPError as e:
        raise ProviderError(f"provider network error: {type(e).__name__}") from e

    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("code") or "")[:240]
            elif isinstance(err, str):
                detail = err[:240]
        except Exception:
            detail = resp.text[:240]
        suffix = f": {detail}" if detail else ""
        raise ProviderError(
            f"provider_http_{resp.status_code}{suffix}",
            status_code=resp.status_code,
        )

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")

    return {
        "reply": _extract_reply(body),
        "usage": body.get("usage") if isinstance(body.get("usage"), dict) else {},
        "raw_id": body.get("id", ""),
        "provider": provider,
        "model": model,
    }


def test_provider_key(config: ProviderConfig) -> dict[str, Any]:
    return chat_completion(
        config,
        [
            {
                "role": "system",
                "content": "You are a health check endpoint. Reply with exactly: ok",
            },
            {"role": "user", "content": "Say ok."},
        ],
        max_tokens=8,
        temperature=0.0,
        timeout=30.0,
    )

