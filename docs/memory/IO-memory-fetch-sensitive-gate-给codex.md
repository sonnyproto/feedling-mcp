# 补 fetch sensitive gate + tests(给 Codex 执行)

> 2026-06-24 · 作者:CC · 状态:**待 Codex review → 执行**
> 来源:读写合同 `IO-memory-read-write-contract.md` §5.1 必办项 / §4 ⚠️ 行。
> 性质:**小范围行为变化**——堵住"按 id 直 `fetch` 能绕过敏感过滤"的洞。补完把合同 §4 那行从 ⚠️ 改成已强制、§2 R4 从"靠自律"改成"服务端强制"。

---

## 0. 一句话

`index` 在 enclave 解密后会丢掉敏感卡,**`fetch` 没有这一步** → agent 拿到一个敏感 `id` 直接 `fetch` 仍能取到正文。**给 `fetch` 补上和 `index` 一样的敏感过滤。**

---

## 1. 根因(已核代码)

- enclave `v1_memory_index`(`backend/enclave_app.py:~1101`)解密后过滤:
  ```python
  if not bool(payload.get("include_sensitive", False)):
      items = [item for item in items if not item.get("is_sensitive")]
  ```
- enclave `v1_memory_fetch`(`backend/enclave_app.py:~1131`)**没有这段**,直接返回所有解密 items。
- `_build_memory_fetch_item` 同样经 `_memory_readside_is_sensitive(envelope, inner)` 算了 `is_sensitive`(`enclave_app.py:1007/1044`),**所以判定数据已经有了,只差过滤动作**。
- backend `memory_fetch_core`(`backend/memory_readside_core.py:235`)给 enclave 的 payload 是 `{"ids", "limit"}`,**没传 `include_sensitive`**。

> 注:`index` 的过滤在 **enclave 解密后**做,能盖住"明文 `is_sensitive` + 密文内层 `sensitivity_class/sensitive_scope`"两种敏感(见 `_memory_readside_is_sensitive`)。fetch 照抄即得同等覆盖,**不要只在 backend 用明文 `is_sensitive` 过滤**(那会漏掉内层敏感)。

---

## 2. 改动(3 处 + 测试)

### C-1 enclave `v1_memory_fetch` 加过滤(权威 gate)
`backend/enclave_app.py` 的 `v1_memory_fetch`,在 `items` 构建后、return 前,**照抄 index 的过滤**;被丢的 id 收集出来回传:
```python
items, unavailable_ids = _memory_readside_decrypt_items(..., item_builder=_build_memory_fetch_item)
blocked_sensitive_ids = []
if not bool(payload.get("include_sensitive", False)):
    kept = []
    for item in items:
        if item.get("is_sensitive"):
            blocked_sensitive_ids.append(item.get("id"))
        else:
            kept.append(item)
    items = kept
return jsonify({
    "user_id": authorized_user_id,
    "items": items,
    "unavailable_ids": unavailable_ids,
    "blocked_sensitive_ids": blocked_sensitive_ids,   # 新增,便于观测/测试
})
```

### C-2 backend `memory_fetch_core` 透传 `include_sensitive` + surface blocked
`backend/memory_readside_core.py:memory_fetch_core`:
- 从 `payload` 读 `include_sensitive`(默认 `False`),放进给 enclave 的 payload:
  ```python
  payload_to_enclave = {"ids": [...], "limit": limit,
                        "include_sensitive": _bool_payload(payload.get("include_sensitive"))}
  ```
- 把 enclave 回的 `blocked_sensitive_ids` 透到返回里(新增字段,或并进 `unavailable_ids` 也行,但**建议独立字段**便于区分原因)。

### C-3 调用方确认(无需改逻辑,确认默认安全)
- `/v1/memory/recall`:已 `include_sensitive` 默认 false → 经 index 已不含敏感,selector 选不到敏感 id → fetch 自然安全。**本改动让"即使 selector 拿到敏感 id 也会在 fetch 被拦"成立。**
- `/v1/memory/fetch` 路由(`backend/memory/routes.py`)已直接转发 payload → 自动带上 `include_sensitive`,无需改。

---

## 3. 测试(必须新增)

| 测试 | 断言 |
|---|---|
| enclave fetch 过滤 | `v1_memory_fetch` 对一张 `is_sensitive=true` 的卡:`include_sensitive` 缺省 → 不在 `items`、在 `blocked_sensitive_ids`;`include_sensitive=true` → 正常返回 |
| 内层敏感也拦 | 卡明文 `is_sensitive` 不设、但密文内层 `sensitivity_class` 敏感 → 缺省同样被拦(验证是在解密后过滤) |
| backend 透传 | `memory_fetch_core` 默认把敏感 id 过滤掉并 surface 到 blocked;传 `include_sensitive=true` 时返回 |
| **直取绕过被堵(核心回归)** | 直接 `POST /v1/memory/fetch {"ids":[敏感id]}`(不带 include_sensitive)→ 正文**取不到** |
| recall 保持干净 | `/v1/memory/recall` 默认不返回敏感卡(回归) |

放进 `tests/test_memory_readside.py` / `test_memory_readside_core.py` / `test_enclave_routeb_readside.py` 既有体系。

---

## 4. 行为变化 & 护栏

- **这是收紧型行为变化**:之前能按 id 直取敏感,现在默认取不到。
- **开工前 pre-check**:确认**没有内部调用方依赖"不带 include_sensitive 也能 fetch 敏感卡"**(route B 走 index 已 gated;iOS Garden 走 `/v1/memory/list` 非 fetch;agent 直 fetch 是主要受影响面 —— 正是要堵的)。若发现有合法依赖,再决定要不要 flag;**默认倾向直接收紧,不加 flag**(这是安全修复)。
- 不影响 `include_sensitive=true` 的显式取用(R4:仅用户显式请求该敏感主题时)。

---

## 5. 验收

1. 新增测试全过;现有 readside / recall 测试不回归。
2. 直取敏感 id(无 flag)取不到正文;带 flag 能取。
3. 改完更新合同:`IO-memory-read-write-contract.md` §4 敏感行 ⚠️→已强制、§2 R4 去掉"靠自律"、§5.1 标完成。

---

## 6. 给 Codex 的确认点

1. `blocked_sensitive_ids` 用独立字段 vs 并进 `unavailable_ids`——倾向独立,你看实现成本。
2. backend 要不要再加一层"明文 `is_sensitive` 提前过滤"做 defense-in-depth?(enclave 那层是权威且覆盖内层;backend 明文层只是省一次往返,可选)
3. pre-check 有没有发现依赖未 gated fetch 的合法调用方?
