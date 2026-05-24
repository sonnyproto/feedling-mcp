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
