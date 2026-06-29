# IO Memory v1 · 给 zhihao 的交付(工具契约 + 调用流程 + 提示词)

> 2026-06-25 · 作者:CC · 给 zhihao 在新 agent-runtime 里"用记忆"
> 这份 = **你(zhihao)消费记忆需要的一切**(工具 / 流程 / 提示词);**不是"后端怎么建"**(那是 `IO-memory-v1实施计划-test基线.md`)。结构以 `IO-memory-v1结构定稿-bucket-thread.md` 为准。
> **Codex 补充 2026-06-25**:本文已按 zhihao 的 `Agent Runtime 计划:Claude Agent SDK / Codex` 对齐:后续 hosted API 用户和 VPS/resident 用户都应逐步走同一套 `agent-runner / consumer / tool gateway`。因此本文里的 memory 能力不是旧 Route B 的局部补丁,而是给统一 agent runtime 消费的工具契约。

---

## 0. 和统一 Agent Runtime 的关系(Codex 补充)

zhihao 的 runtime 计划里,API 用户不再长期依赖"一次 LLM call + 手搓 JSON 协议"的旧 hosted path,而是:

```text
agent-runner
  -> AgentSupervisor
    -> consumer[user]
      -> Claude/Codex agent session
      -> Feedling tools(screen / memory / identity / perception)
```

本文只定义 **memory 这组工具怎么给 agent 用**:

- backend 仍是 memory / identity / chat / wake / push 的事实源。
- `agent-runner` / consumer / driver / session / runtime token / MCP-or-HTTP transport 由 zhihao runtime 侧编排。
- hx/Codex 提供 memory 后端能力、selector、adapter、读写合同和工具契约。
- 新 runtime 下,旧 Route A / Route B 的差异应逐步收敛成"同一个 agent loop + 同一组 Feedling tools"。
- 旧 hosted path / 旧 onboarding skill 里的 memory 工具名只作为 legacy/rollback,不要再作为 v1 新契约。

人话:memory v1 不是"给旧聊天接口多塞点上下文",而是给新的统一 agent runtime 提供一套可调用、可测试、可回滚的记忆工具。

---

## 1. 工具契约(接进 agent loop;每次带 per-user runtime token)

### 1.1 Agent-facing 工具名(建议 canonical)

工具名建议跟 zhihao runtime 文档保持 `feedling_*` 前缀,避免和模型/driver 自带工具冲突:

```
feedling_memory_search(query?, bucket?, thread?, limit?)   # 包 /v1/memory/index
   → 目录 [{ id, bucket, threads[], summary, importance, source, occurred_at, decay }]
   · 只回目录,不含 content;limit 可配(0=全,不硬截);status≠active 不返回
   · follow_thread(X) = feedling_memory_search(thread=X)（跨 bucket 捞同线卡）

feedling_memory_fetch(ids[])                                 # 包 /v1/memory/fetch
   → [{ ...完整卡, content }]
   · 取正文;**真进 prompt 后更新 last_referenced_at(decay 回升)**

（feedling_memory_recall —— v1 不做:我们默认 agent 会 call tool,该查就自己 search+fetch;recall 兜底/preflight/JSON 声明都删,见 §2。recall 以后或作为"记忆多了省 token 的捷径"回来,非兜底。）

feedling_memory_write(op, payload)                         # 包 /v1/memory/actions
   add:       { bucket, threads[], summary, content, importance, pulse, source }
   supersede: { target_id, memory:{...} }                  · soft:旧卡转 superseded、不硬删
   delete:    { id }

feedling_memory_buckets() / feedling_memory_threads()      # 现有词表,给写入提示做 resolve-before-create
   → ["我们的关系","工作",...] / ["工作压力","蛋子",...]

feedling_identity_get()                                    # 包 /v1/identity/get;常驻人设
   → { identity doc }
```

### 1.2 工具名映射到后端端点

| agent-facing tool | 后端能力/端点 | 当前集成状态 |
|---|---|---|
| `feedling_memory_search` | `POST /v1/memory/index` | 后端已有旧 readside;v1 需改成 bucket/thread/content schema |
| `feedling_memory_fetch` | `POST /v1/memory/fetch` | 后端已有旧 fetch;v1 需返回 `content`,并处理 `last_referenced_at` |
| ~~`feedling_memory_recall`~~ | ~~`/v1/memory/recall`~~ | **v1 不做**(读=agent search/fetch + 气氛灯 push;recall 以后或作省 token 捷径,非 v1)|
| `feedling_memory_write` | `POST /v1/memory/actions` | 后端已有 action executor;v1 需收敛到 add/supersede/delete |
| `feedling_memory_buckets` | `GET /v1/memory/buckets` | v1 需新增,从现有卡聚合 |
| `feedling_memory_threads` | `GET /v1/memory/threads` | v1 需新增,从现有卡聚合 |
| `feedling_identity_get` | `GET /v1/identity/get` | 已有;runtime 每轮常驻 push |

**Codex 现状核对**:
- `feedling-mcp` 当前有 `/v1/memory/index`、`/v1/memory/fetch`、`/v1/memory/actions`。
- `io-onboarding/skill.md` 当前仍是旧工具名:`feedling_memory_add_moment/list/get/verify/retype`。
- `backend/agent_runtime/` 和统一 tool gateway / MCP server 还没在当前代码里落地。
- 所以 v1 要做两层:先把 backend HTTP 能力建好;再由 zhihao 的 agent-runtime tool gateway 包成上述 `feedling_memory_*` 工具。

---

## 2. 调用流程(一个回合)

> ⚠️ **最终结论(覆盖下面 preflight / §2.1)**:我们默认 agent 会 call tool,所以**读 = agent-first,该查 agent 自己调,闲聊不调**;**删掉每轮 preflight、should_read gate、recall 兜底、JSON 声明**(那些是给"不会调工具的 agent"兜底的,而不会调工具的就不算 agent)。下面的 preflight/§2.1 内容**已废弃,仅备查**。

**读(回合中)= agent-first**:
```
1. identity 常驻 push（feedling_identity_get,runtime 每轮带）
2. (可选)气氛灯 ambient:runtime 带几条 最近+高 importance 的关系底色
   —— 这是"推"不是"查",便宜、保持人设连续;需 hx 提供"按 importance 取 top-N(无 query)"能力
3. agent 觉得长期记忆相关 → 自己调:
   feedling_memory_search(query?/bucket?/thread?) → 看目录 → 挑 → feedling_memory_fetch(ids) → 用
   → follow_thread(X) = feedling_memory_search(thread=X) 跨桶串
4. 闲聊就不调(agent 自己判断);没命中别编
不做:每轮强制 preflight / should_read / recall 兜底 / JSON 声明
```

---

<details><summary>⬇️ 以下 preflight / §2.1 已废弃(备查)</summary>

**读(回合开始你组装上下文)**:
```
1. identity 常驻 push（feedling_identity_get）
2. 底色 ambient：每轮带 最近 + 高 importance 几条（你从 search/selector 取,~2 条 + recent 2）
3. agent 自己查：
   feedling_memory_search(bucket=该话题 或 thread=某线) → 看目录 summary/threads
   → 挑相关 → feedling_memory_fetch(ids) → 用
   → 想搞清来龙去脉 → feedling_memory_search(thread=X) 串桶
4. runtime 每轮都跑一次便宜的 memory preflight / recall
   · 不再用 should_read 规则猜"该不该查"
   · recall = index→selector→fetch;只有 selector 高置信命中才注入
   · 总数封顶（可配,~3-5）,去重
```

**读侧 memory preflight 地图(v1 默认)**:

```text
用户发消息
  ↓
agent loop 开始
  ↓
runtime 先准备基础上下文:
  - feedling_identity_get 常驻 identity
  - ambient memory: 最近 + 高 importance 少量底色
  ↓
runtime 每轮都做一次便宜的 memory preflight:
  - feedling_memory_recall(query=user_message, top_k=3)
  - 内部流程:index → selector → fetch
  - selector 没有高置信命中 → 不注入 memory
  - selector 有高置信命中 → 注入少量完整卡
  ↓
模型正常思考和回复;如果长期记忆可能相关,模型可以主动调用:
  - feedling_memory_search 看目录
  - feedling_memory_fetch 取正文
  ↓
runtime 记录本轮 tool_trace:
  - 是否调用过 feedling_memory_search?
  - 是否调用过 feedling_memory_fetch?
  ↓
回合结束:
  - preflight 负责保证"不漏掉明显记忆"
  - agent tool call 负责更深的主动检索
  - tool_trace 只用于观测 agent 是否主动查了,不再决定是否兜底
```

**为什么 v1 不做 should_read gate**:
- `should_read` 规则会很脆,用户说法一变就漏。
- 这个系统的产品重点是 recall 可靠,宁可多跑一次便宜检索,也不要该想起时想不起来。
- 多跑的是 readside/selector,不是额外 LLM;成本主要是 index/fetch 和少量 selector 计算。
- 关键控制点不是"查不查",而是"查到后要不要注入 prompt"。

**建议实现流程**:

```python
preflight = feedling_memory_recall(query=user_message, top_k=3)

if preflight.items:
    context.memory.preflight = preflight.items
else:
    context.memory.preflight = []

# agent 仍然可以继续主动查更深:
# feedling_memory_search(bucket=..., thread=...)
# feedling_memory_fetch(ids=[...])
```

**注意**:
- preflight 每轮运行,但不等于每轮注入 memory。
- `feedling_memory_recall` 必须保守:低置信/弱相关就返回空,不要硬塞。
- 如果 preflight 已注入 1-3 张卡,agent 还可以通过工具继续查 thread / bucket 深挖。
- `did_read` 仍然记录在 tool_trace 里,但主要用于观测 agent 主动性,不是用于决定是否跑 recall。

### 2.1 agent 声明式 recall fallback —— ⚠️ CC review:**建议 v1 不做(defer)**

> **CC 结论**:此机制 v1 **不做**。① 和每轮 preflight recall 重复;② "agent 比关键词懂"的情况用原生 `feedling_memory_search` 已覆盖;③ `{reply, tool_calls, memory_recall_request}` 这套**手搓 JSON 协议正是统一 Claude/Codex runtime 要取代的**(开倒车);④ 多一次 LLM 重跑。**只有测试证明原生 SDK 模型仍漏记忆,再回来做。** 下面保留备查。


> Codex / hx 快想法:如果 agent 没有直接走 `feedling_memory_search/get` tool call,但它在语义上知道自己需要记忆,可以让 agent 在结构化输出里返回一个 `memory_recall_request`。runtime 看到后,代它调用 `feedling_memory_recall`,再把 recall 结果塞回 agent 进入第二轮回答。

这解决的是一个具体失败场景:

```text
agent 本来应该查 memory
但没有成功发出 tool_calls
可是它能在输出里声明:
  "我需要查这段长期记忆,请用这些参数 recall"
```

推荐优先级:

```text
1. 首选:agent 直接调用 tool
   feedling_memory_search → feedling_memory_fetch

2. 次选:agent 没 tool call,但输出 memory_recall_request
   runtime 代调 feedling_memory_recall
   然后把结果塞回 agent 第二轮

3. 最后:是否保留每轮 preflight 作为保险
   待 CC / zhihao review
```

结构示例:

```json
{
  "reply": "",
  "tool_calls": [],
  "memory_recall_request": {
    "needed": true,
    "query": "蛋子 狗 品种 比熊 胎记",
    "bucket": "宠物",
    "threads": ["蛋子"],
    "top_k": 3,
    "reason": "用户在问一个依赖长期记忆的宠物事实"
  }
}
```

runtime 判断:

```python
if output.tool_calls:
    run_tools(output.tool_calls)
elif output.memory_recall_request and output.memory_recall_request.get("needed") is True:
    memories = feedling_memory_recall(output.memory_recall_request)
    rerun_agent_with(memories)
else:
    send_reply(output.reply)
```

边界:
- `memory_recall_request.query` 是搜索词,不是事实。最终事实仍以 `feedling_memory_recall/fetch` 返回的 memory 为准。
- `top_k` 默认 3,最大不超过 5。
- `query` 控制在短句,例如 160 字以内。
- recall 结果回来后必须再给 agent 一轮,不要 runtime 直接替 agent 回答。
- 这个机制可以减少"每轮 preflight"的浪费,但是否完全替代 preflight,需要结合模型稳定性和测试决定。

</details>

**写(回合后)**:
```
agent 判断有值得记的 → feedling_memory_write(add/supersede/delete)
  · 写前先 feedling_memory_buckets()/feedling_memory_threads() 喂提示 → 逼复用现有桶/线（resolve-before-create）
  · 改口/纠正 → supersede（不硬删）
  · 别在回复里说"已记好"（异步,当下不知落没落）
```

---

## 3. 提示词(直接给 agent;bucket/thread/importance/pulse 版)

**写入指引**（把现有桶/线塞进去逼复用）:
```
判断这轮有没有值得长期记的事。不记:闲聊/临时情绪/玩笑/角色扮演/没被确认的猜测/只是引用已有。
事件即记忆,不分类型。要记 → feedling_memory_write add:
- bucket(选1,主话题):优先从现有桶选 {feedling_memory_buckets()};没有才新建,克制别造近义。
- threads(选多,1-4,线索):优先从现有线选 {feedling_memory_threads()};同一条线一个名(蛋子≠狗狗)。
- summary 一句话;content MD三段(记忆/上下文/使用提示)。
- importance 0-1(对长期理解用户多重要,不是多激烈);pulse 0-1(当时情绪多强)。
- 纠正旧事实 → supersede(target_id=旧卡)。
不要说"已记好"。
```
**读取指引**:
```
长期记忆可能相关时:
1. 选 bucket/thread → feedling_memory_search 看目录。
2. 看 summary/threads 挑 1-3 张 → feedling_memory_fetch 取正文。
3. 搞清来龙去脉 → feedling_memory_search(thread=某thread)跨桶串。
4. 没命中别编。会话开始已带几条底色(最近+高importance)。
```

---

## 4. 能力 vs 你的编排(分清谁干什么)

| | 谁 |
|---|---|
| 后端 HTTP 能力(index/fetch/recall/actions/buckets/threads)、selector、排序、加密读侧 | **hx 提供(能力)** |
| 把 HTTP 能力包成 `feedling_memory_*` tools(MCP 优先 / HTTP fallback)、runtime token 校验、tool allowlist | **zhihao runtime/tool gateway** |
| **每轮要不要调 / 调几条 / 怎么塞进 prompt / build_companion_context / 底色注入 / 封顶** | **zhihao 编排** |
| agent loop、driver、session、token、consumer、supervisor | **zhihao** |

> 即:hx 给的是"记忆后端能力 + 一份 memory adapter + 工具契约";真正暴露给 Claude/Codex 的 `feedling_memory_*` tool gateway,以及"每轮怎么用",属于 zhihao 的 agent-runtime 编排。

---

## 5. memory adapter(build_companion_context 零件)

hx 会把现在 `hosted/context.py:_model_api_context_messages` 里的 **memory 部分抽成一个独立 adapter**:
```
build_memory_context(user, query, *, limits) → { ambient[], recalled[], ... }
```
你在新 runtime 的 `build_companion_context` 里调它(和 identity/screen/perception 并列),**不用碰它内部**。

这个 adapter 的定位:
- 不是新一套 agent loop。
- 不是替代 zhihao 的 `consumer`。
- 是 `build_companion_context` 里 memory 这块的"取数零件"。
- 底层可以走同一批 `feedling_memory_search/get/recall` 能力,保证 hosted API 用户和 VPS/resident 用户行为一致。

---

**一句话**:hx 交你 = **后端能力 + 工具契约(§1)+ 一份 memory adapter(§5)+ 这套读写流程和提示词(§2/§3)**;你把它们包成 `feedling_memory_*` tools 并接进统一 agent loop,**"每轮怎么用"由你的 runtime 编排**。
