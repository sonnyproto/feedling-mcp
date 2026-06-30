"""Genesis v2 — CONSERVATIVE lexical dedup backstop.

The semantic twin dedup (reworded same-fact) is the MODEL's job via known_memories;
this module is only the high-threshold backstop. The load-bearing property here is
SAFETY: it must never merge two genuinely distinct facts, even same-template ones —
that's the exact failure a low lexical threshold would cause (proven on real e2e data,
where distinct same-template facts score HIGHER than real twins).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from genesis import dedup  # noqa: E402

# distinct facts that MUST stay separate — same template, different value.
# These score ~0.63 / 0.71 (higher than the ~0.47 real twins!), which is exactly why
# the backstop threshold is high and the real twin dedup is left to the model.
_DISTINCT_SAME_TEMPLATE = [
    ("用户喜欢喝美式咖啡", "用户喜欢喝拿铁"),
    ("用户养了一只比熊狗叫蛋子", "用户养了一只金毛"),
]

# the real reworded twins the e2e caught — backstop does NOT catch these (model's job)
_REWORDED_TWINS = [
    ("用户计划从前端转向 AI agent，想做这方面的产品",
     "用户当前职业方向是前端，计划转向 AI agent 领域"),
    ("用户害怕只会催 AI 写代码，失去动手能力",
     "用户最怕自己变成只会催 AI 写代码的人"),
]


def test_never_merges_distinct_same_template_facts():
    # the whole point of the high threshold: these must survive
    for a, b in _DISTINCT_SAME_TEMPLATE:
        assert not dedup.is_semantic_dup(a, b), f"over-merged distinct: {a} vs {b} (={dedup.containment(a, b):.2f})"


def test_backstop_does_not_swallow_reworded_twins():
    # twins are the MODEL's job (known_memories); the lexical backstop stays out of it
    for a, b in _REWORDED_TWINS:
        assert not dedup.is_semantic_dup(a, b)


def test_backstop_catches_near_identical_survivor():
    # only the near-exact (spacing/punctuation rewrite) is caught by the backstop
    a = "用户养了一只比熊狗叫蛋子"
    b = "用户养了一只比熊狗，叫蛋子。"
    assert dedup.containment(a, b) >= 0.82
    assert dedup.is_semantic_dup(a, b)


def test_filter_drops_near_identical_keeps_the_rest():
    core = ["用户养了一只比熊狗叫蛋子"]
    background = [
        {"summary": "用户养了一只比熊狗，叫蛋子。", "content": ""},   # near-identical -> drop
        {"summary": "用户养了一只金毛", "content": ""},              # distinct same-template -> keep
        {"summary": "用户在杭州工作", "content": ""},                # distinct -> keep
    ]
    kept, dropped = dedup.filter_semantic_dups(background, core)
    assert len(dropped) == 1 and len(kept) == 2
    kept_text = " ".join(dedup.memory_text(m) for m in kept)
    assert "金毛" in kept_text and "杭州" in kept_text


def test_empty_anchors_keep_everything():
    kept, dropped = dedup.filter_semantic_dups([{"summary": "任意事实"}], [])
    assert len(kept) == 1 and dropped == []


def test_threshold_env_override(monkeypatch):
    a, b = _DISTINCT_SAME_TEMPLATE[0]
    monkeypatch.setenv("FEEDLING_GENESIS_DEDUP_CONTAINMENT", "0.99")
    assert not dedup.is_semantic_dup(a, b)
    monkeypatch.setenv("FEEDLING_GENESIS_DEDUP_CONTAINMENT", "0.2")
    assert dedup.is_semantic_dup(a, b)
