# Batch 2 — VPS 身份蒸馏收敛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** VPS resident 的身份蒸馏收敛到 v2 —— 共享可执行 prompt 模板(补齐人格字段)+ card_policy 校验 + 坏 JSON 重试,外加记忆蒸馏的 f(days) 非编造引导。

**Architecture:** 新建纯 stdlib 模块 `backend/identity/distill_prompt_v1.py`(prompt 构建 + 输出解析/清洗),consumer 与(未来的)backend 共用,消灭 consumer 里的手抄 DRAFT prompt。genesis 的 fact_write 增加可选 `floor_note` 尾注(默认空 = cloud 零行为变化),resident 记忆蒸馏在"低于天数下限"时喂入非编造引导。

**Tech Stack:** Python 3.12(backend / consumer 共用),pytest,uv。

## Global Constraints

- **契约 B(lenient)**:只拦结构垃圾;稀疏/聚集/空名放行;`agent_name` 命中 RUNTIME_LABELS 时【置空】而不是整卡拒绝(优先 onboarding 成功率)。
- **cloud 零行为变化**:所有 genesis/prompts/worker 改动必须是"新增可选参数,默认值下输出逐字节等价"。
- **绝不编造**:floor 引导只说"真实支持的事实尽量都写",明写【绝不编造】。
- **prompt 措辞标 DRAFT(Seven 定稿)**:沿用现有约定,新模板文件头注释注明。
- **触碰身份写入 ⇒ 真实 test 部署 e2e**(加密信封铁律,Task 6)。
- 共享模块允许 import `identity.card_policy`(同为纯 stdlib);不得 import db/httpx/backend 重依赖(consumer 独立跑)。
- DB 测试命令:`FEEDLING_TEST_PG="postgresql://postgres:test@127.0.0.1:55432/postgres" uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest --with pytest-asyncio --with requests python -m pytest <target> -v`;纯逻辑测试可省 `FEEDLING_TEST_PG`。
- 工作目录 `/Users/hx/Projects/io/feedling-mcp-batch2`,分支 `feat/onboarding-batch2-identity-distill`。合 test 时**直接 merge 不开 PR**。

---

## File Structure

- **Create** `backend/identity/distill_prompt_v1.py` — resident 身份蒸馏的共享可执行模板:`build_resident_identity_prompt()` + `parse_identity_payload()` + `RESIDENT_IDENTITY_FIELDS`。
- **Create** `tests/test_identity_distill_prompt.py` — 模板与解析的纯逻辑测试。
- **Modify** `backend/genesis/prompts.py` — `fact_write_messages(..., floor_note="")`。
- **Modify** `backend/genesis/worker.py` — `_fact_write` / `build_memory_output_from_fact_candidates` 透传 `floor_note`。
- **Modify** `tools/chat_resident_consumer.py` — `_resident_derive_identity`(换共享模板+重试)、`_resident_existing_identity`(补全字段集)、`_resident_extract_memories`(floor_note)+ 新增 `_resident_floor_note()`。
- **Create** `tests/test_resident_identity_distill.py` — consumer 蒸馏路径测试(fake call_agent)。

---

### Task 1: 共享蒸馏模板模块 `distill_prompt_v1.py`

**Files:**
- Create: `backend/identity/distill_prompt_v1.py`
- Test: `tests/test_identity_distill_prompt.py`

**Interfaces:**
- Produces: `RESIDENT_IDENTITY_FIELDS: tuple[str, ...]`;`build_resident_identity_prompt(document: str, existing_identity: dict | None = None) -> str`;`parse_identity_payload(raw: str) -> dict | None`。Task 3 的 consumer 直接 import 这三个名字。
- Consumes: `identity.card_policy`(`sanitize_identity_card` / `validate_full_identity_card` / `is_runtime_label`,Batch 0 已在)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_identity_distill_prompt.py
"""Batch 2 A1: resident 身份蒸馏的共享可执行模板 — prompt 含全量人格字段,
解析端 sanitize + lenient(runtime-label 置空不拒卡),坏输入返 None。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from identity import distill_prompt_v1 as dp


def test_prompt_asks_for_all_persona_fields():
    p = dp.build_resident_identity_prompt("用户上传的人设材料")
    for field in ("agent_name", "self_introduction", "category", "signature",
                  "dimensions", "tone_style", "agent_role", "do_not_say", "boundaries"):
        assert field in p, field
    assert "用户上传的人设材料" in p
    # 证据优先、稀疏放行、不编造 —— cloud 契约措辞的锚点
    assert "sparse is allowed" in p
    assert "Do not invent" in p


def test_prompt_fresh_has_no_merge_block():
    p = dp.build_resident_identity_prompt("材料")
    assert "EXISTING identity card" not in p


def test_prompt_update_carries_merge_rules_and_existing_card():
    p = dp.build_resident_identity_prompt("材料", existing_identity={"agent_name": "老c"})
    assert "EXISTING identity card" in p
    assert "老c" in p
    assert "KEEP the existing card's values" in p


def test_parse_extracts_json_and_keeps_persona_fields():
    raw = '前面有废话 {"agent_name":"小明","tone_style":"短句、直接","agent_role":"同事",' \
          '"do_not_say":["宝贝"],"boundaries":["不聊政治"],"category":"锐 · 实",' \
          '"signature":["有事直说","别客套"],' \
          '"dimensions":[{"name":"直接","value":90,"description":"从不绕"}]} 后面也有'
    out = dp.parse_identity_payload(raw)
    assert out["agent_name"] == "小明"
    assert out["tone_style"] == "短句、直接"
    assert out["do_not_say"] == ["宝贝"]
    assert out["signature"] == ["有事直说", "别客套"]
    assert out["dimensions"][0]["name"] == "直接"


def test_parse_blanks_runtime_label_name_instead_of_rejecting():
    out = dp.parse_identity_payload('{"agent_name":"Claude","dimensions":[]}')
    assert out is not None
    assert out["agent_name"] == ""   # lenient: 置空,不拒卡


def test_parse_sanitizes_dimensions_via_card_policy():
    raw = '{"agent_name":"x","dimensions":[{"name":"a","value":150,"description":"d"},' \
          '{"name":"a","value":50,"description":"dup"},{"name":"","value":1}]}'
    out = dp.parse_identity_payload(raw)
    assert len(out["dimensions"]) == 1          # 去重 + 丢无名
    assert out["dimensions"][0]["value"] == 100  # clamp 到 [0,100]


def test_parse_drops_empty_persona_fields():
    out = dp.parse_identity_payload('{"agent_name":"x","tone_style":"  ","do_not_say":[],"boundaries":["", " "]}')
    assert "tone_style" not in out
    assert "do_not_say" not in out
    assert "boundaries" not in out


def test_parse_caps_list_items():
    items = [f"条目{i}" for i in range(20)]
    out = dp.parse_identity_payload('{"agent_name":"x","boundaries":' +
                                    __import__("json").dumps(items, ensure_ascii=False) + '}')
    assert len(out["boundaries"]) == 12


def test_parse_returns_none_on_garbage():
    assert dp.parse_identity_payload("没有 json") is None
    assert dp.parse_identity_payload('["not","a","dict"]') is None
    assert dp.parse_identity_payload('{"tone_style":"  ","dimensions":[]}') is None  # 清洗后空卡
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hx/Projects/io/feedling-mcp-batch2 && uv run --python 3.12 --with pytest python -m pytest tests/test_identity_distill_prompt.py -q`
Expected: FAIL `ModuleNotFoundError: No module named 'identity.distill_prompt_v1'`

- [ ] **Step 3: 实现模块**

```python
# backend/identity/distill_prompt_v1.py
"""Resident(VPS)身份蒸馏的共享可执行模板 — Batch 2 A1。

consumer(tools/chat_resident_consumer.py)import 本模块构建 prompt 并解析输出,
替代原先手抄在 consumer 里的 DRAFT prompt(只有 3 个字段、无校验、会漂移)。
措辞基于 cloud 的 hosted/history_import.py::_derive_identity_with_provider 与
_IDENTITY_UPDATE_MERGE_TEMPLATE,按 resident source adapter 适配:材料 = 用户
上传的人设文档(无 memory cards / transcript / source stats)。

DRAFT 措辞待 Seven 定稿;行为需真实 test 部署 e2e(加密信封铁律)。
纯 stdlib(仅 import 同目录 card_policy)——consumer 独立运行时也能 import。
"""
from __future__ import annotations

import json

from identity import card_policy

# consumer 侧"部分补全"读取现有卡时要保留的全字段集(Task 3 使用)。
RESIDENT_IDENTITY_FIELDS: tuple[str, ...] = (
    "agent_name", "self_introduction", "category", "signature",
    "dimensions", "tone_style", "agent_role", "do_not_say", "boundaries",
)

_STRING_CAPS = {
    "agent_name": 80,
    "self_introduction": 1200,
    "category": 240,
    "tone_style": 1200,
    "agent_role": 240,
}
_LIST_FIELDS = ("signature", "do_not_say", "boundaries")
_LIST_MAX_ITEMS = 12
_LIST_ITEM_CAP = 240

_FIELDS_SPEC = (
    "Return JSON only with fields: agent_name, self_introduction, category, "
    "signature (array of two short strings), dimensions (at most 7 objects with "
    "name, value 0-100, description; every dimension must be evidenced by the material; "
    "sparse is allowed, do not invent dimensions to fill the list), "
    "tone_style (1-3 sentences capturing HOW the companion speaks — register, verbal tics, "
    "how it addresses the user, characteristic phrasings; quote real examples from the "
    "material where possible, do not generalize to 'friendly and helpful'), "
    "agent_role (one short phrase for the companion's role/relationship to the user), "
    "do_not_say (array of short strings: names, phrasings, or topics the material shows "
    "the companion never uses — empty array if none), "
    "boundaries (array of short strings; empty array if none). "
    "tone_style/agent_role/do_not_say/boundaries capture the companion's VOICE so it "
    "survives the update — extract them from the material, not just the facts. "
    "Do not invent facts not grounded in the material. "
    "agent_name is the AI companion's own chosen or user-given name, not the user's name, "
    "account name, provider, model, runtime, platform, or product name. Only set agent_name "
    "when the material explicitly names the companion; otherwise return an empty string. "
    "self_introduction must be written in the AI companion's own voice; never describe the "
    "user as 'I'. Write every field in the language of the material. "
    "Ground every field in the material; return {} if there is no persona content."
)

_MERGE_TEMPLATE = (
    "\nThis is an UPDATE to an EXISTING identity card, not a fresh derivation.\n"
    "Existing card:\n{existing_identity_json}\n"
    "Merge rules:\n"
    "- For fields the new material ADDRESSES, use the new values (latest wins). On a SERIOUS "
    "conflict, the new material wins — the user uploaded it to change the card.\n"
    "- For fields the new material does NOT address, KEEP the existing card's values unchanged — "
    "do not blank them and do not invent replacements.\n"
    "- Keep the result COHERENT: if a trait / dimension changes, update self_introduction / "
    "tone_style to match, so no stale description from the old card survives.\n"
)


def build_resident_identity_prompt(document: str, existing_identity: dict | None = None) -> str:
    """Persona 材料 → 全字段身份卡蒸馏 prompt。existing_identity 非空时附合并规则(部分补全)。"""
    prompt = (
        "The user uploaded a character/persona description for the companion (you). "
        "Derive the identity card and return ONE JSON object, nothing else.\n"
        + _FIELDS_SPEC + "\n"
    )
    if isinstance(existing_identity, dict) and existing_identity:
        prompt += _MERGE_TEMPLATE.format(
            existing_identity_json=json.dumps(existing_identity, ensure_ascii=False))
    prompt += "--- MATERIAL ---\n" + str(document or "") + "\n--- END MATERIAL ---\n"
    return prompt


def parse_identity_payload(raw: str) -> dict | None:
    """模型输出 → 干净的 identity payload(可直接交 identity.replace),坏输入返 None。

    Lenient(契约 B):结构问题能修就修 —— dimensions 走 card_policy.sanitize
    (clamp/去重/丢畸形),runtime-label 名字【置空】而不是拒卡,字符串截断、
    列表去空 + 截 12 条。清洗后一个有效字段都不剩才返 None。"""
    raw = str(raw or "")
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None

    out: dict = {}
    for field, cap in _STRING_CAPS.items():
        val = str(obj.get(field) or "").strip()[:cap]
        if val:
            out[field] = val
    if card_policy.is_runtime_label(out.get("agent_name", "")):
        out["agent_name"] = ""  # lenient: 名字不合法丢名字,不丢卡
    for field in _LIST_FIELDS:
        raw_list = obj.get(field)
        if not isinstance(raw_list, list):
            continue
        clean = [str(x).strip()[:_LIST_ITEM_CAP] for x in raw_list[:_LIST_MAX_ITEMS]
                 if str(x or "").strip()]
        if clean:
            out[field] = clean
    dims = obj.get("dimensions")
    if isinstance(dims, list) and dims:
        sanitized = card_policy.sanitize_identity_card({"dimensions": dims})
        if sanitized.get("dimensions"):
            out["dimensions"] = sanitized["dimensions"]

    # 清洗后必须还剩"能构成一张卡"的内容(空 agent_name 不算内容)。
    if not any(k for k in out if not (k == "agent_name" and not out[k])):
        return None
    ok, _err = card_policy.validate_full_identity_card(
        {"agent_name": out.get("agent_name", ""), "dimensions": out.get("dimensions", [])})
    if not ok:
        return None  # sanitize 后仍非法 = 真垃圾
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --python 3.12 --with pytest python -m pytest tests/test_identity_distill_prompt.py -q`
Expected: 9 passed

- [ ] **Step 5: pyflakes + commit**

```bash
uv run --python 3.12 --with pyflakes python -m pyflakes backend/identity/distill_prompt_v1.py
git add backend/identity/distill_prompt_v1.py tests/test_identity_distill_prompt.py
git commit -m "feat(identity): shared resident distill prompt template (Batch 2 A1) — full persona fields + lenient parse"
```

---

### Task 2: genesis `floor_note` 透传(cloud 默认零变化)

**Files:**
- Modify: `backend/genesis/prompts.py:220`(`fact_write_messages`)
- Modify: `backend/genesis/worker.py`(`_fact_write` ~L696 与 `build_memory_output_from_fact_candidates` ~L1011)
- Test: `tests/test_genesis_floor_note.py`(新建)

**Interfaces:**
- Produces: `fact_write_messages(..., *, keep_all=False, floor_note: str = "")`;`build_memory_output_from_fact_candidates(..., keep_all=False, floor_note: str = "")`。Task 4 的 consumer 传 `floor_note`。
- Consumes: 现有 `FACT_WRITE_PROMPT` / `_STRICT_JSON_SUFFIX` / `FACT_WRITE_KEEP_ALL_SUFFIX`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_genesis_floor_note.py
"""Batch 2 f(days): fact_write 支持可选 floor_note 尾注;默认空 = cloud 输出逐字节等价。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from genesis import prompts


def test_default_output_unchanged():
    base = prompts.fact_write_messages([{"summary": "s"}])
    with_empty = prompts.fact_write_messages([{"summary": "s"}], floor_note="")
    assert base == with_empty  # 默认/空 note 逐字节等价 → cloud 零变化


def test_floor_note_appended_to_system():
    note = "花园只有 2 张卡,参考下限 38;真实支持的事实尽量都写;绝不编造。"
    msgs = prompts.fact_write_messages([{"summary": "s"}], floor_note=note)
    assert note in msgs[0]["content"]
    # note 在 keep_all 后、STRICT JSON 尾注前
    assert msgs[0]["content"].index(note) < msgs[0]["content"].index("JSON")


def test_floor_note_composes_with_keep_all():
    note = "参考下限 38"
    msgs = prompts.fact_write_messages([{"summary": "s"}], keep_all=True, floor_note=note)
    assert "长期档案" in msgs[0]["content"]
    assert note in msgs[0]["content"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest python -m pytest tests/test_genesis_floor_note.py -q`
Expected: FAIL `TypeError: fact_write_messages() got an unexpected keyword argument 'floor_note'`

- [ ] **Step 3: 改 prompts.py**

`fact_write_messages` 签名加 `floor_note: str = ""`,system 拼接处改为:

```python
def fact_write_messages(fact_digest: list[dict], persona_material: str = "", memory_summary: str = "", known_memories: list[str] | None = None, *, keep_all: bool = False, floor_note: str = "") -> list[dict[str, str]]:
    system = (
        FACT_WRITE_PROMPT
        + (FACT_WRITE_KEEP_ALL_SUFFIX if keep_all else "")
        + (("\n\n★ " + str(floor_note).strip()) if str(floor_note or "").strip() else "")
        + _STRICT_JSON_SUFFIX
    )
```
(函数体其余行原样保留。)

- [ ] **Step 4: 改 worker.py 两处透传**

`_fact_write`(~L696)签名加 `floor_note: str = ""`,内部调用改:
```python
messages=prompts.fact_write_messages(batch, persona_material, memory_summary, known_memories, keep_all=keep_all, floor_note=floor_note),
```
`build_memory_output_from_fact_candidates`(~L1011)签名加 `floor_note: str = ""`,把它传给内部的 `_fact_write(..., floor_note=floor_note)`。其余 `_fact_write` 调用点(前台/后台)**不改**——默认 `""`。

- [ ] **Step 5: 跑测试确认通过 + 回归**

Run: `FEEDLING_TEST_PG="postgresql://postgres:test@127.0.0.1:55432/postgres" uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest --with pytest-asyncio --with requests python -m pytest tests/test_genesis_floor_note.py tests/test_genesis_plaintext_routes.py -q`
Expected: 全 passed(genesis 现有测试零回归)

- [ ] **Step 6: pyflakes + commit**

```bash
uv run --python 3.12 --with pyflakes python -m pyflakes backend/genesis/prompts.py backend/genesis/worker.py
git add backend/genesis/prompts.py backend/genesis/worker.py tests/test_genesis_floor_note.py
git commit -m "feat(genesis): optional floor_note suffix on fact_write (default empty — cloud unchanged)"
```

---

### Task 3: consumer 身份蒸馏换共享模板 + 重试

**Files:**
- Modify: `tools/chat_resident_consumer.py`(`_resident_derive_identity` ~L8110,`_resident_existing_identity` ~L8090)
- Test: `tests/test_resident_identity_distill.py`(新建)

**Interfaces:**
- Consumes: Task 1 的 `distill_prompt_v1.build_resident_identity_prompt` / `parse_identity_payload` / `RESIDENT_IDENTITY_FIELDS`。
- Produces: `_resident_derive_identity(document, job_id) -> dict | None`(签名不变,行为升级)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_resident_identity_distill.py
"""Batch 2 A1 consumer 侧:蒸馏走共享模板、全字段、坏 JSON 重试一次、不静默。"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FEEDLING_API_URL", "http://fake.local")
os.environ.setdefault("FEEDLING_API_KEY", "test-key")
os.environ.setdefault("FEEDLING_DATA_DIR", tempfile.mkdtemp(prefix="feedling-rid-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import chat_resident_consumer as crc


GOOD = json.dumps({
    "agent_name": "小明", "self_introduction": "我是小明。", "category": "锐 · 实",
    "signature": ["有事直说", "别客套"], "tone_style": "短句、直接",
    "agent_role": "同事", "do_not_say": ["宝贝"], "boundaries": ["不聊政治"],
    "dimensions": [{"name": "直接", "value": 90, "description": "从不绕"}],
}, ensure_ascii=False)


def _patch(monkeypatch, replies):
    calls = {"prompts": []}
    def fake_call_agent(prompt, raw_text=True, trace_id=""):
        calls["prompts"].append(prompt)
        return replies[min(len(calls["prompts"]) - 1, len(replies) - 1)]
    monkeypatch.setattr(crc, "call_agent", fake_call_agent)
    monkeypatch.setattr(crc, "_capture_agent_reply_text", lambda x: x)
    monkeypatch.setattr(crc, "_resident_existing_identity", lambda: {})
    return calls


def test_derive_returns_full_persona_fields(monkeypatch):
    _patch(monkeypatch, [GOOD])
    out = crc._resident_derive_identity("人设材料", "job1")
    assert out["tone_style"] == "短句、直接"
    assert out["agent_role"] == "同事"
    assert out["do_not_say"] == ["宝贝"]
    assert out["boundaries"] == ["不聊政治"]
    assert out["signature"] == ["有事直说", "别客套"]


def test_prompt_comes_from_shared_template(monkeypatch):
    calls = _patch(monkeypatch, [GOOD])
    crc._resident_derive_identity("独特材料XYZ", "job2")
    p = calls["prompts"][0]
    assert "tone_style" in p and "do_not_say" in p and "boundaries" in p
    assert "独特材料XYZ" in p


def test_bad_json_retries_once_then_succeeds(monkeypatch):
    calls = _patch(monkeypatch, ["这不是 JSON", GOOD])
    out = crc._resident_derive_identity("材料", "job3")
    assert out is not None
    assert len(calls["prompts"]) == 2
    assert "ONLY the JSON" in calls["prompts"][1]  # 重试带纠偏提示


def test_bad_json_twice_returns_none(monkeypatch):
    calls = _patch(monkeypatch, ["垃圾", "还是垃圾"])
    assert crc._resident_derive_identity("材料", "job4") is None
    assert len(calls["prompts"]) == 2  # 只重试一次,不无限


def test_existing_identity_flows_into_merge_prompt(monkeypatch):
    calls = _patch(monkeypatch, [GOOD])
    monkeypatch.setattr(crc, "_resident_existing_identity",
                        lambda: {"agent_name": "老c", "tone_style": "锐"})
    crc._resident_derive_identity("材料", "job5")
    assert "EXISTING identity card" in calls["prompts"][0]
    assert "老c" in calls["prompts"][0]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest python -m pytest tests/test_resident_identity_distill.py -q`
Expected: FAIL(旧实现无 tone_style / 无重试 / prompt 是手抄版)

- [ ] **Step 3: 改 consumer**

`_resident_derive_identity` 整体替换为:

```python
def _resident_derive_identity(document: str, job_id: str) -> dict | None:
    """Persona/identity is small (fits one context) — a single agent derive, no chunking.
    Prompt + parse 来自共享模板 identity/distill_prompt_v1(Batch 2 A1):全量人格字段、
    card_policy 清洗、坏 JSON 重试一次(guardrail 7:报错到 setup log,不静默吞)。
    Returns a plaintext identity payload for identity.replace, or None if no persona content."""
    from identity import distill_prompt_v1 as _dp
    existing = _resident_existing_identity()
    prompt = _dp.build_resident_identity_prompt(document, existing_identity=existing or None)
    for attempt in (1, 2):
        raw = str(_capture_agent_reply_text(call_agent(prompt, raw_text=True, trace_id=job_id)) or "").strip()
        payload = _dp.parse_identity_payload(raw)
        if payload is not None:
            return payload
        log.warning("resident identity distill: unparseable output (attempt %d/2) job=%s head=%r",
                    attempt, job_id, raw[:120])
        prompt = prompt + "\nReturn ONLY the JSON object — no prose, no code fences."
    log.error("resident identity distill failed after retry job=%s — skipping identity update", job_id)
    return None
```

`_resident_existing_identity` 的字段截取行改为用共享字段集(部分补全能保住人格字段):

```python
        from identity import distill_prompt_v1 as _dp
        return {
            k: identity[k]
            for k in _dp.RESIDENT_IDENTITY_FIELDS
            if identity.get(k) not in (None, "", [], {})
        }
```

- [ ] **Step 4: 跑测试确认通过 + consumer 现有测试回归**

Run: `FEEDLING_TEST_PG="postgresql://postgres:test@127.0.0.1:55432/postgres" uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest --with pytest-asyncio --with requests python -m pytest tests/test_resident_identity_distill.py tests/test_chat_resident_self_update.py -q`
Expected: 全 passed

- [ ] **Step 5: commit**

```bash
git add tools/chat_resident_consumer.py tests/test_resident_identity_distill.py
git commit -m "feat(consumer): resident identity distill via shared template — full persona fields + retry-once (Batch 2 A1)"
```

---

### Task 4: consumer 记忆蒸馏 f(days) 引导

**Files:**
- Modify: `tools/chat_resident_consumer.py`(`_resident_extract_memories` + 新增 `_resident_floor_note`,放在 `_resident_extract_memories` 定义之前)
- Test: 追加到 `tests/test_resident_identity_distill.py`

**Interfaces:**
- Consumes: Task 2 的 `build_memory_output_from_fact_candidates(..., floor_note=...)`;现有 `_capture_get_json("/v1/bootstrap/status")`(返回含 `memory_floor` / `memories_count`,Batch 1.5 已上)。
- Produces: `_resident_floor_note() -> str`。

- [ ] **Step 1: 写失败测试(追加)**

```python
# 追加到 tests/test_resident_identity_distill.py

def test_floor_note_below_floor(monkeypatch):
    monkeypatch.setattr(crc, "_capture_get_json",
                        lambda path, **kw: {"memory_floor": 38, "memories_count": 2})
    note = crc._resident_floor_note()
    assert "2" in note and "38" in note
    assert "绝不编造" in note


def test_floor_note_empty_at_or_above_floor(monkeypatch):
    monkeypatch.setattr(crc, "_capture_get_json",
                        lambda path, **kw: {"memory_floor": 38, "memories_count": 40})
    assert crc._resident_floor_note() == ""


def test_floor_note_empty_on_error(monkeypatch):
    def boom(path, **kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(crc, "_capture_get_json", boom)
    assert crc._resident_floor_note() == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest python -m pytest tests/test_resident_identity_distill.py -k floor_note -q`
Expected: FAIL `AttributeError: ... has no attribute '_resident_floor_note'`

- [ ] **Step 3: 实现**

```python
def _resident_floor_note() -> str:
    """f(days) 蒸馏目标(机制 A,非闸门):花园低于天数下限时给 fact_write 一句
    非编造引导 —— 素材真实支持的事实尽量都写,绝不编造凑数。取不到状态返空(零影响)。"""
    try:
        st = _capture_get_json("/v1/bootstrap/status")
        floor = int(st.get("memory_floor") or 0)
        count = int(st.get("memories_count") or 0)
        if floor > 0 and count < floor:
            return (
                f"这段关系的记忆花园目前只有 {count} 张卡,按相处天数的参考下限约 {floor} 张。"
                "素材【真实支持】的持久事实尽量都写成卡,别为精简丢掉真事实;"
                "仍按 known_memories 去重;【绝不编造】凑数——宁缺毋滥。"
            )
    except Exception:
        pass
    return ""
```

`_resident_extract_memories` 里 `build_memory_output_from_fact_candidates(...)` 调用加一参:

```python
    mem_out = genesis_worker.build_memory_output_from_fact_candidates(
        user_id=uid, job_id=job_id, key_prefix=f"{job_id}:resident:write",
        runtime=runtime, fact_candidates=candidates, llm=llm, keep_all=keep_all,
        floor_note=_resident_floor_note(),
    )
```

- [ ] **Step 4: 跑测试确认通过 + commit**

Run: `uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest python -m pytest tests/test_resident_identity_distill.py -q`
Expected: 全 passed

```bash
git add tools/chat_resident_consumer.py tests/test_resident_identity_distill.py
git commit -m "feat(consumer): f(days) floor note on resident memory distill — non-fabricating guidance"
```

---

### Task 5: 全量回归 + pyflakes

- [ ] **Step 1: 全量 L1**

Run: `FEEDLING_TEST_PG="postgresql://postgres:test@127.0.0.1:55432/postgres" uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest --with pytest-asyncio --with requests python -m pytest tests/ -q -p no:cacheprovider --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
Expected: 零【新增】失败(origin/test 预存失败以干净基线对照,见下)

- [ ] **Step 2: 若有失败,在干净 origin/test 上复跑同名测试定性(预存 vs 新增)**

```bash
cd /Users/hx/Projects/io/feedling-mcp && git worktree add --detach /tmp/fv-origintest origin/test
cd /tmp/fv-origintest && FEEDLING_TEST_PG=... uv run ... python -m pytest <失败的测试> -q
cd /Users/hx/Projects/io/feedling-mcp && git worktree remove /tmp/fv-origintest --force
```

- [ ] **Step 3: pyflakes 全部改动文件,零新增告警;commit(若有收尾改动)**

---

### Task 6: 真实 test 部署 e2e(合并后,人工步骤)

触碰身份写入 ⇒ 必须真实 test 部署 e2e(加密信封铁律)。分支合 test(**直接 merge 不开 PR**)、CI bump 部署后:

- [ ] **Step 1: 确认部署镜像含本分支**(`git log origin/test --format="%h %s" | head` 看 CI bump 的镜像 tag 是否为本 merge)
- [ ] **Step 2: 用 test 环境账号(usr_8852,key 见会话)构造 sealed update_identity job**:用 `backend/content_encryption.py::build_envelope` 把一段人设材料封给(自己 pubkey + test enclave pubkey),`POST /v1/genesis/imports` body 带 `format: "sealed_v1"` + `mode: "update_identity"`(字段名以 `genesis_core._resident_sealed_import` 为准,执行时读代码)
- [ ] **Step 3: 起 consumer(最新 origin/test checkout,test env)**,等它领 job → 蒸馏 → `identity.replace`
- [ ] **Step 4: 验收(A1 标准)**:`GET /v1/identity/get`(经 enclave 解密)→ 新卡含 `tone_style/agent_role/do_not_say/boundaries/category/signature`;上传"没提维度"的部分材料 → 旧维度保留(部分补全);构造坏 JSON 场景不可行则查 consumer 日志确认 retry 路径日志存在
- [ ] **Step 5: 花园低于 floor 的账号传一份事实文档 → 落卡数明显 > 旧行为(且无编造)**;记录证据(job id / identity diff / 卡数)

---

## Self-Review(已跑)

- **Spec 覆盖**:A1 全字段(Task 1/3)✓ 共享可执行模板非 skill-parse(Task 1)✓ card_policy 三档复用(Task 1 parse + backend replace 已接)✓ 坏 JSON 重试不静默(Task 3,护栏 7)✓ f(days)(Task 2/4)✓ 真实 e2e(Task 6)✓。**不做**:cloud deriver 整条复用(spec 明确不强求)、Batch 3 的桶快照/P5、Batch 4 清 floor。
- **占位符扫描**:无 TBD/TODO;Task 6 的字段名指向执行时读 `_resident_sealed_import`(人工步骤,合理)。
- **类型一致**:`build_resident_identity_prompt(document, existing_identity=None) -> str`、`parse_identity_payload(raw) -> dict | None`、`RESIDENT_IDENTITY_FIELDS` 在 Task 1 定义、Task 3 消费,名字一致;`floor_note: str = ""` 贯穿 Task 2/4。
