> ⚠️ **已被取代** —— 见 **`IO-memory-子系统-spec与plan-定稿v1.md`**(冲突以定稿为准)。本文保留备查。

# IO Memory 统一架构 · P1 工程执行计划(施工图 · Codex 执行版)

> 2026-06-23 · 作者:Claude(CC) · 这是**施工图**,不是路线图。路线图见 `统一架构-plan.md`。
> **P1 = 统一 action 协议(schema + conformance)+ route A 接 IO 长期 recall 注入 + 顺序/token 预算 + flag + 测试。** 不碰 propose / 捕获标准(P2)。

---

## 0. 现状锚点(已核对代码,直接用)

| 用途 | 位置 / 形状 |
|---|---|
| 写入 commit(唯一)| `backend/memory/actions.py:_execute_memory_action(s)`;HTTP `POST /v1/memory/actions`(`memory/routes.py:177`)|
| readside core(M2 已抽出,**复用它**)| `backend/memory_readside_core.py`:`memory_index_core` / `memory_fetch_core` / `post_enclave_readside` |
| 召回接口(已存在,route A 直接调)| `POST /v1/memory/index`、`POST /v1/memory/fetch`(`memory/routes.py`)|
| route B coerce | `backend/hosted_runtime.py:240 coerce_runtime_action` |
| route A coerce(自有)| `tools/chat_resident_consumer.py:2272 _normalize_v2_action_type` / `:2640 _proactive_action_type` / `:2011 execute_memory_actions` |
| route A 取数(注入点附近)| `:482 get_decrypted_history`、`:617 _screen_context_for_message`、`:110 build_agent_context_v2`(组装给 agent)、`:1953 _resident_run_agent_v2` |
| flag 约定 | `os.environ.get("FEEDLING_…", default)`,假值 `{"0","false","off","no"}`(参考 `turn.py:928 FEEDLING_MODEL_API_MEMORY_CAPTURE`)|
| 测试目录 | `tests/`(已有 `test_chat_resident_consumer.py`、`test_memory_readside*.py`、`test_hosted_memory_tools.py`)|

### 接口 request/response(route A 要调的)
```
POST /v1/memory/index
  req : {"query": "<用户消息或检索意图>", "limit": 50, "include_sensitive": false}
  resp: {"items": [{"id","summary","bucket_refs","status","salience","is_open_thread","is_sensitive","score"}], ...}
  # ⚠️ Codex 跑一次确认:返回是否已含 selection/suggested_ids;若无,consumer 端按 score 取 topK

POST /v1/memory/fetch
  req : {"ids": ["mem_x","mem_y"]}
  resp: {"items": [{...完整卡:summary/verbatim/context/follow_up/...}], "missing_ids":[], "unavailable_ids":[]}
```

---

## 1. T1 · 统一 action schema + conformance 测试(先写测试)

**先写测试**:`tests/test_memory_action_conformance.py`
- 准备一组样例 agent 输出(覆盖 `memory.create / patch / supersede / delete`,含字段缺失/别名)。
- **只测"规整/canonical normalize"这一纯函数**:① route B `coerce_runtime_action`;② route A `_normalize_v2_action_type`(的 normalize 部分)。
- **断言两边产出的 canonical 动作一致**(type、target、payload 字段),尤其 `memory.supersede` 两边都支持。
- ⚠️ **conformance 不真走 `POST /v1/memory/actions` / `execute_memory_actions`**——HTTP/executor 只在**集成测试或 smoke 里 mock**,不在 conformance 里跑。(Codex review-2)

**再实现**:
- 定 **canonical action schema**(source of truth):字段口径 + 校验。放 `backend/memory/action_schema.py`(新建)。
- 让两边的 coerce 都以它为准(**route A 不 import 后端函数**,只对齐 schema + 过测试)。
- 修正漂移点(若 route A 缺 supersede 等,补齐到 schema)。

**交付**:`action_schema.py` + `test_memory_action_conformance.py`(绿)。

---

## 2. T2 · route A 接 IO 长期 recall 注入(P1 核心)

**决策(钉死,不再"Codex 定")**:
- **P1 不新增 `/v1/context/assemble` 端点**。route A consumer **直接调已存在的 `/v1/memory/index` + `/v1/memory/fetch`**(它本来就调 enclave HTTP)。
- 完整"后端拼装器端点"**推迟**到 route B 也迁移时(P1.5/P2),避免 P1 引入新后端面 + 多一个 zhihao 门。
- **服务端注入,不起 LLM、不做 tool-loop**。

**flag(zhihao 确认后收窄)**:
- `FEEDLING_ROUTE_A_MEMORY_RECALL` — **全局总开关,默认 off(="0")**。off 时 consumer 逐字节回到现状。
- **flag 读在 consumer 侧(每个 VPS 部署各自的 env)**——所以它**本身就是 per-deployment 灰度**:哪个 consumer 设了就对哪个用户生效。**这正是不需要 server 端 per-user allowlist 的原因**(allowlist 是错层 + 冗余)。
- **取消 per-user allowlist 灰度**。zhihao 已确认不加 `FEEDLING_ROUTE_A_MEMORY_RECALL_USERS`;test 环境整体打开验证,prod 各 consumer 部署自行按 env 决策。

**先写测试**:扩 `tests/test_chat_resident_consumer.py`(或新 `tests/test_route_a_memory_recall.py`)
- flag off → 组装结果与现状一致(不调 index/fetch)。
- flag on + mock index/fetch → 注入块出现在发给 agent 的 context 里;ids 去重;空召回时不注入。

**再实现**(`tools/chat_resident_consumer.py`):
1. 新增 `_memory_recall_for_message(content: str) -> tuple[str, list[str]]`(放在 `_screen_context_for_message` ~617 旁边):
   - flag off → 返回空。
   - flag on → `POST /v1/memory/index {query: content, limit: <预算内>}` → 取 topK ids → `POST /v1/memory/fetch {ids}` → 拼成"长期记忆"注入块(纯文本)。
2. 在一轮组装处(`get_decrypted_history` + `_screen_context_for_message` 汇合、喂 `build_agent_context_v2` 那段)**把召回块加进 context**。
3. 注入块格式(短、明确来源):
   ```
   [IO 长期记忆 · 供参考,用户当前说法优先]
   - 武松是橘猫(名字来自武松打虎)
   - …(最多 K 条)
   ```

**交付**:`_memory_recall_for_message` + 接入 + 测试(绿)。

---

## 3. T3 · 短期/长期 顺序 + token 预算(共享配置,防 A/B 再分叉)

**做**:
- 定一份**共享布局契约**:段落顺序 + 每段 token cap + 长期召回条数 K。
  - 建议顺序:`identity → 长期记忆卡 → 短期(recent chat / 当前屏幕 / GPS)→ pending`。
  - 建议预算(初值,可配,Codex/zhihao 调):长期 ≤ K=5 条 / ≤ X tokens;短期 recent ≤ Y;screen ≤ Z。
- **route B** 后端可放 `backend/memory/context_layout.py` 常量。
- ⚠️ **route A consumer 不 import 后端 `context_layout.py`**(独立脚本,跨进程/部署 import 会脆,Codex review-2)。改成:**共享契约文件(如 JSON/常量)+ consumer 侧复制常量,用 conformance 测试校验两边一致**(同 §1 coerce 的处理)。

**交付**:共享布局契约 + route B 常量 + route A 复制值 + conformance 校验。

---

## 4. 测试清单(TDD,先红后绿)

| 测试 | 文件 | 覆盖 |
|---|---|---|
| 协议一致 | `test_memory_action_conformance.py`(新)| A/B 对同输入产同 canonical 动作;supersede 两边通;旧动作不回归 |
| route A 召回 | `test_route_a_memory_recall.py`(新)/ 扩 `test_chat_resident_consumer.py` | flag off 不变;flag on 注入;去重;空召回不注入;token 不超预算 |
| 布局预算 | 上面任一 | 顺序正确;各段 cap 生效 |

---

## 5. 如何验证

**本地**:
```
pytest tests/test_memory_action_conformance.py tests/test_route_a_memory_recall.py -q
# flag off 回归:确认 consumer 组装与现状逐字节一致
```

**test 环境**:
1. 部署,**flag 默认 off** → 确认 route A 行为无变化(回归)。
2. **总开关 on**(本轮取消 per-user allowlist,按环境开关验证):
   - smoke ①(命中):账号里有"武松是橘猫"卡 → 清空近期聊天 → route A 问"武松是什么猫" → 答得出。
   - smoke ②(不命中):问一个库里没有的 → **不瞎编**。
   - 看 trace / prompt:长期记忆块已注入、token 在预算内。
   - 确认 route A 现有 identity/memory 写动作仍正常(不回归)。

---

## 6. P1 验收 gate(硬)

- ✅ flag off → route A 逐字节回旧逻辑。
- ✅ flag on → 能召回 IO 长期记忆卡并注入。
- ✅ prompt token 不超预算。
- ✅ route A 现有 identity/memory action 不回归(过 conformance + 旧流程)。
- ✅ ≥2 真实 smoke(命中 / 不命中)。
- ✅ A/B 走同一 action schema(过 conformance)。
- 护栏 **G1**(回归 route A 现有流程)、**G2**(行为变化 → 冒烟 + 盯 token)。

---

## 7. zhihao 已确认的后端边界

1. **鉴权路径**:route A consumer 用用户 api_key 调 `/v1/memory/index|fetch` OK,按现有 `/v1/chat/history` 的方式处理。
2. **context assembler**:P1 先不加 `/v1/context/assemble`,route A 直接用 index/fetch。
3. **token 预算**:zhihao 暂不指定固定值,实现时先给保守默认值,调试过程再定。
4. **灰度策略**:取消 per-user allowlist 灰度;保留全局 env flag,避免上线还要逐个用户开。

---

## 8. P1 明确不做

❌ 统一 propose / 改捕获标准(P2)· ❌ tool-loop · ❌ `/v1/context/assemble` 新端点 · ❌ 感知/GPS 进长期 · ❌ MemPalace · ❌ merge/decay · ❌ 不动 commit、M2 卡结构、不走 MCP。

---

## 9. 执行顺序

```
T1(schema + conformance 测试,先红后绿)
 → T3 context_layout(共享布局/预算,T2 要用)
 → T2(route A 召回注入,flag 包住,先写测试)
 → 跑 §4 测试 + §5 本地/test 验证 + §6 gate
 (§7 后端边界已由 zhihao 确认)
```

## 10. 一句话

> **P1 施工:① 定 action schema + 两边过同一组 conformance 测试(不 import);② route A consumer 新增 `_memory_recall_for_message`,直接调现有 `/v1/memory/index|fetch` 注入长期记忆,`FEEDLING_ROUTE_A_MEMORY_RECALL` 默认 off;③ 共享 `context_layout` 定顺序+token 预算,A/B 对齐。先写测试,flag off 必须逐字节回旧。zhihao 已确认鉴权同 `/chat/history`,P1 不加 context assemble,token 调试中定,取消 per-user 灰度。**
