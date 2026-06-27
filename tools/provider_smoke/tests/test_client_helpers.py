from tools.provider_smoke import client


def test_is_hosted_response_true_for_202_contract():
    body = {"status": "processing",
            "runtime": {"engine": "feedling_agent_runtime", "mode": "hosted_agent"}}
    assert client.is_hosted_response(body)


def test_is_hosted_response_false_for_native_200():
    body = {"status": "ok", "reply": "hi", "runtime": {"engine": "native"}}
    assert not client.is_hosted_response(body)


def test_newest_openclaw_after_filters_and_picks_latest():
    msgs = [
        {"role": "user", "ts": 100, "body_ct": "x"},        # not openclaw
        {"role": "openclaw", "ts": 90, "body_ct": "old"},   # before cutoff
        {"role": "openclaw", "ts": 110, "body_ct": "a"},    # candidate
        {"role": "openclaw", "ts": 120, "body_ct": "b"},    # newest candidate
        {"role": "openclaw", "ts": 130, "body_ct": ""},     # no body -> skip
    ]
    picked = client.newest_openclaw_after(msgs, after_ts=100)
    assert picked["ts"] == 120 and picked["body_ct"] == "b"


def test_newest_openclaw_after_returns_none_when_empty():
    assert client.newest_openclaw_after([], after_ts=0) is None


def test_newest_openclaw_after_accepts_assistant_and_agent_roles():
    msgs = [
        {"role": "assistant", "ts": 110, "body_ct": "a"},
        {"role": "agent", "ts": 120, "body_ct": "b"},
    ]
    picked = client.newest_openclaw_after(msgs, after_ts=100)
    assert picked["ts"] == 120


def test_is_hosted_response_false_when_processing_without_runtime():
    assert not client.is_hosted_response({"status": "processing"})


def test_identity_init_body_has_required_fields():
    body = client.identity_init_body()
    assert set(body) >= {"identity", "days_with_user", "relationship_anchor_evidence"}
    assert body["days_with_user"] == 0 and isinstance(body["days_with_user"], int)
    assert len(body["relationship_anchor_evidence"]) >= 8
    assert set(body["identity"]) >= {"agent_name", "self_introduction", "dimensions"}
    assert body["identity"]["dimensions"] == []
