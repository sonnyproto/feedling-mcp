"""Unit tests for the 落卡 capture prompt + parser (A-full PR C, no DB).

Pure-function coverage of capture_prompt_v1: prompt rendering and the agent
reply parser (parse_capture_cards). DB-free so it runs anywhere.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from memory.capture_prompt_v1 import (  # noqa: E402
    CAPTURE_TYPES,
    build_capture_prompt,
    parse_capture_cards,
)

_FENCE = "`" * 3


def test_prompt_renders_with_context_and_escaped_json():
    p = build_capture_prompt(
        ai_name="小柒", user_name="Seven",
        buckets="工作, 关系", threads="加班, 吵架",
        identity="伴侣三个月", window="[user] 今天开了一天会",
    )
    assert "小柒" in p and "Seven" in p
    assert "今天开了一天会" in p
    # the JSON template braces survived .format()
    assert '"cards": []' in p
    assert '"action": "add | merge | supersede | noop"' in p


def test_prompt_falls_back_to_neutral_defaults():
    p = build_capture_prompt(
        ai_name="", user_name="", buckets="", threads="", identity="", window="",
    )
    assert "（暂无）" in p and "（空）" in p


def test_parse_normal_card():
    raw = ('{"cards":[{"action":"add","type":"event","target_id":null,'
           '"bucket":"工作","threads":["加班","心率"],"summary":"开了一天会",'
           '"content":"厚正文","importance":0.8,"pulse":0.4}]}')
    cards, err = parse_capture_cards(raw)
    assert err is None and len(cards) == 1
    c = cards[0]
    assert c["action"] == "add" and c["type"] == "event"
    assert c["importance"] == 0.8 and c["pulse"] == 0.4
    assert c["threads"] == ["加班", "心率"]


def test_parse_empty_is_clean():
    cards, err = parse_capture_cards('{"cards": []}')
    assert cards == [] and err is None


def test_parse_drops_noop():
    cards, err = parse_capture_cards('{"cards":[{"action":"noop","summary":"x"}]}')
    assert cards == [] and err is None


def test_parse_coerces_insight_reflection_out():
    # capture never writes insight/reflection (those need anchors / are Dream's job)
    for bad in ("insight", "reflection", "weird"):
        raw = '{"cards":[{"action":"add","type":"%s","summary":"s","content":"c"}]}' % bad
        cards, err = parse_capture_cards(raw)
        assert len(cards) == 1 and cards[0]["type"] in CAPTURE_TYPES
        assert cards[0]["type"] == "event"  # default


def test_parse_handles_json_fence():
    raw = "好的\n" + _FENCE + 'json\n{"cards":[{"action":"add","summary":"s","content":"c"}]}\n' + _FENCE
    cards, err = parse_capture_cards(raw)
    assert err is None and len(cards) == 1


def test_parse_handles_prose_wrapped_and_clamps():
    raw = '我想了想：{"cards":[{"action":"merge","target_id":"mom_1","summary":"s","content":"c","importance":2.0,"pulse":-1}]} 就这些'
    cards, err = parse_capture_cards(raw)
    assert err is None and len(cards) == 1
    assert cards[0]["action"] == "merge" and cards[0]["target_id"] == "mom_1"
    assert cards[0]["importance"] == 1.0 and cards[0]["pulse"] == 0.0


def test_parse_garbage_returns_reason():
    cards, err = parse_capture_cards("not json at all")
    assert cards == [] and err == "no_json_object"


def test_parse_drops_hollow_card():
    cards, err = parse_capture_cards('{"cards":[{"action":"add","type":"event"}]}')
    assert cards == [] and err is None


def test_parse_caps_threads_at_eight():
    raw = ('{"cards":[{"action":"add","summary":"s","content":"c",'
           '"threads":["a","b","c","d","e","f","g","h","i","j"]}]}')
    cards, err = parse_capture_cards(raw)
    assert len(cards[0]["threads"]) == 8
