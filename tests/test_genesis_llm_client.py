from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client  # noqa: E402
from genesis.llm_client import GenesisLLMClient  # noqa: E402


def _runtime():
    return provider_client.ProviderConfig(
        provider="openai",
        model="gpt-test",
        api_key="sk-user-secret",
        base_url="https://api.openai.com/v1",
    )


def test_genesis_llm_client_ignores_plaintext_legacy_cache(monkeypatch):
    from genesis import llm_client

    captured = {}
    monkeypatch.setattr(
        llm_client.db,
        "genesis_upsert_output",
        lambda user_id, job_id, output_type, *, doc, status, ref: captured.update(
            {
                "user_id": user_id,
                "job_id": job_id,
                "output_type": output_type,
                "doc": doc,
                "status": status,
                "ref": ref,
            }
        ),
    )

    def fake_completion(runtime, messages, **_kwargs):
        assert runtime.api_key == "sk-user-secret"
        assert messages[0]["content"] == "hello"
        return {"reply": "fresh text", "usage": {"total_tokens": 4}}

    result = GenesisLLMClient(completion_fn=fake_completion).complete(
        user_id="usr",
        job_id="job",
        task_id="map-1",
        runtime=_runtime(),
        messages=[{"role": "user", "content": "hello"}],
        idempotency_key="job:map:1",
    )

    assert result.cached is False
    assert result.text == "fresh text"
    assert captured["doc"]["plaintext_stored"] is False
    assert "text" not in captured["doc"]


def test_genesis_llm_client_persists_response_metadata_without_plaintext_or_api_key(monkeypatch):
    from genesis import llm_client

    captured = {}
    monkeypatch.setattr(
        llm_client.db,
        "genesis_upsert_output",
        lambda user_id, job_id, output_type, *, doc, status, ref: captured.update(
            {
                "user_id": user_id,
                "job_id": job_id,
                "output_type": output_type,
                "doc": doc,
                "status": status,
                "ref": ref,
            }
        ),
    )

    def fake_completion(runtime, messages, **kwargs):
        assert runtime.api_key == "sk-user-secret"
        assert messages[0]["content"] == "hello"
        assert kwargs["max_tokens"] == 321
        return {"reply": "new text", "usage": {"total_tokens": 9}}

    result = GenesisLLMClient(completion_fn=fake_completion).complete(
        user_id="usr",
        job_id="job",
        task_id="map-1",
        runtime=_runtime(),
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=321,
        idempotency_key="job:map:1",
    )

    assert result.cached is False
    assert result.text == "new text"
    assert captured["status"] == "done"
    assert captured["doc"]["plaintext_stored"] is False
    assert captured["doc"]["response_sha256"]
    assert captured["doc"]["response_chars"] == len("new text")
    assert "text" not in captured["doc"]
    assert captured["doc"]["usage"] == {"total_tokens": 9}
    assert "api_key" not in json.dumps(captured["doc"])
    assert "sk-user-secret" not in json.dumps(captured["doc"])
    assert "new text" not in json.dumps(captured["doc"])
