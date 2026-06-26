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
