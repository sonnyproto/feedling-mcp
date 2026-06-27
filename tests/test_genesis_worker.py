from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import worker  # noqa: E402


def _chunk(seq: int, body: bytes = b"ct") -> dict:
    return {
        "seq": seq,
        "encrypted_body": body + str(seq).encode("ascii"),
        "aad": {
            "envelope_meta": {
                "v": 1,
                "id": f"chunk-{seq}",
                "nonce": "nonce",
                "K_user": "ku",
                "K_enclave": "ke",
                "visibility": "shared",
                "owner_user_id": "usr_1",
            }
        },
    }


class _Resp:
    def __init__(self, data: dict, status: int = 200):
        self._data = data
        self.status_code = status
        self.text = json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http_{self.status_code}")


def _install_success_harness(monkeypatch, *, source_kind: str, chunk_texts: list[str], blobs: dict | None = None):
    apply_payloads = []
    minted = []
    blobs = blobs or {}
    chunks = [_chunk(idx) for idx in range(len(chunk_texts))]
    plaintext_by_id = {f"chunk-{idx}": text for idx, text in enumerate(chunk_texts)}

    monkeypatch.setattr(worker, "get_store", lambda user_id: types.SimpleNamespace(user_id=user_id))
    monkeypatch.setattr(worker.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.service, "mark_failed", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no fail")))
    monkeypatch.setattr(
        worker.db,
        "genesis_claim_uploaded_jobs",
        lambda limit=1: [{
            "user_id": "usr_1",
            "job_id": "job_1",
            "status": "processing",
            "total_chunks": len(chunks),
            "source_kind": source_kind,
        }],
    )
    monkeypatch.setattr(worker.db, "genesis_missing_chunk_seqs", lambda *_args: [])
    monkeypatch.setattr(worker.db, "genesis_list_chunks", lambda *_args: chunks)
    monkeypatch.setattr(
        worker.db,
        "get_blob",
        lambda _user_id, kind: {
            "provider": "openai",
            "model": "gpt-test",
            "base_url": "https://api.openai.com/v1",
            "test_status": "ok",
        } if kind == "model_api" else blobs.get(kind),
    )
    monkeypatch.setattr(worker.httpx, "get", lambda *_args, **_kwargs: _Resp({"api_key_envelope": {"id": "provider-key"}}))

    def fake_post(url, *, headers=None, json=None, **_kwargs):
        assert headers == {"X-Feedling-Runtime-Token": "rtok"}
        if url.endswith("/v1/envelope/decrypt"):
            purpose = json["purpose"]
            if purpose == "model_api_provider_key":
                return _Resp({"plaintext_b64": base64.b64encode(b"sk-user").decode("ascii")})
            if purpose == "genesis_persona":
                return _Resp({"plaintext_b64": base64.b64encode(blobs["persona_plaintext"].encode()).decode("ascii")})
            if purpose == "genesis_voice":
                raw = blobs["voice_plaintext"]
                if isinstance(raw, dict):
                    raw = worker.json.dumps(raw, ensure_ascii=False)
                return _Resp({"plaintext_b64": base64.b64encode(str(raw).encode()).decode("ascii")})
            envelope_id = json["envelope"]["id"]
            return _Resp({"plaintext_b64": base64.b64encode(plaintext_by_id[envelope_id].encode()).decode("ascii")})
        assert url.endswith("/v1/genesis/imports/job_1/outputs")
        apply_payloads.append(json)
        return _Resp({"applied": {"ok": True}})

    monkeypatch.setattr(worker.httpx, "post", fake_post)

    def mint(user_id, scopes):
        minted.append({"user_id": user_id, "scopes": scopes})
        return "rtok"

    return apply_payloads, minted, mint


def test_source_family_accepts_import_suffix_aliases():
    assert worker._source_family("ai_persona_import") == "ai_persona"
    assert worker._source_family("character_import") == "ai_persona"
    assert worker._source_family("user_profile_import") == "user_profile"
    assert worker._source_family("memory_summary_import") == "memory_summary"
    assert worker._source_family("chat_export") == "history"


def test_tick_claims_decrypts_all_chunks_and_posts_distilled_output(monkeypatch):
    llm_calls = []
    apply_payloads = []
    minted = []

    monkeypatch.setattr(worker, "get_store", lambda user_id: types.SimpleNamespace(user_id=user_id))
    monkeypatch.setattr(worker.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.service, "mark_failed", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no fail")))
    monkeypatch.setattr(
        worker.db,
        "genesis_claim_uploaded_jobs",
        lambda limit=1: [{"user_id": "usr_1", "job_id": "job_1", "status": "processing", "total_chunks": 2}],
    )
    monkeypatch.setattr(worker.db, "genesis_missing_chunk_seqs", lambda *_args: [])
    monkeypatch.setattr(worker.db, "genesis_list_chunks", lambda *_args: [_chunk(0), _chunk(1)])
    monkeypatch.setattr(
        worker.db,
        "get_blob",
        lambda _user_id, kind: {
            "provider": "openai",
            "model": "gpt-test",
            "base_url": "https://api.openai.com/v1",
            "test_status": "ok",
        } if kind == "model_api" else None,
    )
    monkeypatch.setattr(
        worker.httpx,
        "get",
        lambda url, **kwargs: _Resp({"api_key_envelope": {"id": "provider-key"}}),
    )

    def fake_post(url, *, headers=None, json=None, **_kwargs):
        assert headers == {"X-Feedling-Runtime-Token": "rtok"}
        if url.endswith("/v1/envelope/decrypt"):
            purpose = json["purpose"]
            if purpose == "model_api_provider_key":
                return _Resp({"plaintext_b64": base64.b64encode(b"sk-user").decode("ascii")})
            envelope_id = json["envelope"]["id"]
            return _Resp({"plaintext_b64": base64.b64encode(f"text for {envelope_id}".encode()).decode("ascii")})
        assert url.endswith("/v1/genesis/imports/job_1/outputs")
        apply_payloads.append(json)
        return _Resp({"applied": {"ok": True}})

    monkeypatch.setattr(worker.httpx, "post", fake_post)

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            if task_id.startswith("voice-map"):
                text = json.dumps({
                    "behavior_notes_candidates": ["short replies"],
                    "exemplar_candidates": [{"turns": [{"role": "ta", "text": "嗯"}], "axis": ["shape"]}],
                })
            elif task_id.startswith("fact-map"):
                text = json.dumps({"fact_candidates": [{"about": "user", "summary": "User likes tea", "evidence": "tea"}]})
            elif task_id.startswith("voice-reduce"):
                text = json.dumps({
                    "behavior_notes": ["short replies"],
                    "exemplars": [{"turns": [{"role": "ta", "text": "嗯"}], "founding": True}],
                })
            elif task_id.startswith("fact-write"):
                text = json.dumps({
                    "memories": [{"type": "fact", "summary": "User likes tea", "content": "User likes tea."}],
                    "identity": {"agent_name": "", "dimensions": [{"name": "Brief", "value": 70, "description": "Short replies"}]},
                    "days_with_user": 3,
                    "relationship_anchor_evidence": "import",
                })
            elif task_id == "persona-build":
                text = "## 你是谁\n\n你是 TA。\n\n## 你怎么说话\n\n短句。"
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    def mint(user_id, scopes):
        minted.append({"user_id": user_id, "scopes": scopes})
        return "rtok"

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
        max_jobs=1,
        now=lambda: 10.0,
    )

    assert result["processed"] == 1
    assert minted == [{"user_id": "usr_1", "scopes": ["envelope_decrypt", "genesis"]}]
    voice_inputs = [call["messages"][1]["content"] for call in llm_calls if call["task_id"].startswith("voice-map")]
    assert voice_inputs == ["text for chunk-0", "text for chunk-1"]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["persona"]["content"].startswith("## 你是谁")
    assert reducer_output["memories"][0]["summary"] == "User likes tea"
    assert "raw_text" not in reducer_output
    assert "chunks" not in reducer_output


def test_complete_json_repairs_invalid_model_json_once():
    calls = []

    class FakeLLM:
        def complete(self, **kwargs):
            calls.append(kwargs)
            if kwargs["task_id"] == "voice-map-0":
                text = '{"behavior_notes_candidates":["keeps "quoted" words"],"exemplar_candidates":[]}'
            elif kwargs["task_id"] == "voice-map-0-json-repair":
                payload = json.loads(kwargs["messages"][1]["content"])
                assert payload["task_id"] == "voice-map-0"
                assert "malformed_json" in payload
                assert kwargs["idempotency_key"] == "job_1:voice_map:0:json_repair"
                assert kwargs["temperature"] == 0.0
                text = json.dumps({
                    "behavior_notes_candidates": ['keeps "quoted" words'],
                    "exemplar_candidates": [],
                })
            else:
                raise AssertionError(kwargs["task_id"])
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=kwargs["task_id"])

    parsed = worker._complete_json(
        FakeLLM(),
        user_id="usr_1",
        job_id="job_1",
        task_id="voice-map-0",
        runtime=types.SimpleNamespace(),
        messages=[{"role": "user", "content": "x"}],
        max_tokens=1000,
        idempotency_key="job_1:voice_map:0",
    )

    assert parsed["behavior_notes_candidates"] == ['keeps "quoted" words']
    assert [call["task_id"] for call in calls] == ["voice-map-0", "voice-map-0-json-repair"]


def test_ai_persona_source_uses_persona_material_without_voice_or_fact_map(monkeypatch):
    llm_calls = []
    apply_payloads, _minted, mint = _install_success_harness(
        monkeypatch,
        source_kind="ai_persona",
        chunk_texts=["你叫 Mira,保持稳定直接的语气。"],
    )

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            payload = json.loads(kwargs["messages"][1]["content"])
            if task_id.startswith("fact-write"):
                assert payload["fact_digest"] == []
                assert "你叫 Mira" in payload["persona_material"]
                assert payload["memory_summary"] == ""
                text = json.dumps({
                    "memories": [{"summary": "should be dropped"}],
                    "identity": {"agent_name": "Mira", "dimensions": [{"name": "Direct", "value": 70, "description": "Persona says direct."}]},
                    "days_with_user": 5,
                    "relationship_anchor_evidence": "persona",
                })
            elif task_id == "persona-build":
                assert "你叫 Mira" in payload["persona_material"]
                assert payload["behavior_notes"] == []
                assert payload["founding_exemplars"] == []
                text = "## 你是谁\n\n你叫 Mira。\n\n## 你怎么说话\n\n稳定直接。"
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
    )

    assert result["processed"] == 1
    assert [call["task_id"] for call in llm_calls] == ["fact-write-0", "persona-build"]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["source_family"] == "ai_persona"
    assert reducer_output["memories"] == []
    assert reducer_output["identity"]["agent_name"] == "Mira"
    assert reducer_output["persona"]["source_family"] == "ai_persona"
    assert reducer_output["voice"]["exemplar_count"] == 0
    assert "chunk 1" not in json.dumps(reducer_output, ensure_ascii=False)


def test_ai_persona_source_merges_existing_history_voice_workset(monkeypatch):
    llm_calls = []
    apply_payloads, _minted, mint = _install_success_harness(
        monkeypatch,
        source_kind="ai_persona",
        chunk_texts=["你叫 Mira,保持稳定直接的语气。"],
        blobs={
            worker.service.GENESIS_VOICE_BLOB: {"content_envelope": {"id": "voice-env"}},
            "voice_plaintext": {
                "behavior_notes": ["短句接住情绪"],
                "exemplars": [{
                    "turns": [{"role": "ta", "text": "别急,我在。"}],
                    "founding": True,
                    "axis": ["emotion"],
                    "why": "grounded",
                }],
            },
        },
    )

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            payload = json.loads(kwargs["messages"][1]["content"])
            if task_id.startswith("fact-write"):
                text = json.dumps({"identity": {"agent_name": "Mira", "dimensions": []}, "memories": []})
            elif task_id == "persona-build":
                assert payload["persona_material"].startswith("--- chunk 1 ---")
                assert payload["behavior_notes"] == ["短句接住情绪"]
                assert payload["founding_exemplars"][0]["turns"][0]["text"] == "别急,我在。"
                text = "## 你是谁\n\n你叫 Mira。\n\n## 你怎么说话\n\n别急,我在。"
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
    )

    assert result["processed"] == 1
    assert [call["task_id"] for call in llm_calls] == ["fact-write-0", "persona-build"]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["persona"]["source_family"] == "merged"
    assert reducer_output["voice"]["behavior_notes_count"] == 1
    assert reducer_output["voice"]["founding_exemplar_count"] == 1


def test_history_source_merges_existing_ai_persona_with_voice_exemplars(monkeypatch):
    llm_calls = []
    apply_payloads, _minted, mint = _install_success_harness(
        monkeypatch,
        source_kind="chat_export",
        chunk_texts=["user: 你会留下吗\nta: 别急,我在。"],
        blobs={
            worker.service.GENESIS_PERSONA_BLOB: {
                "source_priority": 100,
                "source_family": "ai_persona",
                "content_envelope": {"id": "persona-env"},
            },
            "persona_plaintext": "## 你是谁\n\n你叫 Mira。",
        },
    )

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            if task_id.startswith("voice-map"):
                text = json.dumps({
                    "behavior_notes_candidates": ["短句接住情绪"],
                    "exemplar_candidates": [{"turns": [{"role": "ta", "text": "别急,我在。"}], "axis": ["emotion"]}],
                })
            elif task_id.startswith("fact-map"):
                text = json.dumps({"fact_candidates": []})
            elif task_id.startswith("voice-reduce"):
                text = json.dumps({
                    "behavior_notes": ["短句接住情绪"],
                    "exemplars": [{"turns": [{"role": "ta", "text": "别急,我在。"}], "founding": True}],
                })
            elif task_id.startswith("fact-write"):
                text = json.dumps({"memories": [], "identity": {"agent_name": "", "dimensions": []}})
            elif task_id == "persona-build":
                payload = json.loads(kwargs["messages"][1]["content"])
                assert payload["persona_material"].startswith("## 你是谁")
                assert payload["behavior_notes"] == ["短句接住情绪"]
                assert payload["founding_exemplars"][0]["turns"][0]["text"] == "别急,我在。"
                text = "## 你是谁\n\n你叫 Mira。\n\n## 你怎么说话\n\n别急,我在。"
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
    )

    assert result["processed"] == 1
    assert [call["task_id"] for call in llm_calls] == [
        "voice-map-0", "fact-map-0", "voice-reduce-0", "persona-build",
    ]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["persona"]["source_family"] == "merged"
    assert reducer_output["voice_workset"]["behavior_notes"] == ["短句接住情绪"]
    assert reducer_output["voice_workset"]["exemplars"][0]["founding"] is True


def test_user_profile_source_writes_memory_facts_without_identity_or_persona(monkeypatch):
    llm_calls = []
    apply_payloads, _minted, mint = _install_success_harness(
        monkeypatch,
        source_kind="user_profile",
        chunk_texts=["用户叫 Seven,喜欢直接反馈。"],
    )

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            if task_id.startswith("fact-map"):
                assert "source_kind=user_profile" in kwargs["messages"][1]["content"]
                text = json.dumps({"fact_candidates": [{"about": "user", "summary": "User likes direct feedback", "evidence": "direct"}]})
            elif task_id.startswith("fact-write"):
                payload = json.loads(kwargs["messages"][1]["content"])
                assert payload["persona_material"] == ""
                assert payload["memory_summary"] == ""
                text = json.dumps({
                    "memories": [{"type": "fact", "summary": "User likes direct feedback", "content": "User likes direct feedback."}],
                    "identity": {"agent_name": "Seven", "dimensions": [{"name": "Wrong", "value": 90, "description": "from user profile"}]},
                    "days_with_user": 9,
                    "relationship_anchor_evidence": "user profile",
                })
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
    )

    assert result["processed"] == 1
    assert [call["task_id"] for call in llm_calls] == ["fact-map-0", "fact-write-0"]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["source_family"] == "user_profile"
    assert reducer_output["memories"][0]["summary"] == "User likes direct feedback"
    assert "identity" not in reducer_output
    assert "persona" not in reducer_output


def test_memory_summary_source_feeds_fact_write_material_without_maps(monkeypatch):
    llm_calls = []
    apply_payloads, _minted, mint = _install_success_harness(
        monkeypatch,
        source_kind="memory_summary",
        chunk_texts=["用户在五月反复提到需要稳定陪伴。"],
    )

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            if task_id.startswith("fact-write"):
                payload = json.loads(kwargs["messages"][1]["content"])
                assert payload["fact_digest"] == []
                assert payload["persona_material"] == ""
                assert "稳定陪伴" in payload["memory_summary"]
                text = json.dumps({
                    "memories": [{"type": "fact", "summary": "User needs stable companionship", "content": "User needs stable companionship."}],
                    "identity": {"agent_name": "Wrong", "dimensions": [{"name": "Wrong", "value": 90, "description": "from memory summary"}]},
                })
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
    )

    assert result["processed"] == 1
    assert [call["task_id"] for call in llm_calls] == ["fact-write-0"]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["source_family"] == "memory_summary"
    assert reducer_output["memories"][0]["summary"] == "User needs stable companionship"
    assert "identity" not in reducer_output
    assert "persona" not in reducer_output


def test_tick_marks_failed_when_claimed_job_has_missing_chunks(monkeypatch):
    failures = []
    monkeypatch.setattr(worker, "get_store", lambda user_id: types.SimpleNamespace(user_id=user_id))
    monkeypatch.setattr(worker.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.service, "mark_failed", lambda _store, job_id, error: failures.append((job_id, error)))
    monkeypatch.setattr(
        worker.db,
        "genesis_claim_uploaded_jobs",
        lambda limit=1: [{"user_id": "usr_1", "job_id": "job_1", "status": "processing", "total_chunks": 2}],
    )
    monkeypatch.setattr(worker.db, "genesis_missing_chunk_seqs", lambda *_args: [1])
    monkeypatch.setattr(worker.db, "genesis_list_chunks", lambda *_args: (_ for _ in ()).throw(AssertionError("no chunks")))

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=lambda *_args, **_kwargs: "rtok",
    )

    assert result["failed"] == 1
    assert failures[0][0] == "job_1"
    assert "missing_chunks:1" in failures[0][1]


def test_tick_marks_failed_for_empty_import_without_provider_calls(monkeypatch):
    failures = []
    monkeypatch.setattr(worker, "get_store", lambda user_id: types.SimpleNamespace(user_id=user_id))
    monkeypatch.setattr(worker.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.service, "mark_failed", lambda _store, job_id, error: failures.append((job_id, error)))
    monkeypatch.setattr(
        worker.db,
        "genesis_claim_uploaded_jobs",
        lambda limit=1: [{"user_id": "usr_1", "job_id": "job_1", "status": "processing", "total_chunks": 0}],
    )
    monkeypatch.setattr(worker.db, "genesis_missing_chunk_seqs", lambda *_args: (_ for _ in ()).throw(AssertionError("no missing check")))
    monkeypatch.setattr(worker.db, "genesis_list_chunks", lambda *_args: (_ for _ in ()).throw(AssertionError("no chunks")))
    monkeypatch.setattr(worker.httpx, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no provider")))

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=lambda *_args, **_kwargs: "rtok",
    )

    assert result["failed"] == 1
    assert failures[0][0] == "job_1"
    assert "empty_import" in failures[0][1]
