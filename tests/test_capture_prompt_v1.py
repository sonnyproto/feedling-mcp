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


# --- A9 bucket convergence: one shared bilingual canonical vocabulary ----------
# onboarding + capture + migration must steer toward the SAME reusable bucket set
# instead of each card minting a fresh near-synonym (工作/职业/事业) or scattering.

def test_capture_prompt_carries_canonical_buckets():
    from memory.prompts_v1 import COMMON_BUCKETS_LINE_V1, COMMON_BUCKETS_V1
    p = build_capture_prompt(
        ai_name="io", user_name="hx", buckets="（暂无）", threads="（暂无）",
        identity="x", window="y",
    )
    assert COMMON_BUCKETS_LINE_V1 in p   # seed list injected even with no existing buckets
    assert "工作/Work" in p              # bilingual pair present
    assert COMMON_BUCKETS_V1 and all(zh.strip() and en.strip() for zh, en in COMMON_BUCKETS_V1)
    # companion-tuned set (hx): 14 buckets incl. the relationship/emotion/boundary ones
    assert len(COMMON_BUCKETS_V1) == 14
    for pair in (("宠物", "Pets"), ("偏好与边界", "Preferences & boundaries"),
                 ("个性与价值观", "Personality & values"), ("我们的关系", "Our relationship")):
        assert pair in COMMON_BUCKETS_V1


def test_migrate_and_genesis_share_the_same_canonical_buckets():
    from memory.prompts_v1 import COMMON_BUCKETS_LINE_V1
    from memory.migrate_prompt_v1 import build_migrate_prompt
    from genesis.prompts import FACT_WRITE_PROMPT
    mig = build_migrate_prompt(ai_name="io", user_name="hx", old_cards="c", vocab="（暂无）")
    assert COMMON_BUCKETS_LINE_V1 in mig
    # onboarding (genesis FACT_WRITE) had NO bucket guidance before A9 — now it converges too
    assert COMMON_BUCKETS_LINE_V1 in FACT_WRITE_PROMPT
    assert "桶名收敛" in FACT_WRITE_PROMPT
