from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import service  # noqa: E402


def _store(user_id: str = "usr_genesis"):
    return types.SimpleNamespace(user_id=user_id)


def _chunk_meta(user_id: str = "usr_genesis", *, body: bytes = b"ciphertext") -> dict:
    return {
        "v": 1,
        "id": "genesis_chunk_job_1_0",
        "body_ct": base64.b64encode(body).decode("ascii"),
        "nonce": "nonce_b64",
        "K_user": "ku_b64",
        "K_enclave": "ke_b64",
        "visibility": "shared",
        "owner_user_id": user_id,
        "enclave_pk_fpr": "fpr",
    }


def test_genesis_state_maps_active_job_to_processing_gate_status(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        service.db,
        "set_blob",
        lambda user_id, kind, doc: captured.update({"user_id": user_id, "kind": kind, "doc": doc}),
    )

    state = service.write_genesis_state(_store(), {"job_id": "job_1", "status": "uploading"})

    assert state["status"] == "processing"
    assert state["job_status"] == "uploading"
    assert captured["kind"] == service.GENESIS_STATE_BLOB
    assert captured["doc"]["status"] == "processing"


def test_genesis_state_preserves_uploaded_done_failed_gate_status(monkeypatch):
    states = []
    monkeypatch.setattr(service.db, "set_blob", lambda _user_id, _kind, doc: states.append(doc))

    service.write_genesis_state(_store(), {"job_id": "job_1", "status": "uploaded"}, status="uploaded")
    service.write_genesis_state(_store(), {"job_id": "job_1", "status": "done"})
    service.write_genesis_state(_store(), {"job_id": "job_1", "status": "failed", "error": "boom"})

    assert [state["status"] for state in states] == ["uploaded", "done", "failed"]
    assert states[-1]["error"] == "boom"


def test_create_import_job_writes_state_only_after_real_upload_start(monkeypatch):
    captured = {}

    def fake_create(user_id, job):
        assert user_id == "usr_genesis"
        assert job["metadata"]["privacy_copy"] == service.PRIVACY_COPY
        return {
            "job_id": job["job_id"],
            "status": "created",
            "privacy_mode": job["privacy_mode"],
            "memory_action_count": 0,
        }

    monkeypatch.setattr(service.db, "genesis_create_job", fake_create)
    monkeypatch.setattr(
        service.db,
        "set_blob",
        lambda user_id, kind, doc: captured.update({"user_id": user_id, "kind": kind, "doc": doc}),
    )

    job, status = service.create_import_job(
        _store(),
        {"job_id": "job_1", "source_kind": "chat_export", "total_chunks": 2},
    )

    assert status == 201
    assert job["job_id"] == "job_1"
    assert captured["kind"] == service.GENESIS_STATE_BLOB
    assert captured["doc"]["status"] == "processing"


def test_create_import_job_drops_plaintext_metadata(monkeypatch):
    saved = {}

    def fake_create(_user_id, job):
        saved.update(job)
        return {"job_id": job["job_id"], "status": "created", "privacy_mode": job["privacy_mode"]}

    monkeypatch.setattr(service.db, "genesis_create_job", fake_create)
    monkeypatch.setattr(service.db, "set_blob", lambda *_args: None)

    service.create_import_job(_store(), {
        "job_id": "job_1",
        "metadata": {
            "transcript": "raw chat should not persist",
            "ai_persona": "raw persona should not persist",
            "file_manifest_hash": "abc123",
            "file_count": 2,
        },
    })

    metadata = saved["metadata"]
    assert metadata["file_manifest_hash"] == "abc123"
    assert metadata["file_count"] == 2
    assert metadata["privacy_copy"] == service.PRIVACY_COPY
    assert "transcript" not in metadata
    assert "ai_persona" not in metadata


def test_create_import_job_is_idempotent_for_existing_job(monkeypatch):
    monkeypatch.setattr(service.db, "genesis_create_job", lambda *_args: None)
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading"},
    )
    monkeypatch.setattr(service.db, "set_blob", lambda *_args: None)

    job, status = service.create_import_job(_store(), {"job_id": "job_1"})

    assert status == 200
    assert job == {"job_id": "job_1", "status": "uploading"}


def test_finalize_upload_blocks_gate_when_chunks_missing(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading", "total_chunks": 3},
    )
    monkeypatch.setattr(service.db, "genesis_missing_chunk_seqs", lambda *_args: [1])
    monkeypatch.setattr(
        service.db,
        "set_blob",
        lambda _user_id, _kind, doc: captured.update(doc),
    )

    _job, missing = service.finalize_upload(_store(), "job_1")

    assert missing == [1]
    assert captured["status"] == "processing"
    assert captured["job_status"] == "uploading"


def test_finalize_upload_sets_uploaded_gate_status_when_complete(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading", "total_chunks": 2},
    )
    monkeypatch.setattr(service.db, "genesis_missing_chunk_seqs", lambda *_args: [])
    monkeypatch.setattr(
        service.db,
        "genesis_mark_finalized",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploaded", "total_chunks": 2},
    )
    monkeypatch.setattr(service.db, "set_blob", lambda _user_id, _kind, doc: captured.update(doc))

    job, missing = service.finalize_upload(_store(), "job_1")

    assert missing == []
    assert job["status"] == "uploaded"
    assert captured["status"] == "uploaded"
    assert captured["job_status"] == "uploaded"


def test_put_chunk_requires_v1_envelope_meta(monkeypatch):
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading", "total_chunks": 1},
    )

    try:
        service.put_chunk(
            _store(),
            "job_1",
            seq=0,
            encrypted_body=b"ciphertext",
            byte_start=0,
            byte_end=10,
        )
    except ValueError as e:
        assert str(e) == "chunk_envelope_required"
    else:
        raise AssertionError("expected missing envelope metadata to be rejected")


def test_put_chunk_stores_envelope_meta_without_body_ct(monkeypatch):
    captured = {}
    body = b"ciphertext"
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading", "total_chunks": 1},
    )

    def fake_put(_user_id, _job_id, **kwargs):
        captured.update(kwargs)
        return {"seq": kwargs["seq"], "aad": kwargs["aad"]}

    monkeypatch.setattr(service.db, "genesis_put_chunk", fake_put)
    monkeypatch.setattr(service.db, "set_blob", lambda *_args: None)

    chunk = service.put_chunk(
        _store(),
        "job_1",
        seq=0,
        encrypted_body=body,
        byte_start=0,
        byte_end=len(body),
        envelope_meta=_chunk_meta(body=body),
    )

    meta = captured["aad"]["envelope_meta"]
    assert chunk["seq"] == 0
    assert meta["owner_user_id"] == "usr_genesis"
    assert meta["K_enclave"] == "ke_b64"
    assert "body_ct" not in meta


def test_put_chunk_rejects_cross_user_envelope_meta(monkeypatch):
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading", "total_chunks": 1},
    )

    try:
        service.put_chunk(
            _store("usr_genesis"),
            "job_1",
            seq=0,
            encrypted_body=b"ciphertext",
            byte_start=0,
            byte_end=10,
            envelope_meta=_chunk_meta("usr_other"),
        )
    except ValueError as e:
        assert str(e) == "chunk_envelope_owner_mismatch"
    else:
        raise AssertionError("expected cross-user chunk envelope to be rejected")


def test_chunk_envelope_from_row_reconstructs_worker_decrypt_payload():
    body = b"ciphertext"
    meta = dict(_chunk_meta(body=body))
    meta.pop("body_ct")
    envelope = service.chunk_envelope_from_row({
        "encrypted_body": body,
        "aad": {"envelope_meta": meta},
    })

    assert envelope["body_ct"] == base64.b64encode(body).decode("ascii")
    assert envelope["owner_user_id"] == "usr_genesis"
    assert envelope["id"] == "genesis_chunk_job_1_0"


def test_apply_reducer_output_writes_persona_and_done_state(monkeypatch):
    blobs = []
    outputs = []

    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploaded", "total_chunks": 1},
    )
    monkeypatch.setattr(
        service.db,
        "genesis_set_job_status",
        lambda _user_id, _job_id, **_kwargs: {"job_id": "job_1", "status": "processing"},
    )
    monkeypatch.setattr(
        service.db,
        "set_blob",
        lambda _user_id, kind, doc: blobs.append({"kind": kind, "doc": doc}),
    )
    monkeypatch.setattr(service.db, "get_blob", lambda *_args: None)
    monkeypatch.setattr(
        service.db,
        "genesis_upsert_output",
        lambda _user_id, _job_id, output_type, **kwargs: outputs.append({"type": output_type, **kwargs}),
    )
    monkeypatch.setattr(
        service.db,
        "genesis_complete_job",
        lambda _user_id, _job_id, **kwargs: {
            "job_id": "job_1",
            "status": "done",
            **kwargs,
        },
    )
    monkeypatch.setattr(service, "apply_memory_outputs", lambda *_args: (2, [{"memory": {"id": "m1"}}]))
    monkeypatch.setattr(service, "init_identity_if_absent", lambda *_args: "initialized")
    monkeypatch.setattr(
        service.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _plaintext, item_id=None: ({
            "id": item_id,
            "body_ct": "encrypted_persona",
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
            "visibility": "shared",
            "owner_user_id": "usr_genesis",
        }, ""),
    )

    result = service.apply_reducer_output(
        _store(),
        "api_key",
        "job_1",
        {"persona": {"content": "You remember the user's voice.", "prompt_version": "7.B"}},
    )

    assert result["memory_action_count"] == 2
    assert result["identity_status"] == "initialized"
    persona_blob = next(blob for blob in blobs if blob["kind"] == service.GENESIS_PERSONA_BLOB)
    assert persona_blob["doc"]["encrypted"] is True
    assert persona_blob["doc"]["content_envelope"]["body_ct"] == "encrypted_persona"
    assert "content" not in persona_blob["doc"]
    state_blob = [blob for blob in blobs if blob["kind"] == service.GENESIS_STATE_BLOB][-1]
    assert state_blob["doc"]["status"] == "done"
    reducer_doc = next(output["doc"] for output in outputs if output["type"] == "reducer")
    reducer_json = json.dumps(reducer_doc, ensure_ascii=False)
    assert reducer_doc["plaintext_stored"] is False
    assert reducer_doc["persona_provided"] is True
    assert "You remember the user's voice." not in reducer_json
    assert any(output["type"] == "apply" for output in outputs)


def test_write_persona_artifact_keeps_existing_higher_priority_persona(monkeypatch):
    writes = []

    monkeypatch.setattr(
        service.db,
        "get_blob",
        lambda _user_id, kind: {"source_priority": 100, "sha256": "existing_sha"} if kind == service.GENESIS_PERSONA_BLOB else None,
    )
    monkeypatch.setattr(service.db, "set_blob", lambda *_args: writes.append(_args))
    monkeypatch.setattr(
        service.core_envelope,
        "_build_shared_envelope_for_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no overwrite")),
    )

    ref, digest = service.write_persona_artifact(
        _store(),
        "job_history",
        {
            "source_kind": "chat_export",
            "source_family": "history",
            "persona": {"content": "history-derived persona", "prompt_version": "7.B"},
        },
    )

    assert ref == service.GENESIS_PERSONA_REF
    assert digest == "existing_sha"
    assert writes == []


def test_apply_reducer_output_rejects_raw_transcript_fields(monkeypatch):
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploaded", "total_chunks": 1},
    )

    try:
        service.apply_reducer_output(_store(), "api_key", "job_1", {"raw_text": "do not send raw text"})
    except ValueError as e:
        assert str(e) == "raw_reducer_field_not_allowed:raw_text"
    else:
        raise AssertionError("expected raw reducer output to be rejected")


def test_identity_payload_from_output_leaves_intro_and_signature_for_respawn():
    payload = service._identity_payload_from_output(
        {
            "identity": {
                "agent_name": "Assistant",
                "self_introduction": "I should not be written by genesis.",
                "signature": ["not yet"],
                "dimensions": [
                    {"name": "Direct", "value": 82, "description": "TA often gives blunt feedback."},
                    {"name": "Warmth", "value": 60},
                ],
            }
        }
    )

    assert payload == {
        "agent_name": "",
        "self_introduction": "",
        "dimensions": [
            {"name": "Direct", "value": 82, "description": "TA often gives blunt feedback."}
        ],
    }


def test_identity_payload_from_output_ignores_empty_identity():
    assert service._identity_payload_from_output({"identity": {"agent_name": "", "dimensions": []}}) is None


def test_apply_memory_outputs_batches_memory_actions(monkeypatch):
    calls = []

    def fake_execute(_store, _api_key, actions):
        calls.append(actions)
        return {
            "status": "ok",
            "results": [{"memory": {"id": f"m{len(calls)}_{idx}"}} for idx, _action in enumerate(actions)],
        }, 200

    monkeypatch.setattr(service.memory_actions, "_execute_memory_actions", fake_execute)
    memories = [
        {
            "type": "fact",
            "summary": f"Fact {idx}",
            "content": f"Memory: Fact {idx}",
            "bucket": "Imported",
        }
        for idx in range(25)
    ]

    count, results = service.apply_memory_outputs(_store(), "api_key", {"memories": memories})

    assert count == 25
    assert len(results) == 25
    assert [len(call) for call in calls] == [20, 5]
