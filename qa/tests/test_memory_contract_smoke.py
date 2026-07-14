from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest
from jsonschema import ValidationError

from qa import memory_contract_smoke as smoke
from tools.provider_smoke import crypto
from tools.provider_smoke.client import Session


class QueueClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _req(self, method, path, *, api_key=None, body=None, **_kwargs):
        self.calls.append((method, path, api_key, body))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {path}")
        response = self.responses.pop(0)
        if callable(response):
            return response(method, path, api_key, body)
        return response


def _session(*, api_key: str = "qa-private-api-key") -> Session:
    sk, pk = crypto.generate_keypair()
    return Session(user_id="usr_memory_contract", api_key=api_key, sk=sk, pk=pk)


def _context(responses=()) -> smoke._Context:
    return smoke._Context(client=QueueClient(responses), session=_session())


def _patch_passing_runners(monkeypatch):
    observations = {
        "_check_fresh_empty": {"index_count": 0, "fetch_count": 0, "missing_count": 1},
        "_check_encrypted_v1": {
            "stored_record_count": 1,
            "index_count": 1,
            "fetch_count": 1,
            "encrypted_at_rest": True,
            "round_trip_verified": True,
        },
        "_check_quiet_window_capture_write": {
            "before_card_count": 1,
            "after_card_count": 2,
            "capture_job_count": 1,
            "cards_added": 1,
            "cards_superseded": 0,
            "quiet_window_enqueued": True,
            "capture_noop": False,
            "fetch_count": 1,
            "round_trip_verified": True,
        },
        "_check_route_chat_message_trace": {
            "route_event_count": 1,
            "route_event_correlated": True,
        },
        "_check_capture_noop_disposable_chitchat": {
            "before_card_count": 2,
            "after_card_count": 2,
            "capture_job_count": 1,
            "cards_added": 0,
            "cards_superseded": 0,
            "quiet_window_enqueued": True,
            "capture_noop": True,
            "bucket_vocab_unchanged": True,
            "thread_vocab_unchanged": True,
        },
        "_check_duplicate_fact_no_growth": {
            "before_card_count": 2,
            "after_card_count": 2,
            "capture_job_count": 1,
            "cards_added": 0,
            "cards_superseded": 0,
            "quiet_window_enqueued": True,
            "capture_noop": True,
            "fetch_count": 1,
            "bucket_vocab_unchanged": True,
            "thread_vocab_unchanged": True,
            "existing_card_preserved": True,
        },
        "_check_local_only": {
            "index_count": 0,
            "fetch_count": 0,
            "unavailable_count": 1,
            "local_only_excluded": True,
        },
        "_check_supersede": {
            "default_visible_count": 1,
            "explicit_visible_count": 1,
            "unavailable_count": 1,
            "superseded_hidden_by_default": True,
            "replacement_visible": True,
        },
        "_check_legacy_migration": {
            "fetch_count": 1,
            "stable_id_preserved": True,
            "legacy_shape_removed": True,
            "round_trip_verified": True,
        },
        "_check_stale_cas": {
            "fetch_count": 2,
            "stable_id_preserved": True,
            "stale_write_rejected": True,
            "winning_update_preserved": True,
            "concurrent_card_preserved": True,
        },
    }
    for name, evidence in observations.items():
        monkeypatch.setattr(smoke, name, lambda _context, value=evidence: dict(value))


def test_envelope_builder_produces_real_shared_and_local_only_v1_crypto():
    context = _context()
    _, enclave_pk = crypto.generate_keypair()
    context.enclave_pk = enclave_pk
    inner = {
        "summary": "synthetic",
        "content": "memory contract",
        "bucket": "QA",
        "threads": [],
    }

    shared = smoke._envelope(
        context,
        inner,
        visibility="shared",
        source="qa_shared",
    )
    local = smoke._envelope(
        context,
        inner,
        visibility="local_only",
        source="qa_local",
    )

    assert json.loads(crypto.decrypt_reply(shared, context.session.sk, context.session.pk)) == inner
    assert json.loads(crypto.decrypt_reply(local, context.session.sk, context.session.pk)) == inner
    assert shared["visibility"] == "shared" and shared["K_enclave"]
    assert local["visibility"] == "local_only" and "K_enclave" not in local
    assert shared["owner_user_id"] == context.session.user_id
    assert shared["type"] == "fact"


def test_fresh_empty_recall_proves_empty_index_and_explicit_missing_fetch():
    context = _context(
        [
            (200, {"items": [], "user_card_count": 0, "truncated": False}),
            lambda _m, _p, _k, body: (
                200,
                {
                    "items": [],
                    "missing_ids": list(body["ids"]),
                    "unavailable_ids": [],
                },
            ),
        ]
    )

    evidence = smoke._check_fresh_empty(context)

    assert evidence == {"index_count": 0, "fetch_count": 0, "missing_count": 1}
    assert context.client.responses == []


def test_fresh_empty_recall_rejects_reused_account():
    context = _context(
        [
            (200, {"items": [{"id": "existing"}], "user_card_count": 1}),
            lambda _m, _p, _k, body: (
                200,
                {"items": [], "missing_ids": list(body["ids"]), "unavailable_ids": []},
            ),
        ]
    )

    with pytest.raises(smoke.MemoryContractError, match="FRESH_ACCOUNT_NOT_EMPTY"):
        smoke._check_fresh_empty(context)


def test_enclave_material_binds_api_key_account_to_manifest_keypair():
    context = _context(
        [
            (
                200,
                {
                    "user_id": "usr_memory_contract",
                    "public_key": base64.b64encode(b"x" * 32).decode("ascii"),
                    "enclave_content_public_key_hex": "ab" * 32,
                },
            )
        ]
    )

    with pytest.raises(smoke.MemoryContractError, match="KEY_MATERIAL_UNAVAILABLE"):
        smoke._load_enclave_material(context)


def test_encrypted_v1_check_proves_ciphertext_storage_and_plaintext_readside():
    session = _session()
    _, enclave_pk = crypto.generate_keypair()
    captured = {}

    def add(_method, _path, _api_key, body):
        captured["envelope"] = dict(body["envelope"])
        return 201, {"status": "created", "moment": {"id": body["envelope"]["id"]}}

    def raw(_method, _path, _api_key, _body):
        return 200, {"moment": dict(captured["envelope"])}

    def index(_method, _path, _api_key, _body):
        return 200, {
            "items": [
                {"id": captured["envelope"]["id"], "summary": smoke._SHARED_SUMMARY}
            ]
        }

    def fetch(_method, _path, _api_key, _body):
        return 200, {
            "items": [
                {
                    "id": captured["envelope"]["id"],
                    "summary": smoke._SHARED_SUMMARY,
                    "content": smoke._SHARED_CONTENT,
                }
            ],
            "missing_ids": [],
            "unavailable_ids": [],
        }

    client = QueueClient(
        [
            (
                200,
                {
                    "user_id": session.user_id,
                    "public_key": base64.b64encode(session.pk).decode("ascii"),
                    "enclave_content_public_key_hex": enclave_pk.hex(),
                },
            ),
            add,
            raw,
            index,
            fetch,
        ]
    )
    context = smoke._Context(client=client, session=session)

    evidence = smoke._check_encrypted_v1(context)

    assert evidence["encrypted_at_rest"] is True
    assert evidence["round_trip_verified"] is True
    assert context.shared_id == captured["envelope"]["id"]
    assert smoke._SHARED_CONTENT not in json.dumps(captured["envelope"])


def test_native_capture_executor_runs_real_resident_job_path_without_provider_call():
    context = _context()
    _, context.enclave_pk = crypto.generate_keypair()
    message = smoke.build_envelope(
        plaintext=smoke._CAPTURE_USER_TEXT.encode("utf-8"),
        owner_user_id=context.session.user_id,
        user_pk_bytes=context.session.pk,
        enclave_pk_bytes=context.enclave_pk,
        visibility="shared",
        item_id="msg_capture_contract",
    )
    message.update(
        {
            "role": "user",
            "source": "chat",
            "content_type": "text",
            "ts": 100.0,
        }
    )
    job = {
        "job_id": "cap_contract",
        "job_kind": "memory_capture",
        "source": "memory_capture",
        "status": "pending",
        "trigger": "quiet_timeout",
        "ts": 101.0,
        "window": {
            "after_message_id": "",
            "until_message_id": "msg_capture_contract",
            "until_ts": 100.0,
            "message_count": 1,
        },
    }
    terminal = {
        **job,
        "status": "completed",
        "capture_result": {
            "status": "ok",
            "cards": 1,
            "job_kind": "memory_capture",
        },
        "cards_added": 1,
        "cards_superseded": 0,
    }
    context.client = QueueClient(
        [
            (200, {"claimed": True, "job": {**job, "status": "claimed"}}),
            (200, {"job": {**job, "status": "realizing"}}),
            (200, {"messages": [message]}),
            (200, {"status": "ok", "results": [{}], "effects": []}),
            (200, {"job": terminal}),
        ]
    )
    reply = json.dumps(
        {
            "cards": [
                {
                    "action": "add",
                    "type": "fact",
                    "target_id": None,
                    "bucket": smoke._CAPTURE_BUCKET,
                    "threads": smoke._CAPTURE_THREADS,
                    "summary": smoke._CAPTURE_SUMMARY,
                    "content": smoke._CAPTURE_CONTENT,
                    "importance": 0.8,
                    "pulse": 0.3,
                }
            ]
        }
    )

    result = smoke._native_capture_executor(context, job, reply)

    assert result["status"] == "completed"
    assert result["capture_result"]["status"] == "ok"
    assert [call[1].split("?", 1)[0] for call in context.client.calls] == [
        "/v1/proactive/jobs/cap_contract/claim",
        "/v1/proactive/jobs/cap_contract/status",
        "/v1/chat/history",
        "/v1/memory/actions",
        "/v1/proactive/jobs/cap_contract/status",
    ]
    assert context.client.responses == []


def test_run_capture_job_requires_exact_quiet_job_and_terminal_receipt():
    job = {
        "job_id": "cap_contract",
        "job_kind": "memory_capture",
        "status": "pending",
        "trigger": "quiet_timeout",
    }
    context = _context(
        [
            (200, {"enqueued": True, "reason": "enqueued", "job": job}),
            (200, {"jobs": [job], "timed_out": False}),
        ]
    )
    context.capture_executor = lambda *_args: {
        **job,
        "status": "completed",
        "capture_result": {"status": "noop", "reason": "nothing_worth_keeping"},
        "cards_added": 0,
        "cards_superseded": 0,
        "noop_reason": "nothing_worth_keeping",
    }

    evidence = smoke._run_capture_job(
        context,
        agent_reply='{"cards":[]}',
        now_offset_sec=5_000,
        expected_capture_status="noop",
        expected_cards_added=0,
        expected_cards_superseded=0,
        failure_code="CAPTURE_NOOP_FAILED",
    )

    assert evidence == {
        "capture_job_count": 1,
        "cards_added": 0,
        "cards_superseded": 0,
        "quiet_window_enqueued": True,
        "capture_noop": True,
    }


def test_quiet_capture_write_requires_card_delta_and_round_trip(monkeypatch):
    context = _context()
    before = [{"id": "seed", "summary": smoke._SHARED_SUMMARY}]
    after = [*before, {"id": "captured", "summary": smoke._CAPTURE_SUMMARY}]
    snapshots = iter((before, after))
    monkeypatch.setattr(smoke, "_memory_index", lambda *_args, **_kwargs: next(snapshots))
    monkeypatch.setattr(smoke, "_post_chat_message", lambda *_args, **_kwargs: "msg")
    monkeypatch.setattr(
        smoke,
        "_run_capture_job",
        lambda *_args, **_kwargs: {
            "capture_job_count": 1,
            "cards_added": 1,
            "cards_superseded": 0,
            "quiet_window_enqueued": True,
            "capture_noop": False,
        },
    )
    responses = iter(
        (
            {"enabled": True, "deploy_enabled": True},
            {"status": "ok"},
            {
                "items": [
                    {
                        "id": "captured",
                        "summary": smoke._CAPTURE_SUMMARY,
                        "content": smoke._CAPTURE_CONTENT,
                    }
                ],
                "missing_ids": [],
                "unavailable_ids": [],
            },
        )
    )
    monkeypatch.setattr(smoke, "_request", lambda *_args, **_kwargs: next(responses))

    evidence = smoke._check_quiet_window_capture_write(context)

    assert context.captured_id == "captured"
    assert context.trace_message_id == "msg"
    assert evidence["before_card_count"] == 1
    assert evidence["after_card_count"] == 2
    assert evidence["round_trip_verified"] is True


def test_route_trace_requires_exact_message_correlation(monkeypatch):
    context = _context()
    context.trace_message_id = "msg-correlated"
    monkeypatch.setattr(
        smoke,
        "_request",
        lambda *_args, **_kwargs: {
            "enabled": True,
            "deploy_enabled": True,
            "events": [
                {
                    "type": "chat.message",
                    "trace_id": "msg-correlated",
                    "turn_id": "msg-correlated",
                    "detail": {"msg_id": "msg-correlated"},
                }
            ],
        },
    )

    assert smoke._check_route_chat_message_trace(context) == {
        "route_event_count": 1,
        "route_event_correlated": True,
    }


def test_chitchat_noop_rejects_vocabulary_growth(monkeypatch):
    context = _context()
    monkeypatch.setattr(
        smoke,
        "_memory_index",
        lambda *_args, **_kwargs: [{"id": "captured"}],
    )
    vocabularies = iter(
        (
            (("QA",), ("Lyra",)),
            (("QA", "new"), ("Lyra",)),
        )
    )
    monkeypatch.setattr(
        smoke, "_memory_vocabulary", lambda *_args, **_kwargs: next(vocabularies)
    )
    monkeypatch.setattr(smoke, "_post_chat_message", lambda *_args, **_kwargs: "msg")
    monkeypatch.setattr(smoke, "_run_capture_job", lambda *_args, **_kwargs: {})

    with pytest.raises(smoke.MemoryContractError, match="CAPTURE_NOOP_FAILED"):
        smoke._check_capture_noop_disposable_chitchat(context)


def test_duplicate_fact_noop_preserves_existing_card_and_vocabulary(monkeypatch):
    context = _context()
    context.captured_id = "captured"
    monkeypatch.setattr(
        smoke,
        "_memory_index",
        lambda *_args, **_kwargs: [
            {"id": "captured", "summary": smoke._CAPTURE_SUMMARY}
        ],
    )
    monkeypatch.setattr(
        smoke,
        "_memory_vocabulary",
        lambda *_args, **_kwargs: (("QA",), ("Lyra",)),
    )
    monkeypatch.setattr(smoke, "_post_chat_message", lambda *_args, **_kwargs: "msg")
    monkeypatch.setattr(
        smoke,
        "_run_capture_job",
        lambda *_args, **_kwargs: {
            "capture_job_count": 1,
            "cards_added": 0,
            "cards_superseded": 0,
            "quiet_window_enqueued": True,
            "capture_noop": True,
        },
    )
    monkeypatch.setattr(
        smoke,
        "_request",
        lambda *_args, **_kwargs: {
            "items": [
                {
                    "id": "captured",
                    "summary": smoke._CAPTURE_SUMMARY,
                    "content": smoke._CAPTURE_CONTENT,
                }
            ],
            "missing_ids": [],
            "unavailable_ids": [],
        },
    )

    evidence = smoke._check_duplicate_fact_no_growth(context)

    assert evidence["existing_card_preserved"] is True
    assert evidence["bucket_vocab_unchanged"] is True
    assert evidence["thread_vocab_unchanged"] is True


def test_local_only_check_requires_index_exclusion_and_fetch_unavailable(monkeypatch):
    local_id = "mom_local"
    local_envelope = {
        "id": local_id,
        "visibility": "local_only",
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "wrapped",
    }
    monkeypatch.setattr(smoke, "_envelope", lambda *_args, **_kwargs: local_envelope)
    context = _context(
        [
            (201, {"status": "created", "moment": {"id": local_id}}),
            (200, {"moment": dict(local_envelope)}),
            (200, {"items": [{"id": "shared"}]}),
            (200, {"items": [], "missing_ids": [], "unavailable_ids": [local_id]}),
        ]
    )

    evidence = smoke._check_local_only(context)

    assert evidence["local_only_excluded"] is True
    assert evidence["unavailable_count"] == 1


def test_local_only_missing_from_fetch_is_not_misreported_as_unavailable(monkeypatch):
    local_id = "mom_local"
    local_envelope = {
        "id": local_id,
        "visibility": "local_only",
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "wrapped",
    }
    monkeypatch.setattr(smoke, "_envelope", lambda *_args, **_kwargs: local_envelope)
    context = _context(
        [
            (201, {"status": "created", "moment": {"id": local_id}}),
            (200, {"moment": dict(local_envelope)}),
            (200, {"items": []}),
            (200, {"items": [], "missing_ids": [local_id], "unavailable_ids": []}),
        ]
    )

    with pytest.raises(smoke.MemoryContractError, match="LOCAL_ONLY_FAILED"):
        smoke._check_local_only(context)


def test_success_receipt_is_schema_valid_and_contains_no_session_material(monkeypatch):
    _patch_passing_runners(monkeypatch)
    session = _session(api_key="qa-secret-never-persist")

    receipt = smoke.execute_memory_contract(QueueClient([]), session)

    smoke.validate_receipt(receipt)
    encoded = json.dumps(receipt, sort_keys=True)
    assert receipt["status"] == "PASS"
    assert receipt["summary"] == {
        "pass": 10,
        "fail": 0,
        "not_exercised": 0,
        "not_run": 0,
    }
    assert "qa-secret-never-persist" not in encoded
    assert session.user_id not in encoded
    assert smoke._SHARED_CONTENT not in encoded
    assert "body_ct" not in encoded


def test_memory_contract_profile_id_is_locked(monkeypatch):
    _patch_passing_runners(monkeypatch)

    with pytest.raises(smoke.MemoryContractError, match="PROFILE_ID_INVALID"):
        smoke.execute_memory_contract(
            QueueClient([]), _session(), profile_id="provider-profile"
        )


def test_migration_disabled_is_unverified_and_never_exit_zero(monkeypatch):
    _patch_passing_runners(monkeypatch)

    def unavailable(_context):
        raise smoke._MigrationUnavailable

    monkeypatch.setattr(smoke, "_check_legacy_migration", unavailable)
    receipt = smoke.execute_memory_contract(QueueClient([]), _session())

    smoke.validate_receipt(receipt)
    assert receipt["status"] == "UNVERIFIED"
    assert receipt["failure_code"] == "MIGRATION_DISABLED"
    assert [check["status"] for check in receipt["checks"]] == [
        "PASS",
        "PASS",
        "PASS",
        "PASS",
        "PASS",
        "PASS",
        "PASS",
        "PASS",
        "NOT_EXERCISED",
        "NOT_EXERCISED",
    ]
    assert smoke.exit_code(receipt) == 2


def test_failure_stops_mutation_sequence_and_marks_remaining_not_run(monkeypatch):
    def fail(_context):
        raise smoke.MemoryContractError("FRESH_ACCOUNT_NOT_EMPTY")

    monkeypatch.setattr(smoke, "_check_fresh_empty", fail)
    receipt = smoke.execute_memory_contract(QueueClient([]), _session())

    smoke.validate_receipt(receipt)
    assert receipt["status"] == "FAIL"
    assert receipt["checks"][0]["status"] == "FAIL"
    assert all(check["status"] == "NOT_RUN" for check in receipt["checks"][1:])
    assert smoke.exit_code(receipt) == 1


def test_unexpected_check_error_is_sanitized(monkeypatch):
    def fail(_context):
        raise RuntimeError("response accidentally contained qa-private-api-key")

    monkeypatch.setattr(smoke, "_check_fresh_empty", fail)
    receipt = smoke.execute_memory_contract(QueueClient([]), _session())

    smoke.validate_receipt(receipt)
    assert receipt["status"] == "FAIL"
    assert receipt["failure_code"] == "INTERNAL_CHECK_ERROR"
    assert "qa-private-api-key" not in json.dumps(receipt)


def test_schema_rejects_receipt_that_claims_raw_responses_were_persisted(monkeypatch):
    _patch_passing_runners(monkeypatch)
    receipt = smoke.execute_memory_contract(QueueClient([]), _session())
    receipt["raw_responses_persisted"] = True

    with pytest.raises(ValidationError):
        smoke.validate_receipt(receipt)


def test_run_smoke_reads_injected_session_and_writes_private_receipt(monkeypatch, tmp_path: Path):
    _patch_passing_runners(monkeypatch)
    session = _session(api_key="manifest-secret")
    monkeypatch.setattr(
        smoke,
        "load_profile_session",
        lambda _path, expected: (expected, "https://test-api.feedling.app", session),
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "memory-receipt.json"

    receipt = smoke.run_smoke(
        tmp_path / "manifest.json",
        output,
        client=QueueClient([]),
    )

    assert receipt["status"] == "PASS"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    raw = output.read_text(encoding="utf-8")
    assert "manifest-secret" not in raw
    assert session.user_id not in raw


def test_receipt_writer_rejects_world_readable_parent(monkeypatch, tmp_path: Path):
    _patch_passing_runners(monkeypatch)
    receipt = smoke.execute_memory_contract(QueueClient([]), _session())
    tmp_path.chmod(0o755)

    with pytest.raises(smoke.MemoryContractError, match="RECEIPT_PATH_UNSAFE"):
        smoke._write_receipt(tmp_path / "receipt.json", receipt)


def test_run_smoke_validates_receipt_path_before_loading_credentials(monkeypatch, tmp_path: Path):
    tmp_path.chmod(0o755)
    loaded = False

    def load(*_args):
        nonlocal loaded
        loaded = True
        raise AssertionError("must not load credentials")

    monkeypatch.setattr(smoke, "load_profile_session", load)

    with pytest.raises(smoke.MemoryContractError, match="RECEIPT_PATH_UNSAFE"):
        smoke.run_smoke(tmp_path / "manifest.json", tmp_path / "receipt.json")
    assert loaded is False
