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


def test_proactive_debug_folds_no_frame_gate_ticks(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    store = appmod.UserStore("usr_test_proactive_folded_gate")

    no_frame = {
        "decision_id": "gd_no_frame",
        "ts": 1000,
        "gate_model": "openrouter:google/gemini-3.1-flash-lite",
        "should_reach_out": False,
        "reason": "no_recent_frames_unit_test",
        "abstention_reason": "no_recent_frames",
        "intent_label": "blocked_before_model",
        "connection": {},
        "frame_ids": [],
        "gate_input": {
            "ocr_chars": 0,
            "sampled_frame_count": 0,
            "decrypt_ok": False,
            "image_count": 0,
        },
    }
    with_frame = {
        "decision_id": "gd_with_frame",
        "ts": 1001,
        "gate_model": "openrouter:google/gemini-3.1-flash-lite",
        "should_reach_out": False,
        "reason": "frame_backed_false_unit_test",
        "abstention_reason": "model_false",
        "intent_label": "reviewable_false",
        "connection": {},
        "frame_ids": [],
        "gate_input": {
            "ocr_chars": 42,
            "sampled_frame_count": 2,
            "decrypt_ok": True,
            "image_count": 1,
        },
    }
    store.append_gate_decision(no_frame)
    store.append_gate_decision(with_frame)

    assert appmod._gate_decision_has_frame_context(no_frame) is False
    assert appmod._gate_decision_has_frame_context(with_frame) is True

    snapshot = appmod._proactive_debug_snapshot(store)
    with appmod.app.test_request_context("/debug/proactive?key=test&lang=zh"):
        page = appmod._render_proactive_dashboard(snapshot)

    assert "主表判定 1" in page
    assert "隐藏空 tick 1" in page
    assert "显示隐藏的无屏幕帧 Gate 空 tick（1）" in page
    assert page.find("frame_backed_false_unit_test") < page.find("no_recent_frames_unit_test")

    with appmod.app.test_request_context("/debug/proactive?key=test&lang=en"):
        page_en = appmod._render_proactive_dashboard(snapshot)

    assert "visible decisions 1" in page_en
    assert "hidden no-frame ticks 1" in page_en
    assert "Show hidden no-frame Gate ticks (1)" in page_en


def test_proactive_debug_translates_prose_only_in_zh_view(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    store = appmod.UserStore("usr_test_proactive_debug_translate")
    reason = "The screen has a concrete connection to the user's memory garden."
    context_hint = "The user is comparing a dense note and may want one gentle nudge."
    abstention = "The companion has already successfully engaged with the user's current context and provided a relevant response."
    decision = {
        "decision_id": "gd_translate",
        "ts": 1002,
        "gate_model": "openrouter:google/gemini-3.1-flash-lite",
        "should_reach_out": True,
        "reason": reason,
        "abstention_reason": "",
        "intent_label": "manual_proactive_test",
        "connection": {},
        "frame_ids": ["frame_translate"],
        "gate_input": {
            "ocr_chars": 120,
            "sampled_frame_count": 1,
            "decrypt_ok": True,
            "image_count": 1,
        },
        "context_hint": context_hint,
    }
    false_decision = {
        "decision_id": "gd_translate_false",
        "ts": 1003,
        "gate_model": "openrouter:google/gemini-3.1-flash-lite",
        "should_reach_out": False,
        "reason": "already_responded",
        "abstention_reason": abstention,
        "intent_label": "already_responded",
        "connection": {},
        "frame_ids": ["frame_translate_2"],
        "gate_input": {
            "ocr_chars": 120,
            "sampled_frame_count": 1,
            "decrypt_ok": True,
            "image_count": 1,
        },
        "context_hint": "",
    }
    store.append_gate_decision(decision)
    store.append_gate_decision(false_decision)
    snapshot = appmod._proactive_debug_snapshot(store)

    monkeypatch.setattr(
        appmod,
        "_translate_debug_texts_to_zh",
        lambda texts: {
            reason: "屏幕内容和用户的记忆花园有明确关联。",
            context_hint: "用户正在比较一段密集笔记，可能适合轻轻提醒一句。",
            abstention: "陪伴者已经结合用户当前上下文给过合适回复。",
        },
    )

    with appmod.app.test_request_context("/debug/proactive?key=test&lang=zh"):
        page_zh = appmod._render_proactive_dashboard(snapshot)
    with appmod.app.test_request_context("/debug/proactive?key=test&lang=en"):
        page_en = appmod._render_proactive_dashboard(snapshot)

    assert "屏幕内容和用户的记忆花园有明确关联。" in page_zh
    assert "陪伴者已经结合用户当前上下文给过合适回复。" in page_zh
    assert "已经回应过" in page_zh
    assert "title='The screen has a concrete connection to the user&#x27;s memory garden.'" in page_zh
    assert "title='The companion has already successfully engaged with the user&#x27;s current context" in page_zh
    assert "The screen has a concrete connection" in page_en
    assert "屏幕内容和用户的记忆花园有明确关联。" not in page_en
    assert "陪伴者已经结合用户当前上下文给过合适回复。" not in page_en
    assert snapshot["decisions"][0]["reason"] == reason


def test_proactive_settings_persists_timezone(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_proactive_timezone_key"
    user_id = "usr_endpoint_proactive_timezone"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id

    client = appmod.app.test_client()
    headers = {"X-API-Key": api_key}

    resp = client.post(
        "/v1/proactive/settings",
        headers=headers,
        json={"timezone": "Asia/Tokyo"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["timezone"] == "Asia/Tokyo"

    bad = client.post(
        "/v1/proactive/settings",
        headers=headers,
        json={"timezone": "Not/AZone"},
    )
    assert bad.status_code == 200
    assert bad.get_json()["timezone"] == "Asia/Tokyo"


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

    page = client.get("/debug/proactive?lang=zh", headers=headers)
    assert page.status_code == 200
    assert "Feedling 主动触发调试台".encode() in page.data
    assert body["job"]["job_id"].encode() in page.data
    # Section header is always present once the jobs list renders; the
    # previous "frames sent" probe relied on a table column header that
    # the new card layout only emits when a job actually has frames.
    assert "隐藏任务".encode() in page.data

    page_en = client.get("/debug/proactive?lang=en", headers=headers)
    assert page_en.status_code == 200
    assert b"Feedling Proactive Debug" in page_en.data
    assert b"Hidden Jobs" in page_en.data


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
        assert kwargs["gate_context"]["memory_set"][0]["id"] == "mom_rewrite"
        return {
            "ok": True,
            "raw": {
                "should_reach_out": True,
                "confidence": 0.88,
                "intent_label": "help_compress_point",
                "context_hint": "The user is looking at a dense post and may want help turning it into one usable sentence.",
                "reason": "model_detected_helpful_moment",
                "frame_ids": ["abcd1234abcd1234"],
                "connection": {
                    "source_type": "memory_set",
                    "source_id": "mom_rewrite",
                    "quote": "The user often asks Dora to compress dense ideas into a sendable point.",
                    "why_concrete": "The screen asks whether to compress three paragraphs into one point.",
                },
            },
            "usage": {"total_tokens": 123},
        }

    monkeypatch.setattr(appmod, "_decrypt_frame_metadata_for_gate", _fake_decrypt)
    monkeypatch.setattr(appmod, "_call_openrouter_proactive_gate", _fake_llm_gate)
    monkeypatch.setattr(appmod, "_build_gate_memory_context", lambda *_args, **_kwargs: {
        "identity_card": {"agent_name": "Dora"},
        "memory_set": [{
            "id": "mom_rewrite",
            "type": "fact",
            "title": "The user likes turning dense arguments into compact sendable points.",
            "description": "Dora has helped compress notes into concise arguments before.",
        }],
        "passive_observations": [],
        "recent_fires": [],
        "now_local": {"iso": "2026-05-24T10:00:00-04:00"},
        "connection_candidates": [{
            "source_type": "memory_set",
            "source_id": "mom_rewrite",
            "quote": "The user likes turning dense arguments into compact sendable points.",
        }],
        "context_errors": {"identity": "", "memory": ""},
    })

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
    assert body["decision"]["connection"]["source_id"] == "mom_rewrite"
    assert body["job"]["frame_ids"] == ["abcd1234abcd1234"]
    assert body["job"]["connection"]["source_id"] == "mom_rewrite"


def test_auto_proactive_gate_does_not_block_after_recent_user_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(appmod, "OPENROUTER_API_KEY", "sk-test")
    appmod._stores.clear()

    monkeypatch.setattr(appmod, "_decrypt_frame_metadata_for_gate", lambda _store, frame_id, _api_key, include_image=False: {
        "frame_id": frame_id,
        "app": "xhs",
        "ocr_text": "这段内容和我们之前聊过的压缩观点有关。",
        "image_b64": "ZmFrZS1qcGVn" if include_image else "",
        "image_mime": "image/jpeg",
    })
    monkeypatch.setattr(appmod, "_call_openrouter_proactive_gate", lambda **kwargs: {
        "ok": True,
        "raw": {
            "should_reach_out": True,
            "confidence": 0.86,
            "intent_label": "memory_connection",
            "context_hint": "The user is looking at a screen tied to a known memory.",
            "reason": "model_detected_memory_connection",
            "frame_ids": ["recentchat123456"],
            "connection": {
                "source_type": "memory_set",
                "source_id": "mom_known",
                "quote": "The user likes compact observations.",
                "why_concrete": "The screen is about compressing a point.",
            },
        },
        "usage": {"total_tokens": 88},
    })
    monkeypatch.setattr(appmod, "_build_gate_memory_context", lambda *_args, **_kwargs: {
        "identity_card": {"agent_name": "Dora"},
        "memory_set": [{"id": "mom_known", "title": "The user likes compact observations."}],
        "passive_observations": [],
        "recent_fires": [],
        "now_local": {"iso": "2026-05-24T10:00:00-04:00"},
        "connection_candidates": [{
            "source_type": "memory_set",
            "source_id": "mom_known",
            "quote": "The user likes compact observations.",
        }],
        "context_errors": {"identity": "", "memory": ""},
    })

    api_key = "test_proactive_recent_chat_key"
    user_id = "usr_endpoint_proactive_recent_chat"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
    store.append_chat("user", "ios", {
        "id": "msg_recent_user",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "visibility": "shared",
        "owner_user_id": user_id,
    })
    store.frames_meta.append({
        "id": "recentchat123456",
        "filename": "recentchat123456.env.json",
        "ts": appmod.time.time(),
        "encrypted": True,
    })

    client = appmod.app.test_client()
    resp = client.post("/v1/proactive/tick", headers={"X-API-Key": api_key}, json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enqueued"] is True
    assert body["decision"]["reason"] == "model_detected_memory_connection"


def test_recent_proactive_fire_cooldown_is_ten_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    store = appmod.UserStore("usr_test_proactive_cooldown")
    now = appmod.time.time()
    envelope = {
        "id": "msg_fire",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "k-user",
        "visibility": "shared",
        "owner_user_id": store.user_id,
    }
    msg = store.append_chat("openclaw", appmod.PROACTIVE_JOB_SOURCE, envelope)

    msg["ts"] = now - 599
    assert appmod._recent_proactive_fire_active(store, now) is True

    msg["ts"] = now - 601
    assert appmod._recent_proactive_fire_active(store, now) is False


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
    monkeypatch.setattr(appmod, "_build_gate_memory_context", lambda *_args, **_kwargs: {
        "identity_card": {"agent_name": "Dora"},
        "memory_set": [{"id": "mom_summary", "title": "User often asks for concise summaries."}],
        "passive_observations": [],
        "recent_fires": [],
        "now_local": {"iso": "2026-05-24T10:00:00-04:00"},
        "connection_candidates": [{
            "source_type": "memory_set",
            "source_id": "mom_summary",
            "quote": "User often asks for concise summaries.",
        }],
        "context_errors": {"identity": "", "memory": ""},
    })

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


def test_llm_gate_true_requires_known_concrete_connection():
    raw = {
        "should_reach_out": True,
        "confidence": 0.9,
        "intent_label": "screen_context",
        "context_hint": "The user is on a screen tied to a memory.",
        "reason": "has_connection",
        "connection": {
            "source_type": "memory_set",
            "source_id": "mom_unknown",
            "quote": "unknown",
            "why_concrete": "claims a match",
        },
        "frame_ids": ["frame_a"],
    }

    decision = appmod._coerce_llm_gate_payload(raw, ["frame_a"], {"mom_known"})

    assert decision["should_reach_out"] is False
    assert decision["abstention_reason"] == "llm_unrecognized_connection"
    assert decision["context_hint"] == ""


def test_gate_review_endpoint_records_human_label(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_gate_review_key"
    user_id = "usr_gate_review"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)

    decision = appmod._build_proactive_gate_decision(
        store,
        {
            "force": True,
            "context_hint": "The user is testing the review harness.",
            "intent_label": "manual_proactive_test",
        },
    )
    store.append_gate_decision(decision)

    client = appmod.app.test_client()
    resp = client.post(
        f"/v1/proactive/decisions/{decision['decision_id']}/review",
        headers={"X-API-Key": api_key},
        json={"label": "great_companion_moment", "notes": "felt natural"},
    )

    assert resp.status_code == 200
    review = resp.get_json()["review"]
    assert review["label"] == "great_companion_moment"
    assert review["decision_id"] == decision["decision_id"]

    snapshot = appmod._proactive_debug_snapshot(store)
    latest = snapshot["latest_review_by_decision"][decision["decision_id"]]
    assert latest["notes"] == "felt natural"

    listing = client.get("/v1/proactive/reviews?since=0", headers={"X-API-Key": api_key})
    assert listing.status_code == 200
    assert listing.get_json()["reviews"][0]["label"] == "great_companion_moment"


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
    monkeypatch.setattr(appmod, "_gate_bootstrap_for_chat", lambda store, **_: None)
    appmod._stores.clear()

    sent_push_types = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
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


def test_apns_retries_production_when_sandbox_rejects_testflight_token(monkeypatch):
    monkeypatch.setattr(appmod, "APNS_KEY", "test-key")
    monkeypatch.setattr(appmod, "APNS_SANDBOX", True)
    monkeypatch.setattr(appmod, "_make_apns_jwt", lambda: "jwt")

    calls = []

    class _Resp:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, json, headers):
            calls.append(url)
            if "sandbox" in url:
                return _Resp(400, '{"reason":"BadDeviceToken"}')
            return _Resp(200)

    monkeypatch.setattr(appmod.httpx, "Client", _Client)

    result = appmod._send_apns(
        "testflight-token",
        {"aps": {"alert": {"body": "hi"}}},
        push_type="alert",
        topic="com.feedling.mcp",
    )

    assert [("sandbox" in url) for url in calls] == [True, False]
    assert result["status"] == "delivered"
    assert result["apns_env"] == "production"
    assert result["fallback_attempted"] is True
    assert result["fallback_from"] == "sandbox"


def test_apns_retries_production_when_sandbox_returns_bad_environment_key(monkeypatch):
    monkeypatch.setattr(appmod, "APNS_KEY", "test-key")
    monkeypatch.setattr(appmod, "APNS_SANDBOX", True)
    monkeypatch.setattr(appmod, "_make_apns_jwt", lambda: "jwt")

    calls = []

    class _Resp:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, json, headers):
            calls.append(url)
            if "sandbox" in url:
                return _Resp(400, '{"reason":"BadEnvironmentKeyInToken"}')
            return _Resp(200)

    monkeypatch.setattr(appmod.httpx, "Client", _Client)

    result = appmod._send_apns(
        "testflight-token",
        {"aps": {"alert": {"body": "hi"}}},
        push_type="alert",
        topic="com.feedling.mcp",
    )

    assert [("sandbox" in url) for url in calls] == [True, False]
    assert result["status"] == "delivered"
    assert result["apns_env"] == "production"
    assert result["fallback_attempted"] is True
    assert result["fallback_from"] == "sandbox"


def test_chat_alert_falls_back_from_bad_latest_device_token(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_bad_device_token_key"
    user_id = "usr_bad_device_token"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
    store.tokens = [
        {
            "type": "device",
            "token": "old-device-token",
            "status": "active",
            "registered_at": "2026-05-24T00:00:00",
        },
        {
            "type": "device",
            "token": "new-device-token",
            "status": "active",
            "registered_at": "2026-05-29T00:00:00",
        },
    ]

    seen = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        seen.append(device_token)
        if device_token == "old-device-token":
            return {"status": "delivered", "apns_env": "production"}
        return {"status": "error", "code": 400, "reason": '{"reason":"BadDeviceToken"}', "apns_env": "production"}

    monkeypatch.setattr(appmod, "_send_apns", _fake_send_apns)

    result = appmod._send_chat_alert(store, "hello", alert_title="Dora")

    assert result["status"] == "delivered"
    assert seen == ["new-device-token", "old-device-token"]
    latest = [t for t in store.tokens if t["token"] == "new-device-token"][0]
    assert latest["status"] == "expired"
    assert "BadDeviceToken" in latest["last_error"]
    older = [t for t in store.tokens if t["token"] == "old-device-token"][0]
    assert older["status"] == "active"
    assert older["last_success_at"]


def test_live_activity_falls_back_from_topic_mismatch_token(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "FEEDLING_DIR", tmp_path)
    appmod._stores.clear()

    api_key = "test_bad_live_activity_token_key"
    user_id = "usr_bad_live_activity_token"
    appmod._key_to_user[appmod._hash_api_key(api_key)] = user_id
    store = appmod.get_store(user_id)
    store.tokens = [
        {
            "type": "live_activity",
            "token": "old-live-token",
            "status": "active",
            "activity_id": "old_activity",
            "registered_at": "2026-05-24T00:00:00",
        },
        {
            "type": "live_activity",
            "token": "new-live-token",
            "status": "active",
            "activity_id": "new_activity",
            "registered_at": "2026-05-29T00:00:00",
        },
    ]

    seen = []

    def _fake_send_apns(device_token, payload, push_type, topic, **_kwargs):
        seen.append(device_token)
        if device_token == "old-live-token":
            return {"status": "delivered", "apns_env": "production"}
        return {
            "status": "error",
            "code": 400,
            "reason": '{"reason":"DeviceTokenNotForTopic"}',
            "apns_env": "production",
        }

    monkeypatch.setattr(appmod, "_send_apns", _fake_send_apns)

    with appmod.app.test_client() as client:
        resp = client.post(
            "/v1/push/live-activity",
            headers={"X-API-Key": api_key},
            json={"body": "hello"},
        )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "delivered"
    assert seen == ["new-live-token", "old-live-token"]
    latest = [t for t in store.tokens if t["token"] == "new-live-token"][0]
    assert latest["status"] == "expired"
    assert "DeviceTokenNotForTopic" in latest["last_error"]


def test_apns_prefers_token_recorded_environment(monkeypatch):
    monkeypatch.setattr(appmod, "APNS_KEY", "test-key")
    monkeypatch.setattr(appmod, "APNS_SANDBOX", True)
    monkeypatch.setattr(appmod, "_make_apns_jwt", lambda: "jwt")

    calls = []

    class _Resp:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, json, headers):
            calls.append(url)
            return _Resp(200)

    monkeypatch.setattr(appmod.httpx, "Client", _Client)

    result = appmod._send_apns(
        "testflight-token",
        {"aps": {"alert": {"body": "hi"}}},
        push_type="alert",
        topic="com.feedling.mcp",
        preferred_env="production",
    )

    assert result["status"] == "delivered"
    assert result["apns_env"] == "production"
    assert [("sandbox" in url) for url in calls] == [False]
