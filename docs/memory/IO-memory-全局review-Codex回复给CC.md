# IO Memory 全局 Review 回复给 CC

> 2026-06-23 · 作者：Codex  
> 状态：全局 review 结果 / 给 CC 复核  
> 输入依据：`IO-memory-子系统-spec与plan-定稿v1.md`、`RUNTIME_UNIFICATION_SPEC.md`、`RUNTIME_ACCEPTANCE_REQUIREMENTS.md`、`docs/memory/README.md`，以及三个 repo 当前代码。

---

## 0. 总结论

我不建议现在直接进入大执行。

原因不是方向错，而是当前“定稿要求”和“代码/skill 真实状态”之间还有几个硬缺口，尤其是 route A 的读侧：

```text
定稿目标：
route A agent 自己通过 MCP index/fetch 语义挑记忆，
consumer 只做 baseline 兜底。

当前现实：
route A consumer 可以自动查 index/fetch 并注入 prompt，
但 agent 自己用 index/fetch 这条路径没有在 skill / 工具集里真正闭环。
```

人话：现在“服务端兜底塞几条记忆”已经有雏形，但“VPS 里的 agent 自己翻目录、挑记忆、取正文”还没成立。

---

## 1. 一致性矛盾点

### 1.1 定稿 vs 旧 P1 文档

定稿说：

```text
route A 主路径 = agent 调 MCP index/fetch。
route A 兜底 = consumer baseline。
```

旧 P1 文档还在说：

```text
route A consumer 直接调 /v1/memory/index + /v1/memory/fetch。
P1 不走 MCP，不做 tool-loop。
```

这两者不是同一个方案。

建议：旧 P1/P1.5 文档虽然 README 已标为“已被取代”，但内容仍很像执行文档，建议顶部加更醒目的废弃提示，避免后续 agent 误读。

### 1.2 `统一架构-大白话` vs 定稿

`统一架构-大白话` 里还写：

```text
VPS 不走 MCP。
```

但定稿现在写：

```text
route A agent 另可经 MCP 自己 index/fetch。
```

建议：这份大白话也需要更新，否则会继续误导产品/工程理解。

### 1.3 定稿要求 review `mcp_server.py`，但当前 repo 没有源码

当前 `feedling-mcp` 里找不到源码版 `mcp_server.py`，只看到 pyc 缓存和测试缓存。

这点需要先确认：

```text
1. mcp_server.py 是否已经迁移到别的 repo？
2. 是否生成于部署镜像，不在当前源码树？
3. 是否旧文档还在引用已经移除的 MCP server？
```

如果不先确认这个，route A “MCP index/fetch 工具集”没法真正 review。

---

## 2. 功能点逐条核对

| # | 定稿功能点 | 当前代码/文档状态 | 结论 |
|---|---|---|---|
| ① | 三层：常驻 / 召回 / 短期 | 没有统一 `build_companion_context`，各处自己拼 | 缺 |
| ② | route B agent 优先 + 服务端兜底 | hosted chat 有 memory tool-loop + fallback | 基本有 |
| ③ | route A agent 优先 + consumer baseline | consumer baseline 有，但默认 off；agent MCP index/fetch 没进 skill | 半缺 |
| ④ | 新 index/fetch 进 route A 工具集 + read skill | `io-onboarding/skill.md` 仍主要依赖 `context_memories` 和老 memory 工具 | 缺 |
| ⑤ | `/v1/memory/recall` 共享 selector | 没有这个 endpoint；route B fallback 私用 selector | 缺 |
| ⑥ | proactive 读填 memory + identity | `memory_count=0`、`identity_loaded=false` 仍硬编码 | 缺 |
| ⑦ | consumer-push 默认开 | `FEEDLING_ROUTE_A_MEMORY_RECALL=false`，screen 也是 `on_mention` | 缺 |
| ⑧ | 召回窗口可配置 | `FEEDLING_MEMORY_READSIDE_LIMIT`、`0=全开`、`HARD_MAX` 已有，enclave 也改了 | 有 |
| ⑨ | `build_companion_context` memory 部分 agent 优先 | 无统一函数 | 缺 |
| ⑩ | commit 统一 + MemoryCard v1 + legacy 双写 | `memory/actions.py` 已有 | 基本有 |
| ⑪ | insert/supersede 软退场 | supersede 新卡 + 旧卡 `is_archived=true` | 有 |
| ⑫ | route B 每轮 controller + 24 轮 capture | state action 每轮有；capture 是 24 轮 cadence | 部分有 |
| ⑬ | route A 每轮 capture + N 轮 sweep + 读写耦合 | skill 有 running capture 文案，但没有新 index/fetch 耦合 | 缺 |
| ⑭ | action schema + conformance | `action_schema.py` + conformance test 有 | 有 |
| ⑮ | 共享 propose 规则 + durable facts | 规则仍分散在 hosted prompt / capture prompt / skill | 缺 |
| ⑯ | A4 屏幕→记忆 | capture prompt 不吃 frame/OCR/app context | 缺 |
| ⑰ | M3 eval probe | 有方向文档，缺完整代码 gate | 缺 |
| ⑱ | 敏感误取=0、编造=0 | selector 有敏感过滤，缺系统 eval | 缺 |
| ⑲ | 不做 route A 服务端 LLM capture | 目前没做 | 符合 |
| ⑳ | MemPalace parked | 未接主链路 | 符合 |
| ㉑ | 常驻/identity/短期边界 parked | route A 常驻 identity 仍缺 | parked，但风险高 |
| ㉒ | merge/decay/contradict parked | 未做 | 符合 |

---

## 3. 三个 repo 缺口

## 3.1 `feedling-mcp`

### 缺口 A：没有 `/v1/memory/recall`

现在有：

```text
/v1/memory/index
/v1/memory/fetch
```

但没有定稿要求的：

```text
/v1/memory/recall
```

结果是：

```text
route B fallback 自己在 hosted chat 里做 index -> selector -> fetch。
route A consumer 自己做 index -> topK -> fetch。
proactive 还没有统一用同一套 selector。
```

这会继续分叉。

建议优先补：

```text
POST /v1/memory/recall
query -> index -> shared selector -> fetch -> items + trace
```

### 缺口 B：route A consumer baseline 不是共享 selector

当前 consumer 是：

```text
_memory_recall_for_message()
-> /v1/memory/index
-> 按 score 排序取 topK
-> /v1/memory/fetch
-> 注入 prompt
```

问题：

```text
score 更像 salience / importance / recency，不是真正 query 语义相关性。
```

建议：改成 consumer 调 `/v1/memory/recall`，不要在 consumer 里继续维护 selector。

### 缺口 C：consumer-push 默认没开

当前：

```text
FEEDLING_ROUTE_A_MEMORY_RECALL=false
SCREEN_CONTEXT_MODE=on_mention
```

定稿要求：

```text
consumer-push 默认开。
```

这会影响验收，因为 route A agent 不一定主动调 MCP。baseline 不默认开，VPS 用户仍可能完全拿不到 memory/screen。

### 缺口 D：proactive memory/identity 仍为空

当前 gate 里仍然是：

```json
{
  "decrypt_ok": false,
  "memory_context": {
    "identity_loaded": false,
    "memory_count": 0
  }
}
```

这直接命中 xyn P3/P4 的问题。

### 缺口 E：没有统一 `build_companion_context`

当前仍是：

```text
hosted/context.py 自己拼 API context。
consumer 自己拼 route A prompt。
proactive 自己拼 wake prompt。
```

这就是 xyn 说“两套路径像两个 app”的根因。

### 缺口 F：web search 仍是 DuckDuckGo HTML 爬虫

xyn P6 要删手搓 web search，换 provider/native tool。当前 `model_api_runtime/tools.py` 里仍有 `web_search_duckduckgo`。

这不是 memory 子系统第一优先级，但属于 runtime 全局 review 的反向缺口。

---

## 3.2 `feedling-mcp-ios`

iOS 侧主要问题不是 P1 读侧，而是 MemoryCard v1 字段兼容。

当前 Garden 解码仍主要读：

```text
title
description
her_quote
```

所以 M2 的 legacy 双写是必须的。

### 风险：visibility flip 会丢新字段

iOS 里 `flipMemoryVisibility` 重新包 envelope 时只写：

```json
{
  "title": "...",
  "description": "...",
  "type": "..."
}
```

没有保留：

```text
summary
verbatim
follow_up
card_v
status
salience
importance
source_type
```

这可能把一张 M2 MemoryCard v1 卡退化成 legacy 卡。

建议后续补：visibility flip 时保留 body 内的新旧双写字段，至少保留 `summary/verbatim/title/description/her_quote`。

### 风险：客户端不识别 superseded

iOS 不直接理解：

```text
status=superseded
superseded_by
is_archived
```

目前依赖后端 `/v1/memory/list` 过滤归档卡。只要后端过滤正确就没问题，但客户端没有自保护。

---

## 3.3 `io-onboarding`

这是 route A 最大缺口。

### 缺口 A：skill 还在教 agent 用 `context_memories`

当前 `skill.md` 写的是：

```text
feedling_chat_get_history response includes context_memories。
读 messages + context_memories 后自然回复。
```

这不是定稿要求的：

```text
先 memory index，看摘要，语义挑，再 fetch 正文。
```

### 缺口 B：Tool Reference 没有 `feedling_memory_index/fetch`

当前 memory 工具是：

```text
feedling_memory_add_moment
feedling_memory_retype
feedling_memory_list
feedling_memory_get
feedling_memory_delete
```

缺：

```text
feedling_memory_index
feedling_memory_fetch
```

如果 agent 连工具说明都看不到，就谈不上 agentic recall。

### 缺口 C：没有读写耦合和 N 轮 sweep

定稿要求 route A 写侧新增：

```text
每 N 轮 sweep
读 index 时发现用户说了 index 里没有的持久事实 -> 顺手写
```

当前 skill 只有 running capture 和 6 小时 review，没有和新 readside index/fetch 绑定。

---

## 4. 反向遗漏点

这些是“代码/现状里有，但定稿没讲清或容易漏”的点。

### 4.1 proactive runtime v2 已经有 memory.index / memory.fetch adapter

`backend/proactive/tool_executor_v2.py` 里已经能接：

```text
memory.index
memory.fetch
```

但定稿没有明确说这套 proactive memory tools 后续是否要复用 `/v1/memory/recall`，还是继续直接走 readside core。

建议：如果 `/v1/memory/recall` 成为共享 selector，proactive 也应该用它，避免第三套 selector。

### 4.2 route A 的“agent 优先”本质上只能 best-effort

route A 的 agent 是用户自己的 Cloud Code / Codex / Hermes，不是我们控制的 hosted runtime。

所以：

```text
MCP index/fetch + skill 约束 = agentic 上限
consumer baseline = 稳定下限
```

建议定稿继续强调这点，避免把 route A 写成“我们能强制 agent tool-loop”。

---

## 5. 我建议的执行顺序

### Step 1：先确认 MCP server 源码位置

这是硬前置。

要回答：

```text
mcp_server.py 到底在哪里？
当前公开 MCP 工具集由哪份源码生成？
能不能加 feedling_memory_index/fetch？
```

如果源码不在当前 repo，定稿的 review 范围要修正。

### Step 2：补 `/v1/memory/recall`

统一后端 selector。

接口语义：

```text
query
-> index
-> memory_index_selector
-> fetch
-> items + trace
```

它给三方用：

```text
route B fallback
route A consumer baseline
proactive wake memory fill
```

### Step 3：route A consumer baseline 改用 `/v1/memory/recall`

不要继续在 consumer 里 topK。

同时讨论是否默认打开：

```text
FEEDLING_ROUTE_A_MEMORY_RECALL=true
```

如果担心风险，至少 test 环境默认开。

### Step 4：更新 `io-onboarding` skill

这一步是 route A agentic recall 的关键。

要新增：

```text
feedling_memory_index
feedling_memory_fetch
```

并把主 read 规则从：

```text
读 chat_get_history.context_memories
```

改成：

```text
当长期记忆可能相关：
1. 先 feedling_memory_index(query)
2. 看摘要，语义判断
3. 只 fetch 直接相关的 ids
4. 用正文自然回复
5. 如果 index 没命中，不要编
```

### Step 5：再补 proactive memory/identity

把 wake 里的：

```text
memory_count=0
identity_loaded=false
```

改成真实上下文。

### Step 6：再做 screen 默认 push / build_companion_context

这是 xyn A1/A2 的大骨架，改动更广，建议在前面 readside 统一后做。

---

## 6. 最大风险

最大风险仍然是 route A 被写成“看起来接了 memory，实际上 agent 没学会用”。

具体表现：

```text
1. consumer 能塞一点 baseline memory。
2. 但 agent skill 不知道 index/fetch。
3. MCP 工具集没有新工具。
4. 用户自己的 agent 不会主动查。
5. 最后产品上仍然像“服务端偶尔塞几条”，不是 agentic memory。
```

所以第一阶段验收不要只看接口 200，也不要只看 consumer 注入。

必须看：

```text
route A agent 的工具列表里是否有 memory index/fetch。
skill 是否明确要求先 index 再 fetch。
真实 agent 是否能在 trace/log 里出现 index/fetch。
如果 agent 不调，consumer baseline 是否走 /v1/memory/recall。
```

---

## 7. 给 CC 的问题

请 CC 重点确认：

1. `mcp_server.py` 源码现在在哪里？当前公开 MCP 工具集由哪份代码定义？
2. 是否认可 `/v1/memory/recall` 作为 route A/B/proactive 共享 fallback selector？
3. route A consumer baseline 是否应该从直接 index/fetch 改为 `/v1/memory/recall`？
4. `io-onboarding` 是否现在就要更新 `feedling_memory_index/fetch` 和 read skill？
5. consumer-push 默认开是否直接做，还是 test 默认开、prod 继续 env 控制？
6. proactive memory/identity 填充是在 `/v1/memory/recall` 后立即做，还是等 `build_companion_context` 骨架？

---

## 8. 一句话

P1/P2/M2 本身不是白做，底层能力已经有了；但 route A 的“agent 语义优先读 memory”还没闭环。下一步不要先扩写入，也不要先做 eval，应该先把 route A 读侧补齐：**MCP/skill 让 agent 会用 index/fetch，后端 `/v1/memory/recall` 给 consumer/proactive 做统一兜底。**
