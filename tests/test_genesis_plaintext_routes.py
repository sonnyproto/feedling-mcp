from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from flask import Flask

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import routes, service  # noqa: E402


def _store(user_id: str = "usr_plaintext"):
    return types.SimpleNamespace(user_id=user_id)


def _client(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(routes.bp)
    monkeypatch.setattr(routes.auth, "require_user", lambda: _store())
    monkeypatch.setattr(routes.auth, "_extract_api_key", lambda: "user_api_key")
    return app.test_client()


def test_plaintext_import_returns_genesis_job_and_does_not_persist_raw(monkeypatch):
    client = _client(monkeypatch)
    payload = {
        "format": "plaintext",
        "content": "User: hello\nAssistant: hi",
        "ai_persona_content": "secret persona text",
        "client_job_id": "ios_job_1",
    }
    captured: dict = {}

    monkeypatch.setattr(routes.db, "genesis_list_jobs", lambda *_args, **_kwargs: [])

    def fake_create(_store, create_payload):
        captured["create_payload"] = create_payload
        job = {
            "job_id": "genesis_job_1",
            "status": "created",
            "source_kind": create_payload["source_kind"],
            "metadata": {
                **create_payload["metadata"],
                "privacy_copy": service.PRIVACY_COPY,
            },
            "privacy_mode": service.PRIVACY_MODE,
        }
        return job, 201

    monkeypatch.setattr(routes.service, "create_import_job", fake_create)
    monkeypatch.setattr(
        routes.db,
        "genesis_set_job_status",
        lambda _user_id, _job_id, **_kwargs: {
            "job_id": "genesis_job_1",
            "status": "processing",
            "metadata": captured["create_payload"]["metadata"],
            "source_kind": captured["create_payload"]["source_kind"],
            "privacy_mode": service.PRIVACY_MODE,
        },
    )
    monkeypatch.setattr(routes.service, "write_genesis_state", lambda *_args, **_kwargs: None)

    def fake_start(_store, _api_key, job, *, chunk_texts, source_kind):
        captured["started"] = {
            "job_id": job["job_id"],
            "chunk_texts": chunk_texts,
            "source_kind": source_kind,
        }
        return True

    monkeypatch.setattr(routes, "_start_plaintext_genesis_job", fake_start)

    resp = client.post("/v1/genesis/imports/plaintext", json=payload)

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["job"]["job_id"] == "genesis_job_1"
    assert body["status"] == "processing"
    assert body["privacy_copy"] == service.PRIVACY_COPY
    assert captured["started"]["job_id"] == "genesis_job_1"
    assert len(captured["started"]["chunk_texts"]) <= 8
    assert captured["create_payload"]["total_chunks"] == len(captured["started"]["chunk_texts"])
    metadata_blob = json.dumps(captured["create_payload"]["metadata"], ensure_ascii=False)
    assert "User: hello" not in metadata_blob
    assert "secret persona text" not in metadata_blob
    assert captured["create_payload"]["metadata"]["ingest"] == "plaintext"
    assert captured["create_payload"]["metadata"]["client_job_id"] == "ios_job_1"
    assert captured["create_payload"]["metadata"]["timeline_span_days"] == 0


def test_plaintext_import_reuses_done_job_without_restart(monkeypatch):
    client = _client(monkeypatch)
    payload = {"format": "plaintext", "content": "User: hello"}
    input_hash = routes.history_import._history_import_payload_hash(payload)
    existing = {
        "job_id": "genesis_done",
        "status": "done",
        "metadata": {"ingest": "plaintext", "input_hash": input_hash},
    }
    monkeypatch.setattr(routes.db, "genesis_list_jobs", lambda *_args, **_kwargs: [existing])
    monkeypatch.setattr(
        routes,
        "_start_plaintext_genesis_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not restart done job")),
    )

    resp = client.post("/v1/genesis/imports/plaintext", json=payload)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["job"]["job_id"] == "genesis_done"
    assert body["status"] == "done"


def test_prepare_plaintext_import_caps_windows(monkeypatch):
    monkeypatch.setattr(
        routes.history_import,
        "_parse_import_history_content",
        lambda *_args: [{"role": "user", "content": "x", "source": "history_import"}],
    )
    monkeypatch.setattr(routes.history_import, "_persona_support_messages", lambda _payload: [])
    monkeypatch.setattr(
        routes.history_import,
        "_history_import_profile",
        lambda *_args, **_kwargs: {"tier": "small", "total_windows": 2, "message_count": 1, "support_count": 0},
    )
    monkeypatch.setattr(
        routes.history_import,
        "_build_transcript_windows",
        lambda *_args, **_kwargs: [{"text": f"window {idx}"} for idx in range(5)],
    )

    prepared = routes._prepare_plaintext_import({"content": "x"})

    assert len(prepared["chunk_texts"]) == 2
    assert prepared["source_kind"] == routes.history_import._HISTORY_SOURCE


def test_prepare_plaintext_import_computes_timeline_span_days(monkeypatch):
    base_ts = 1_700_000_000
    messages = [
        {"role": "user", "content": "start", "source": "history_import", "ts": base_ts},
        {
            "role": "assistant",
            "content": "later",
            "source": "history_import",
            "ts": base_ts + 3 * 24 * 60 * 60 + 123,
        },
        {"role": "user", "content": "ignored", "source": "history_import", "ts": "not-a-timestamp"},
    ]
    monkeypatch.setattr(routes.history_import, "_parse_import_history_content", lambda *_args: messages)
    monkeypatch.setattr(routes.history_import, "_persona_support_messages", lambda _payload: [])
    monkeypatch.setattr(
        routes.history_import,
        "_history_import_profile",
        lambda *_args, **_kwargs: {"tier": "small", "total_windows": 1, "message_count": 3, "support_count": 0},
    )
    monkeypatch.setattr(
        routes.history_import,
        "_build_transcript_windows",
        lambda *_args, **_kwargs: [{"text": "window"}],
    )

    prepared = routes._prepare_plaintext_import({"content": "x"})
    metadata = routes._plaintext_job_metadata({}, prepared, client_job_id="", input_hash="input_hash")

    assert prepared["timeline_span_days"] == 3
    assert metadata["timeline_span_days"] == 3


def test_plaintext_background_runner_distills_and_applies(monkeypatch):
    store = _store()
    calls: dict = {}

    def fake_set_status(_user_id, _job_id, **kwargs):
        calls.setdefault("statuses", []).append(kwargs)
        return {
            "job_id": "genesis_job_1",
            "status": kwargs["status"],
            "source_kind": "history_import",
            "privacy_mode": service.PRIVACY_MODE,
        }

    monkeypatch.setattr(routes.db, "genesis_set_job_status", fake_set_status)
    monkeypatch.setattr(routes.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")

    def fake_build(**kwargs):
        calls["build"] = kwargs
        return {"memories": [], "identity": {}, "voice": {}, "persona": {}}

    monkeypatch.setattr(routes.worker, "build_reducer_output_from_texts", fake_build)
    monkeypatch.setattr(
        routes.service,
        "apply_reducer_output",
        lambda _store, _api_key, _job_id, output: calls.update({"applied": output}),
    )
    monkeypatch.setattr(
        routes.service,
        "mark_failed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not fail")),
    )

    routes._run_plaintext_genesis_job(
        store,
        "user_api_key",
        "genesis_job_1",
        chunk_texts=["window 1"],
        source_kind="history_import",
    )

    assert calls["build"]["user_id"] == "usr_plaintext"
    assert calls["build"]["chunk_texts"] == ["window 1"]
    assert calls["build"]["source_kind"] == "history_import"
    assert calls["applied"]["memories"] == []
    assert calls["statuses"][-1]["processed_chunks"] == 1
