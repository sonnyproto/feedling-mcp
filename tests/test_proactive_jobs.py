import importlib
import os
import sys
import tempfile
from pathlib import Path


_DATA_DIR = tempfile.mkdtemp(prefix="feedling-proactive-test-")
os.environ.setdefault("FEEDLING_DATA_DIR", _DATA_DIR)
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

appmod = importlib.import_module("app")


def test_device_event_payload_is_redacted():
    raw = {
        "permission": "authorized",
        "status": "changed",
        "lat": 40.7128,
        "lng": -74.0060,
        "title": "private calendar event",
        "safe_bucket": "home_area",
        "scene_tags": ["work", "reading"],
    }

    event = appmod._make_device_event("ios", "permission_changed", raw)

    assert event["event_id"].startswith("evt_")
    assert event["payload"]["permission"] == "authorized"
    assert event["payload"]["safe_bucket"] == "home_area"
    assert event["payload"]["scene_tags"] == ["work", "reading"]
    assert "lat" not in event["payload"]
    assert "lng" not in event["payload"]
    assert "title" not in event["payload"]


def test_manual_proactive_gate_creates_hidden_job(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    store = appmod.UserStore("usr_test_proactive")

    decision = appmod._build_proactive_gate_decision(
        store,
        {
            "force": True,
            "context_hint": "The user has been comparing a product for several minutes.",
            "connections": ["This matches a repeated research pattern."],
            "intent_label": "screen_research",
        },
    )
    store.append_gate_decision(decision)
    job = store.append_proactive_job(appmod._proactive_job_from_decision(decision))

    assert decision["should_reach_out"] is True
    assert decision["decision_id"].startswith("gd_")
    assert job["job_id"].startswith("pj_")
    assert job["source"] == appmod.PROACTIVE_JOB_SOURCE
    assert job["gate_decision_id"] == decision["decision_id"]
    assert job["context_hint"].startswith("The user has been comparing")

    jobs = store.list_proactive_jobs(since_epoch=0)
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == job["job_id"]


def test_proactive_debug_derives_job_delivery_state(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    store = appmod.UserStore("usr_test_proactive_delivery")

    decision = appmod._build_proactive_gate_decision(
        store,
        {
            "force": True,
            "context_hint": "The user is testing proactive delivery.",
            "intent_label": "manual_proactive_test",
            "frames": [{"id": "abcd1234abcd1234"}],
        },
    )
    job = store.append_proactive_job(appmod._proactive_job_from_decision(decision))
    envelope = {
        "id": "msg_proactive_1",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "owner_user_id": store.user_id,
    }
    store.append_chat(
        "openclaw",
        appmod.PROACTIVE_JOB_SOURCE,
        envelope,
        extra={
            "gate_decision_id": decision["decision_id"],
            "proactive_job_id": job["job_id"],
            "alert_preview": "自然地提醒用户。",
            "alert_status": "delivered",
            "live_activity_status": "delivered",
            "live_activity_activity_id": "la_1",
        },
    )

    snapshot = appmod._proactive_debug_snapshot(store)

    assert snapshot["jobs"][0]["derived_status"] == "delivered"
    assert snapshot["jobs"][0]["preview"] == "自然地提醒用户。"
    assert snapshot["jobs"][0]["alert_status"] == "delivered"
    assert snapshot["jobs"][0]["live_activity_status"] == "delivered"


def test_proactive_tick_endpoint_enqueues_pollable_job(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_proactive_key"
    user_id = "usr_endpoint_proactive"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id

    client = appmod.app.test_client()
    headers = {"X-API-Key": api_key}

    tick = client.post(
        "/v1/proactive/tick",
        headers=headers,
        json={
            "force": True,
            "context_hint": "The user has paused on a research screen.",
            "intent_label": "research_pause",
        },
    )
    assert tick.status_code == 200
    body = tick.get_json()
    assert body["enqueued"] is True
    assert body["job"]["source"] == appmod.PROACTIVE_JOB_SOURCE

    poll = client.get("/v1/proactive/jobs/poll?since=0&timeout=0", headers=headers)
    assert poll.status_code == 200
    jobs = poll.get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == body["job"]["job_id"]

    debug = client.get("/v1/proactive/debug", headers=headers)
    assert debug.status_code == 200
    snapshot = debug.get_json()
    assert snapshot["counts"]["decisions"] == 1
    assert snapshot["counts"]["jobs"] == 1

    page = client.get("/debug/proactive", headers=headers)
    assert page.status_code == 200
    assert b"Feedling Proactive Debug" in page.data
    assert body["job"]["job_id"].encode() in page.data
    assert b"frames sent" in page.data


def test_auto_proactive_gate_uses_decrypted_frame_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(appmod, "OPENROUTER_API_KEY", "sk-test")
    appmod._stores.clear()

    def _fake_decrypt(_store, frame_id, _api_key, include_image=False):
        return {
            "frame_id": frame_id,
            "app": "xhs",
            "ocr_text": "我在对比两个方案，要不要帮我把这三段压成一句可以发的观点？",
            "image_b64": "ZmFrZS1qcGVn" if include_image else "",
            "image_mime": "image/jpeg",
        }

    def _fake_llm_gate(**kwargs):
        assert kwargs["frame_contexts"][0]["image_b64"] == "ZmFrZS1qcGVn"
        assert "要不要帮我" in kwargs["ocr_summary"]
        return {
            "ok": True,
            "raw": {
                "should_reach_out": True,
                "confidence": 0.88,
                "intent_label": "help_compress_point",
                "context_hint": "The user is looking at a dense post and may want help turning it into one usable sentence.",
                "reason": "model_detected_helpful_moment",
                "frame_ids": ["abcd1234abcd1234"],
                "connections": ["The screen contains a rewrite/compression cue."],
            },
            "usage": {"total_tokens": 123},
        }

    monkeypatch.setattr(appmod, "_decrypt_frame_metadata_for_gate", _fake_decrypt)
    monkeypatch.setattr(appmod, "_call_openrouter_proactive_gate", _fake_llm_gate)

    api_key = "test_proactive_auto_key"
    user_id = "usr_endpoint_proactive_auto"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
    store.frames_meta.append({
        "id": "abcd1234abcd1234",
        "filename": "abcd1234abcd1234.env.json",
        "ts": appmod.time.time(),
        "encrypted": True,
        "app": None,
        "ocr_text": "",
    })

    client = appmod.app.test_client()
    resp = client.post("/v1/proactive/tick", headers={"X-API-Key": api_key}, json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enqueued"] is True
    assert body["decision"]["gate_model"] == "openrouter:google/gemini-3.1-flash-lite"
    assert body["decision"]["reason"] == "model_detected_helpful_moment"
    assert body["decision"]["gate_input"]["decrypt_ok"] is True
    assert body["decision"]["gate_input"]["image_count"] == 1
    assert body["decision"]["gate_input"]["llm_called"] is True
    assert body["job"]["frame_ids"] == ["abcd1234abcd1234"]


def test_auto_proactive_gate_requires_model_even_with_strong_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(appmod, "OPENROUTER_API_KEY", "")
    appmod._stores.clear()

    def _fake_decrypt(_store, frame_id, _api_key, include_image=False):
        return {
            "frame_id": frame_id,
            "app": "xhs",
            "ocr_text": "帮我总结这段，然后压成一句可以发的观点。",
            "image_b64": "ZmFrZS1qcGVn" if include_image else "",
            "image_mime": "image/jpeg",
        }

    monkeypatch.setattr(appmod, "_decrypt_frame_metadata_for_gate", _fake_decrypt)

    api_key = "test_proactive_auto_no_model_key"
    user_id = "usr_endpoint_proactive_auto_no_model"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
    store.frames_meta.append({
        "id": "efef1234efef1234",
        "filename": "efef1234efef1234.env.json",
        "ts": appmod.time.time(),
        "encrypted": True,
    })

    client = appmod.app.test_client()
    resp = client.post("/v1/proactive/tick", headers={"X-API-Key": api_key}, json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enqueued"] is False
    assert body["decision"]["should_reach_out"] is False
    assert body["decision"]["reason"] == "model_not_configured"
    assert body["decision"]["gate_input"]["llm_called"] is True


def test_auto_proactive_gate_records_false_without_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_proactive_auto_false_key"
    user_id = "usr_endpoint_proactive_auto_false"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id

    client = appmod.app.test_client()
    resp = client.post("/v1/proactive/tick", headers={"X-API-Key": api_key}, json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enqueued"] is False
    assert body["decision"]["should_reach_out"] is False
    assert body["decision"]["reason"] == "no_recent_frames"


def test_proactive_job_claim_and_status_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_proactive_claim_key"
    user_id = "usr_endpoint_proactive_claim"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)

    decision = appmod._build_proactive_gate_decision(
        store,
        {"force": True, "context_hint": "claim test"},
    )
    job = store.append_proactive_job(appmod._proactive_job_from_decision(decision))

    client = appmod.app.test_client()
    headers = {"X-API-Key": api_key}

    claim = client.post(
        f"/v1/proactive/jobs/{job['job_id']}/claim",
        headers=headers,
        json={"consumer_id": "consumer-a"},
    )
    assert claim.status_code == 200
    assert claim.get_json()["claimed"] is True

    poll = client.get("/v1/proactive/jobs/poll?since=0&timeout=0", headers=headers)
    assert poll.status_code == 200
    assert poll.get_json()["jobs"] == []

    status = client.post(
        f"/v1/proactive/jobs/{job['job_id']}/status",
        headers=headers,
        json={"status": "failed", "reason": "agent_call_failed", "consumer_id": "consumer-a"},
    )
    assert status.status_code == 200

    snapshot = appmod._proactive_debug_snapshot(store)
    row = snapshot["jobs"][0]
    assert row["derived_status"] == "failed"
    assert row["status_reason"] == "agent_call_failed"
    assert row["consumer_id"] == "consumer-a"


def test_proactive_chat_response_records_push_delivery_results(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(appmod, "_gate_bootstrap_for_chat", lambda store: None)
    appmod._stores.clear()

    sent_push_types = []

    def _fake_send_apns(device_token, payload, push_type, topic):
        sent_push_types.append(push_type)
        return {"status": "delivered"}

    monkeypatch.setattr(appmod, "_send_apns", _fake_send_apns)

    api_key = "test_proactive_delivery_key"
    user_id = "usr_endpoint_proactive_delivery"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
    store.tokens = [
        {
            "type": "live_activity",
            "token": "live-token",
            "activity_id": "activity_1",
            "status": "active",
            "registered_at": "2026-05-24T00:00:00",
        },
        {
            "type": "device",
            "token": "device-token",
            "status": "active",
            "registered_at": "2026-05-24T00:00:00",
        },
    ]

    client = appmod.app.test_client()
    headers = {"X-API-Key": api_key}
    envelope = {
        "id": "msg_delivery_1",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
    }

    resp = client.post(
        "/v1/chat/response",
        headers=headers,
        json={
            "envelope": envelope,
            "source": appmod.PROACTIVE_JOB_SOURCE,
            "gate_decision_id": "gd_delivery",
            "proactive_job_id": "pj_delivery",
            "alert_body": "我看到你停在这里了。",
            "push_live_activity": True,
            "push_body": "我看到你停在这里了。",
        },
    )

    assert resp.status_code == 200
    assert sent_push_types == ["liveactivity", "alert"]
    snapshot = appmod._proactive_debug_snapshot(store)
    msg = snapshot["proactive_messages"][0]
    assert msg["alert_preview"] == "我看到你停在这里了。"
    assert msg["alert_status"] == "delivered"
    assert msg["live_activity_status"] == "delivered"
