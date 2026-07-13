# VPS Onboarding Batch 0 — Identity Card Policy 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建一个纯 Python 的 `card_policy.py` 作为"合格身份卡"的单一判定来源,io_cli 与 backend 共用,消除规则漂移;并把 init / replace / profile_patch / dimension_nudge 四条写入路接上它。

**Architecture:** `card_policy.py` 只依赖 stdlib(io_cli 在 VPS 独立跑,不能拖后端 DB 依赖),对外暴露 `validate_full_identity_card`(init/全量 replace)、`validate_profile_patch`(局部改)、`validate_dimension_nudge`(单维)。它同时成为 `RUNTIME_LABELS` 的单一来源,`backend/identity/service.py` 改为从它 import。

**Tech Stack:** Python 3.10+,pytest。测试从 `backend/` 顶层 import(见 `tests/conftest.py`,`sys.path.insert(0, backend)`)。

## Global Constraints

- **契约 = B(证据优先、稀疏允许):policy 只校验【结构】。** 绝不要求"恰好 7 维",绝不因方差/聚集/稀疏拒卡——那些是 prompt 引导的质量项,不是门。**优先 onboarding 成功率。**
- 只校验:`agent_name` 非空且非 runtime label;`dimensions` 是 list、每项 `{name:非空 str, value:0-100 数字, description}`、维度名不重复、数量 ≤ 12(sanity cap,非 floor)。
- 校验函数签名统一:`-> tuple[bool, str]`,成功 `(True, "")`,失败 `(False, "<error_code>")`。
- `card_policy.py` **只 import stdlib**,不得 import `db` / `core.store` / 任何后端重模块。
- 现有行为不回归:repoint `RUNTIME_LABELS` 后,`agent_name_too_generic` 等既有校验结果不变。

---

### Task 1: `card_policy.py` — 结构校验基石

**Files:**
- Create: `backend/identity/card_policy.py`
- Test: `tests/test_identity_card_policy.py`

**Interfaces:**
- Produces:
  - `RUNTIME_LABELS: frozenset[str]`(小写标签集)
  - `is_runtime_label(name: str) -> bool`
  - `validate_dimensions_structure(dims) -> tuple[bool, str]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_identity_card_policy.py
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from identity import card_policy  # noqa: E402


def test_is_runtime_label_matches_known_and_ignores_case():
    assert card_policy.is_runtime_label("Claude") is True
    assert card_policy.is_runtime_label(" hermes ") is True
    assert card_policy.is_runtime_label("阿锐") is False
    assert card_policy.is_runtime_label("") is False


def test_dimensions_structure_accepts_sparse_and_clustered():
    # 契约 B:2 维稀疏、全部聚集在高位,都是合法结构
    sparse = [{"name": "锐利", "value": 90, "description": "x"},
              {"name": "直接", "value": 88, "description": "y"}]
    assert card_policy.validate_dimensions_structure(sparse) == (True, "")
    clustered = [{"name": f"d{i}", "value": 85, "description": "z"} for i in range(7)]
    assert card_policy.validate_dimensions_structure(clustered) == (True, "")


def test_dimensions_structure_rejects_bad_shape():
    assert card_policy.validate_dimensions_structure("nope")[0] is False
    assert card_policy.validate_dimensions_structure(
        [{"name": "", "value": 50, "description": "x"}]) == (False, "dimension_name_empty")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": 150, "description": "x"}]) == (False, "dimension_value_out_of_range")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": "hi", "description": "x"}]) == (False, "dimension_value_not_number")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": 50, "description": "x"},
         {"name": "A", "value": 60, "description": "y"}]) == (False, "dimension_name_duplicate")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": True, "description": "x"}]) == (False, "dimension_value_not_number")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify && python -m pytest tests/test_identity_card_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'identity.card_policy'`

- [ ] **Step 3: 写实现**

```python
# backend/identity/card_policy.py
"""Single source of truth for what a valid IO identity card must satisfy.

Imported by BOTH the backend write paths (init / replace / profile_patch /
dimension_nudge) AND io_cli's local pre-validation, so the two never drift.
Therefore this module MUST stay pure stdlib — io_cli runs standalone on a VPS
and cannot pull backend DB deps.

Contract = B (evidence-first, sparse-allowed): we validate STRUCTURE only.
We do NOT require exactly 7 dimensions and we do NOT reject clustered /
low-spread / sparse cards — those are quality nudges owned by the prompt, not
gates. Blocking on them would hurt onboarding success rate.
"""
from __future__ import annotations

# Single source of truth. backend/identity/service.py imports this.
RUNTIME_LABELS: frozenset[str] = frozenset({
    "hermes", "claude", "claude code", "claude desktop", "claude-code",
    "claude-desktop", "claude.ai", "anthropic", "openclaw", "open-claw",
    "open claw", "cursor", "chatgpt", "chat-gpt", "gpt", "gpt-4", "gpt-4o",
    "gpt-5", "openai", "openrouter", "gemini", "assistant", "ai", "bot",
})

MAX_DIMENSIONS = 12  # sanity cap, NOT a floor
_VALUE_MIN, _VALUE_MAX = 0, 100
_OK: tuple[bool, str] = (True, "")


def is_runtime_label(name: str) -> bool:
    return str(name or "").strip().lower() in RUNTIME_LABELS


def validate_dimensions_structure(dims) -> tuple[bool, str]:
    if not isinstance(dims, list):
        return (False, "dimensions_must_be_list")
    if len(dims) > MAX_DIMENSIONS:
        return (False, "too_many_dimensions")
    seen: set[str] = set()
    for d in dims:
        if not isinstance(d, dict):
            return (False, "dimension_must_be_object")
        name = str(d.get("name") or "").strip()
        if not name:
            return (False, "dimension_name_empty")
        key = name.lower()
        if key in seen:
            return (False, "dimension_name_duplicate")
        seen.add(key)
        value = d.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return (False, "dimension_value_not_number")
        if value < _VALUE_MIN or value > _VALUE_MAX:
            return (False, "dimension_value_out_of_range")
    return _OK
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify && python -m pytest tests/test_identity_card_policy.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: 提交**

```bash
cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify
git add backend/identity/card_policy.py tests/test_identity_card_policy.py
git commit -m "feat(identity): card_policy 结构校验基石(契约 B,纯 stdlib)"
```

---

### Task 2: 三档卡校验(full / patch / nudge)

**Files:**
- Modify: `backend/identity/card_policy.py`
- Test: `tests/test_identity_card_policy.py`

**Interfaces:**
- Consumes: `is_runtime_label`, `validate_dimensions_structure`(Task 1)
- Produces:
  - `validate_full_identity_card(card: dict) -> tuple[bool, str]`
  - `validate_profile_patch(patch: dict) -> tuple[bool, str]`
  - `validate_dimension_nudge(target_name: str, new_value) -> tuple[bool, str]`

- [ ] **Step 1: 写失败测试(追加到同一测试文件)**

```python
def test_full_card_structure_only_lenient():
    ok_card = {"agent_name": "阿锐", "self_introduction": "hi",
               "dimensions": [{"name": "锐利", "value": 90, "description": "x"}]}
    assert card_policy.validate_full_identity_card(ok_card) == (True, "")
    # 稀疏(1 维)在契约 B 下合法
    assert card_policy.validate_full_identity_card(
        {"agent_name": "阿锐", "dimensions": []}) == (True, "")
    assert card_policy.validate_full_identity_card(
        {"agent_name": "", "dimensions": []}) == (False, "agent_name_empty")
    assert card_policy.validate_full_identity_card(
        {"agent_name": "Claude", "dimensions": []}) == (False, "agent_name_is_runtime_label")


def test_profile_patch_only_checks_present_fields():
    # 只改名字:旧卡维度稀疏也不该因此被拒
    assert card_policy.validate_profile_patch({"agent_name": "阿锐"}) == (True, "")
    assert card_policy.validate_profile_patch({"tone_style": "sharp"}) == (True, "")
    assert card_policy.validate_profile_patch({"agent_name": "gpt"}) == (False, "agent_name_is_runtime_label")
    assert card_policy.validate_profile_patch(
        {"dimensions": [{"name": "a", "value": 150, "description": "x"}]}) == (False, "dimension_value_out_of_range")


def test_dimension_nudge_range_only():
    assert card_policy.validate_dimension_nudge("锐利", 70) == (True, "")
    assert card_policy.validate_dimension_nudge("锐利", 150) == (False, "dimension_value_out_of_range")
    assert card_policy.validate_dimension_nudge("", 50) == (False, "dimension_name_empty")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify && python -m pytest tests/test_identity_card_policy.py -k "full_card or profile_patch or nudge" -v`
Expected: FAIL — `AttributeError: module 'identity.card_policy' has no attribute 'validate_full_identity_card'`

- [ ] **Step 3: 写实现(追加到 card_policy.py)**

```python
def validate_full_identity_card(card: dict) -> tuple[bool, str]:
    """init / full replace. Structure only (contract B — no count/spread floor)."""
    if not isinstance(card, dict):
        return (False, "identity_must_be_object")
    name = str(card.get("agent_name") or "").strip()
    if not name:
        return (False, "agent_name_empty")
    if is_runtime_label(name):
        return (False, "agent_name_is_runtime_label")
    return validate_dimensions_structure(card.get("dimensions", []))


def validate_profile_patch(patch: dict) -> tuple[bool, str]:
    """Only validate fields PRESENT in the patch — never judge the whole card,
    so a name change is not rejected because the old card is sparse."""
    if not isinstance(patch, dict):
        return (False, "patch_must_be_object")
    if "agent_name" in patch:
        name = str(patch.get("agent_name") or "").strip()
        if not name:
            return (False, "agent_name_empty")
        if is_runtime_label(name):
            return (False, "agent_name_is_runtime_label")
    if "dimensions" in patch:
        return validate_dimensions_structure(patch.get("dimensions"))
    return _OK


def validate_dimension_nudge(target_name: str, new_value) -> tuple[bool, str]:
    if not str(target_name or "").strip():
        return (False, "dimension_name_empty")
    if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
        return (False, "dimension_value_not_number")
    if new_value < _VALUE_MIN or new_value > _VALUE_MAX:
        return (False, "dimension_value_out_of_range")
    return _OK
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify && python -m pytest tests/test_identity_card_policy.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: 提交**

```bash
cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify
git add backend/identity/card_policy.py tests/test_identity_card_policy.py
git commit -m "feat(identity): card_policy 三档校验 full/patch/nudge"
```

---

### Task 3: `RUNTIME_LABELS` 收敛为单一来源

**Files:**
- Modify: `backend/identity/service.py:140`(把内联集合改成从 card_policy import)
- Test: `tests/test_identity_card_policy.py`

**Interfaces:**
- Consumes: `card_policy.RUNTIME_LABELS`(Task 1)

- [ ] **Step 1: 写回归测试(锁住"repoint 后行为不变")**

```python
def test_service_runtime_labels_are_card_policy_source():
    from identity import service as identity_service
    assert identity_service._IDENTITY_RUNTIME_LABELS is card_policy.RUNTIME_LABELS
    # 既有判定不回归
    assert "claude" in identity_service._IDENTITY_RUNTIME_LABELS
    assert "hermes" in identity_service._IDENTITY_RUNTIME_LABELS
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify && python -m pytest tests/test_identity_card_policy.py -k runtime_labels_are_card_policy -v`
Expected: FAIL — `assert <inline set> is <card_policy.RUNTIME_LABELS>`(二者不是同一对象)

- [ ] **Step 3: 改 service.py**

在 `backend/identity/service.py` 顶部 import 区加(与其它 `from identity import ...` 同风格):

```python
from identity.card_policy import RUNTIME_LABELS as _IDENTITY_RUNTIME_LABELS
```

并**删除** `service.py:140` 起原来内联定义的 `_IDENTITY_RUNTIME_LABELS = { ... }` 整块。

- [ ] **Step 4: 跑测试确认通过 + 既有身份测试不回归**

Run: `cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify && python -m pytest tests/test_identity_card_policy.py tests/test_identity_actions.py -v`
Expected: PASS(card_policy 全绿 + test_identity_actions 原有用例不回归)

- [ ] **Step 5: 提交**

```bash
cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify
git add backend/identity/service.py tests/test_identity_card_policy.py
git commit -m "refactor(identity): RUNTIME_LABELS 收敛到 card_policy 单一来源"
```

---

### Task 4: init / 全量 replace 接上 `validate_full_identity_card`

**Files:**
- Modify: `backend/identity/identity_core.py`(`init_identity` L62 附近的 `identity_plain` 分支;`replace_identity` L179)
- Test: `tests/test_identity_replace_action.py`(追加)或新增 `tests/test_identity_policy_wiring.py`

**Interfaces:**
- Consumes: `card_policy.validate_full_identity_card`(Task 2)

- [ ] **Step 1: 写失败测试**

在现有身份写入测试文件里(参照 `tests/test_identity_init_server_encrypt.py` 的账号/store fixture 用法),加:

```python
def test_init_rejects_runtime_label_name(client_with_fresh_account):
    # client_with_fresh_account:复用现有 fixture(见 test_identity_init_server_encrypt.py)
    body = {
        "identity": {"agent_name": "Claude",
                     "dimensions": [{"name": "锐利", "value": 90, "description": "x"}]},
        "days_with_user": 3,
        "relationship_anchor_evidence": "chat log 2026-01-01",
    }
    resp = client_with_fresh_account.post("/v1/identity/init", json=body)
    assert resp.status_code == 400
    assert resp.json()["error"] == "agent_name_is_runtime_label"


def test_init_accepts_sparse_two_dimensions(client_with_fresh_account):
    body = {
        "identity": {"agent_name": "阿锐",
                     "dimensions": [{"name": "锐利", "value": 90, "description": "x"},
                                    {"name": "直接", "value": 88, "description": "y"}]},
        "days_with_user": 3,
        "relationship_anchor_evidence": "chat log 2026-01-01",
    }
    resp = client_with_fresh_account.post("/v1/identity/init", json=body)
    assert resp.status_code == 201  # 契约 B:稀疏合法
```

> 注:若无 `client_with_fresh_account` fixture,按 `tests/test_identity_init_server_encrypt.py` 现有构造账号 + 调用方式复制过来;不要臆造 fixture 名。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify && python -m pytest tests/test_identity_policy_wiring.py -v`
Expected: FAIL — init 目前不校验 runtime label,`test_init_rejects_runtime_label_name` 期望 400 实得 201

- [ ] **Step 3: 接入校验**

在 `backend/identity/identity_core.py::init_identity` 的 `if identity_plain is not None:` 分支内、构建 envelope **之前**加:

```python
        from identity import card_policy
        ok, err = card_policy.validate_full_identity_card(identity_plain)
        if not ok:
            return {"error": err}, 400
```

在 `replace_identity` 里,取到明文 `identity` dict(全量)后、写入前,同样加:

```python
        from identity import card_policy
        ok, err = card_policy.validate_full_identity_card(identity_plain)
        if not ok:
            return {"error": err}, 400
```

(`identity_plain` 用该函数内实际的明文卡变量名;若 replace 走 envelope 分支拿不到明文,则仅在有明文入参时校验——不改变 envelope 直传路径。)

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify && python -m pytest tests/test_identity_policy_wiring.py tests/test_identity_init_server_encrypt.py tests/test_identity_replace_action.py -v`
Expected: PASS(新用例过 + 原有 init/replace 用例不回归)

- [ ] **Step 5: 提交**

```bash
cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify
git add backend/identity/identity_core.py tests/test_identity_policy_wiring.py
git commit -m "feat(identity): init/replace 接 card_policy 全量校验(契约 B)"
```

---

### Task 5: profile_patch / dimension_nudge 接上分档校验

**Files:**
- Modify: `backend/identity/actions.py`(`_identity_profile_patch` L107;`_identity_dimension_nudge` L239)
- Test: `tests/test_identity_actions.py`(追加)

**Interfaces:**
- Consumes: `card_policy.validate_profile_patch`, `card_policy.validate_dimension_nudge`(Task 2)

- [ ] **Step 1: 写失败测试**

参照 `tests/test_identity_actions.py` 现有 profile_patch / nudge 用例风格,加:

```python
def test_profile_patch_rename_allowed_on_sparse_card(fresh_identity_store):
    # 旧卡只有 2 维,仅改名字应放行(不因整卡形状被拒)
    # fresh_identity_store:复用 test_identity_actions.py 现有 fixture
    out, changes, status = _run_action(fresh_identity_store,
        {"type": "identity.profile_patch", "patch": {"agent_name": "阿锐"}})
    assert status == 200


def test_profile_patch_rejects_runtime_label(fresh_identity_store):
    out, changes, status = _run_action(fresh_identity_store,
        {"type": "identity.profile_patch", "patch": {"agent_name": "hermes"}})
    assert status == 400
    assert out["error"] == "agent_name_is_runtime_label"
```

> `_run_action` / `fresh_identity_store` 用该测试文件已有的辅助;不要臆造新 helper。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify && python -m pytest tests/test_identity_actions.py -k "profile_patch_rename or profile_patch_rejects_runtime" -v`
Expected: FAIL(patch 未走 card_policy;runtime label 目前可能已被旧 `agent_name_too_generic` 拦——若已拦,改断言为对齐新错误码 `agent_name_is_runtime_label`,并在实现里统一)

- [ ] **Step 3: 接入校验**

`backend/identity/actions.py::_identity_profile_patch` 内,取到 `patch` dict 后、落库前加:

```python
    from identity import card_policy
    ok, err = card_policy.validate_profile_patch(patch)
    if not ok:
        return {"status": "error", "error": err, "action": "identity.profile_patch"}, [], 400
```

`_identity_dimension_nudge` 内,算出 `new_value`(L271 `max(0, min(100, ...))` 前后)与目标维度名后加:

```python
    from identity import card_policy
    ok, err = card_policy.validate_dimension_nudge(target_name, new_value)
    if not ok:
        return {"status": "error", "error": err, "action": "identity.dimension_nudge"}, [], 400
```

(`target_name` 用该函数内实际的目标维度名变量。)

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify && python -m pytest tests/test_identity_actions.py -v`
Expected: PASS(新用例过 + 原有 patch/nudge 用例不回归)

- [ ] **Step 5: 提交**

```bash
cd /Users/hx/Projects/io/feedling-mcp-onboarding-unify
git add backend/identity/actions.py tests/test_identity_actions.py
git commit -m "feat(identity): profile_patch/nudge 接 card_policy 分档校验"
```

---

## Self-Review 结论

- **Spec 覆盖**:本计划实现 spec §2 "Batch 0 — card_policy(lenient/B)+ 三档校验 + io_cli/backend 共用单一来源"。io_cli 侧的 import 使用在 **Batch 1**(`identity-init --fresh-start` verb 里 call `validate_full_identity_card` 本地预校验)。
- **契约 B 落实**:Task 1/2/4 明确测了"稀疏 2 维合法""聚集合法"——不因数量/形状拒卡,只拦结构垃圾 + runtime label 名字。
- **无占位符**:每步有真实测试/实现代码与命令。少数 fixture 名(`client_with_fresh_account`/`fresh_identity_store`/`_run_action`)标注为"复用现有测试文件的既有辅助,不臆造"——执行者需先读对应测试文件确认真实名称。
- **类型一致**:所有校验函数统一 `-> tuple[bool, str]`;错误码在 Task 1/2 定义、Task 4/5 复用同名。

## 后续批次(各自单独出计划)

- Batch 1:io_cli onboarding verbs(`identity-init --fresh-start` 本地调 card_policy)+ `onboard`/`onboard start`/`doctor` 护栏 + skill 指向。
- Batch 2:VPS 身份蒸馏 source adapter + 共享 policy + 补人格字段 + 共享 prompt 模板;真实 e2e。
- Batch 3:记忆读桶快照 + known_memories 去重 + P5 并发基线(revision)。
- Batch 4:清所有旧 floor(validate + memory/verify + gates + docs)+ skill promote gate。
