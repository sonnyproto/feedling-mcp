from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

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
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "deepseek": "https://api.deepseek.com",
}


def normalize_provider(provider: str) -> str:
    p = (provider or "").strip().lower().replace("-", "_")
    aliases = {
        "anthropic": "anthropic",
        "claude": "anthropic",
        "compatible": "openai_compatible",
        "custom": "openai_compatible",
        "custom_endpoint": "openai_compatible",
        "deep_seek": "deepseek",
        "deepseek": "deepseek",
        "gemini": "gemini",
        "google": "gemini",
        "google_gemini": "gemini",
        "open_ai": "openai",
        "openai_compatible": "openai_compatible",
        "open_router": "openrouter",
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

    if provider not in {
        "openai",
        "openrouter",
        "anthropic",
        "gemini",
        "deepseek",
        "openai_compatible",
    }:
        raise ProviderError(
            "provider must be openai, openrouter, anthropic, gemini, "
            "deepseek, or openai_compatible"
        )
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


def _response_error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or err.get("code") or err.get("status") or ""
                return str(detail)[:240]
            if isinstance(err, str):
                return err[:240]
            message = body.get("message")
            if isinstance(message, str):
                return message[:240]
    except Exception:
        pass
    return resp.text[:240]


def _raise_for_provider_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    detail = _response_error_detail(resp)
    suffix = f": {detail}" if detail else ""
    raise ProviderError(
        f"provider_http_{resp.status_code}{suffix}",
        status_code=resp.status_code,
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            elif isinstance(part, str) and part.strip():
                parts.append(part.strip())
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _json_only_instruction(response_format: dict[str, Any] | None) -> str:
    if not response_format:
        return ""
    if response_format.get("type") in {"json_object", "json_schema"}:
        return "Return only a valid JSON object. Do not wrap it in Markdown."
    return ""


def _append_text_message(
    messages: list[dict[str, str]],
    *,
    role: str,
    content: str,
) -> None:
    content = content.strip()
    if not content:
        return
    if messages and messages[-1].get("role") == role:
        messages[-1]["content"] = f"{messages[-1]['content']}\n\n{content}"
    else:
        messages.append({"role": role, "content": content})


def _split_system_messages(
    messages: list[dict[str, Any]],
    *,
    assistant_role: str,
) -> tuple[str, list[dict[str, str]]]:
    system_parts: list[str] = []
    provider_messages: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        content = _content_text(message.get("content"))
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        mapped_role = (
            assistant_role
            if role in {"assistant", "openclaw", "agent", "model"}
            else "user"
        )
        _append_text_message(provider_messages, role=mapped_role, content=content)
    if not provider_messages:
        provider_messages.append({"role": "user", "content": "Say ok."})
    return "\n\n".join(system_parts).strip(), provider_messages


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


def _extract_anthropic_reply(body: dict[str, Any]) -> str:
    content = body.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return "\n".join(parts).strip()
    raise ProviderError("provider response had no usable reply text")


def _extract_gemini_reply(body: dict[str, Any]) -> str:
    candidates = body.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            text_parts: list[str] = []
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        text_parts.append(text.strip())
            if text_parts:
                return "\n".join(text_parts).strip()
    raise ProviderError("provider response had no usable reply text")


def _chat_completion_openai_compatible(
    *,
    provider: str,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    response_format: dict[str, Any] | None,
) -> dict[str, Any]:
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

    _raise_for_provider_status(resp)

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


def _chat_completion_anthropic(
    *,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    response_format: dict[str, Any] | None,
) -> dict[str, Any]:
    system, provider_messages = _split_system_messages(
        messages,
        assistant_role="assistant",
    )
    json_instruction = _json_only_instruction(response_format)
    if json_instruction:
        system = f"{system}\n\n{json_instruction}".strip()
    payload: dict[str, Any] = {
        "model": model,
        "messages": provider_messages,
        "max_tokens": max(1, min(int(max_tokens), 4096)),
        "temperature": temperature,
    }
    if system:
        payload["system"] = system

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as e:
        raise ProviderError(f"provider network error: {type(e).__name__}") from e

    _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")

    return {
        "reply": _extract_anthropic_reply(body),
        "usage": body.get("usage") if isinstance(body.get("usage"), dict) else {},
        "raw_id": body.get("id", ""),
        "provider": "anthropic",
        "model": model,
    }


def _chat_completion_gemini(
    *,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    response_format: dict[str, Any] | None,
) -> dict[str, Any]:
    system, split_messages = _split_system_messages(messages, assistant_role="model")
    contents = [
        {"role": message["role"], "parts": [{"text": message["content"]}]}
        for message in split_messages
    ]
    generation_config: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max(1, min(int(max_tokens), 4096)),
    }
    if response_format and response_format.get("type") in {"json_object", "json_schema"}:
        generation_config["responseMimeType"] = "application/json"

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/models/{quote(model, safe='')}:generateContent",
                headers={
                    "x-goog-api-key": key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as e:
        raise ProviderError(f"provider network error: {type(e).__name__}") from e

    _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")

    return {
        "reply": _extract_gemini_reply(body),
        "usage": (
            body.get("usageMetadata")
            if isinstance(body.get("usageMetadata"), dict)
            else {}
        ),
        "raw_id": body.get("responseId", ""),
        "provider": "gemini",
        "model": model,
    }


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

    if provider == "anthropic":
        return _chat_completion_anthropic(
            model=model,
            base_url=base_url,
            key=key,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            response_format=response_format,
        )
    if provider == "gemini":
        return _chat_completion_gemini(
            model=model,
            base_url=base_url,
            key=key,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            response_format=response_format,
        )

    return _chat_completion_openai_compatible(
        provider=provider,
        model=model,
        base_url=base_url,
        key=key,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        response_format=response_format,
    )


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
