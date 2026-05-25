import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).parent.parent / "tools" / "proactive_gate_eval.py"
_SPEC = importlib.util.spec_from_file_location("proactive_gate_eval", _SCRIPT)
evalmod = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(evalmod)


def test_proactive_gate_eval_summarizes_human_reviews():
    snapshot = {
        "decisions": [
            {
                "decision_id": "gd_true",
                "should_reach_out": True,
                "reason": "memory_connection",
                "intent_label": "companion_notice",
                "connection": {"source_id": "mom_1"},
                "context_hint": "The user is looking at a topic tied to mom_1.",
                "frame_ids": ["f1"],
            },
            {
                "decision_id": "gd_false",
                "should_reach_out": False,
                "abstention_reason": "no_concrete_connection",
                "intent_label": "llm_false",
                "frame_ids": ["f2"],
            },
        ],
        "reviews": [
            {"decision_id": "gd_true", "label": "great_companion_moment", "notes": "felt right"},
            {"decision_id": "gd_false", "label": "missed_opportunity", "notes": "should have noticed"},
        ],
    }

    report = evalmod.summarize(snapshot)

    assert report["reviewed"] == 2
    assert report["confusion"]["tp"] == 1
    assert report["confusion"]["fn"] == 1
    assert report["precision"] == 1.0
    assert report["recall"] == 0.5
