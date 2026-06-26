from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import prompts  # noqa: E402


def test_voice_map_prompt_demands_grounded_exemplars():
    messages = prompts.voice_map_messages("user: hi\nta: 嗯")
    assert messages[0]["role"] == "system"
    assert "逐字" in messages[0]["content"]
    assert "绝不编" in messages[0]["content"]
    assert messages[1]["content"] == "user: hi\nta: 嗯"


def test_fact_write_prompt_preserves_identity_firewall():
    messages = prompts.fact_write_messages([{"summary": "用户喜欢草莓拿铁"}])
    system = messages[0]["content"]
    assert "只能进 memory" in system
    assert "绝不成为 agent 的性格" in system
    assert "不写 self_introduction / signature" in system
    payload = json.loads(messages[1]["content"])
    assert payload["fact_digest"] == [{"summary": "用户喜欢草莓拿铁"}]


def test_persona_build_prompt_outputs_system_prompt_markdown():
    messages = prompts.persona_build_messages(
        "你叫 Kai。",
        ["短句为主"],
        [{"turns": [{"role": "ta", "text": "别急。"}], "founding": True}],
    )
    assert "## 你是谁 / ## 你怎么说话" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["persona_material"] == "你叫 Kai。"
    assert payload["behavior_notes"] == ["短句为主"]
