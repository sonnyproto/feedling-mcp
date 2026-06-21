from __future__ import annotations

import json
import sys
import types
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import hosted_runtime as runtime  # noqa: E402
import memory_readside_core as readside_core  # noqa: E402
from memory import actions as memory_actions  # noqa: E402


def _install_memory_action_fakes(monkeypatch, moments: list[dict]) -> list[dict]:
    saved: list[dict] = []
    envelope_counter = {"value": 0}

    def fake_load(_store):
        return list(moments)

    def fake_save(_store, new_moments):
        saved[:] = [dict(moment) for moment in new_moments]
        moments[:] = [dict(moment) for moment in new_moments]

    def fake_envelope(store, inner, *, item_id=None):
        envelope_counter["value"] += 1
        eid = item_id or f"mem_new_{envelope_counter['value']}"
        return {
            "id": eid,
            "body_ct": json.dumps(inner, ensure_ascii=False),
            "nonce": f"nonce_{eid}",
            "K_user": f"ku_{eid}",
            "K_enclave": f"ke_{eid}",
            "enclave_pk_fpr": "fpr_test",
            "visibility": "shared",
            "owner_user_id": store.user_id,
        }, ""

    monkeypatch.setattr(memory_actions.memory_service, "_load_moments", fake_load)
    monkeypatch.setattr(memory_actions.memory_service, "_save_moments", fake_save)
    monkeypatch.setattr(memory_actions, "_build_memory_envelope_for_store", fake_envelope)
    monkeypatch.setattr(memory_actions.boot_gates, "_log_bootstrap_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        memory_actions.memory_service,
        "_append_memory_change",
        lambda _store, change: {"id": "chg_test", **change},
    )
    return saved


def _inner(moment: dict) -> dict:
    return json.loads(moment["body_ct"])


def test_memory_add_writes_card_v1_metadata_and_legacy_body_fields(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_m2")
    moments: list[dict] = []
    saved = _install_memory_action_fakes(monkeypatch, moments)

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.add",
            "memory": {
                "type": "fact",
                "summary": "用户有只猫叫武松，是狸花猫。",
                "verbatim": "我有只猫叫武松，是狸花猫。",
                "salience": "high",
                "importance": 0.8,
                "occurred_at": "2026-06-21",
                "source": "hosted_runtime_state",
            },
            "reason": "User explicitly stated a durable pet fact.",
        }
    ])

    assert status == 200
    assert body["status"] == "ok"
    assert len(saved) == 1
    moment = saved[0]
    assert moment["card_v"] == 1
    assert moment["status"] == "active"
    assert moment["salience"] == "high"
    assert moment["importance"] == 0.8
    assert moment["source_type"] == "hosted_runtime_state"
    inner = _inner(moment)
    assert inner["summary"] == "用户有只猫叫武松，是狸花猫。"
    assert inner["verbatim"] == "我有只猫叫武松，是狸花猫。"
    assert inner["description"] == inner["summary"]
    assert inner["her_quote"] == inner["verbatim"]
    assert inner["title"]


def test_memory_supersede_soft_retires_old_card_and_new_card_is_recallable(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_m2")
    old = {
        "v": 1,
        "id": "mem_old_cat",
        "type": "fact",
        "owner_user_id": "usr_m2",
        "visibility": "shared",
        "body_ct": json.dumps({"summary": "武松是狸花猫", "description": "武松是狸花猫"}),
        "nonce": "nonce_old",
        "K_user": "ku_old",
        "K_enclave": "ke_old",
        "enclave_pk_fpr": "fpr_test",
        "occurred_at": "2026-06-20",
        "created_at": "2026-06-20",
        "updated_at": "2026-06-20",
        "source": "hosted_runtime_state",
        "status": "active",
    }
    moments = [old]
    saved = _install_memory_action_fakes(monkeypatch, moments)

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.supersede",
            "supersedes": "mem_old_cat",
            "memory": {
                "type": "fact",
                "summary": "武松其实是橘猫。",
                "verbatim": "我记错了，武松其实是橘猫。",
                "occurred_at": "2026-06-21",
                "source": "hosted_runtime_state",
            },
            "reason": "User corrected the cat breed.",
        }
    ])

    assert status == 200
    assert body["status"] == "ok"
    assert len(saved) == 2
    old_after = next(moment for moment in saved if moment["id"] == "mem_old_cat")
    new_card = next(moment for moment in saved if moment["id"] != "mem_old_cat")
    assert old_after["status"] == "superseded"
    assert old_after["superseded_by"] == new_card["id"]
    assert old_after["is_archived"] is True
    assert old_after["archive_reason"] == f"superseded_by:{new_card['id']}"
    assert new_card["status"] == "active"
    assert new_card["supersedes"] == ["mem_old_cat"]
    assert readside_core.memory_available(old_after, "usr_m2") is False
    assert readside_core.memory_available(old_after, "usr_m2", include_superseded=True, include_archived=True) is True
    assert readside_core.memory_available(new_card, "usr_m2") is True


def test_coerce_runtime_action_maps_memory_supersede_to_executor_action():
    action = {
        "type": "memory.supersede",
        "confidence": 0.96,
        "target": {"memory_id": "mem_old_cat"},
        "payload": {
            "memory": {
                "type": "fact",
                "summary": "武松其实是橘猫。",
                "verbatim": "我记错了，武松其实是橘猫。",
                "occurred_at": "2026-06-21",
            }
        },
        "reason": "User corrected an old cat breed memory.",
    }

    coerced = runtime.coerce_runtime_action(action, [], direct_confidence=0.9)

    assert coerced is not None
    assert coerced["domain"] == "memory"
    assert coerced["requires_confirmation"] is False
    assert coerced["executor_action"] == {
        "type": "memory.supersede",
        "supersedes": "mem_old_cat",
        "memory": {
            "type": "fact",
            "summary": "武松其实是橘猫。",
            "verbatim": "我记错了，武松其实是橘猫。",
            "occurred_at": "2026-06-21",
            "source": "hosted_runtime_state",
        },
        "reason": "User corrected an old cat breed memory.",
        "capture_mode": "state",
    }


def test_background_execution_prompt_advertises_memory_supersede():
    messages = runtime.build_background_execution_messages(
        user_message="我记错了，武松其实是橘猫。",
        identity={},
        memory_candidates=[{"id": "mem_old_cat", "title": "武松是狸花猫"}],
        context_refs=[],
        pending_items=[],
    )

    system_prompt = messages[0]["content"]
    assert "memory.supersede" in system_prompt
    assert "target.memory_id" in system_prompt


def test_memory_content_patch_keeps_summary_and_legacy_fields_in_sync(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_m2")
    existing = {
        "v": 1,
        "id": "mem_patch_cat",
        "type": "fact",
        "owner_user_id": "usr_m2",
        "visibility": "shared",
        "body_ct": json.dumps({"summary": "旧摘要", "description": "旧摘要"}),
        "nonce": "nonce_patch",
        "K_user": "ku_patch",
        "K_enclave": "ke_patch",
        "enclave_pk_fpr": "fpr_test",
        "occurred_at": "2026-06-20",
        "created_at": "2026-06-20",
        "updated_at": "2026-06-20",
        "source": "hosted_runtime_state",
        "status": "active",
    }
    moments = [existing]
    saved = _install_memory_action_fakes(monkeypatch, moments)
    monkeypatch.setattr(memory_actions, "_memory_plain_from_envelope", lambda _moment, _api_key: (_inner(existing), ""))

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.content_patch",
            "memory_id": "mem_patch_cat",
            "patch": {
                "summary": "用户有只猫叫武松，是橘猫。",
                "verbatim": "我记错了，武松其实是橘猫。",
            },
            "reason": "Patch should keep Garden and readside in sync.",
        }
    ])

    assert status == 200
    assert body["status"] == "ok"
    inner = _inner(saved[0])
    assert inner["summary"] == "用户有只猫叫武松，是橘猫。"
    assert inner["description"] == inner["summary"]
    assert inner["verbatim"] == "我记错了，武松其实是橘猫。"
    assert inner["her_quote"] == inner["verbatim"]
