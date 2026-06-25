> ⚠️ **已被取代** —— 见 **`IO-memory-子系统-spec与plan-定稿v1.md`**(冲突以定稿为准)。本文保留备查。

# IO Memory P1.5 读侧统一疑问与方案（给 CC review）

> 2026-06-23 · 作者：Codex  
> 状态：讨论稿 / 给 CC review  
> 背景：P1 已实现 route A consumer 最小 readside 接入，但用户指出这还不是最终预期的 route A / route B 统一 recall 架构。本文整理用户疑问、当前实现、我的判断，以及建议补一个 P1.5。

---

## 给 CC 的阅读地图

这份文档不是新的最终 spec，而是一次“架构对齐记录”。

它要解决的问题是：

```text
P1 已经把 route A 接上 IO memory 了，
但现在接法更像 fallback，
还没有达到用户预期的 agentic recall。
```

所以请 CC 主要看三件事：

1. 当前 P1 的边界描述是否准确。
2. P1.5 是否应该独立出来做，而不是混进 P2 写入规则。
3. route A / route B 的读侧、兜底 selector、上下文拼接到底应该怎么统一。

人话：这不是在否定 P1，而是在确认“水管接上后，下一步水龙头和分流阀怎么设计”。

---

## 术语对齐

为了避免 route A / route B 混乱，本文里的词按下面理解：

```text
route A:
  VPS / self-hosted consumer 路径。
  用户自己的 agent runtime 收消息，consumer 负责转发、注入上下文、执行 action。

route B:
  hosted/API 路径。
  服务端 hosted runtime 控制 agent loop、memory tools、fallback recall 和 prompt 拼接。

agentic recall:
  模型自己先调 memory.index，看摘要，再决定调 memory.fetch 取哪些正文。

fallback recall:
  模型没有主动调 tool 时，系统自动帮它走 index/select/fetch，再把 memory 注入 prompt。

selector:
  从 index 列表里判断哪些 memory 跟当前问题最相关的选择逻辑。

context assembler:
  把 identity、long-term memory、recent chat、screen/GPS 等上下文按统一顺序和预算拼成 prompt 的逻辑。
```

---

## 0. 一句话结论

当前 P1 只是把 route A 的 IO 长期 memory 管道先接通，属于 **fallback / 保底读侧接入**。

它还不是最终的：

```text
agent 读 index → agent 判断 fetch 哪些 memory → 统一拼接上下文 → 回答
```

所以建议补一个 **P1.5：读侧统一架构**，专门处理：

- route A agentic recall。
- route A / route B fallback selector 统一。
- context assembler / prompt 拼接统一。
- agent 如何知道何时读 memory、何时写 memory。

人话：P1 是“先让 route A 能读到 IO memory”；P1.5 才是“让 route A 像 route B 一样正确使用 memory”。

---

## 1. 当前 P1 已经做了什么

当前分支：

```text
repo: feedling-mcp
branch: feat/hosted-memory-tools
commits:
- caeaed9 docs: organize io memory plans
- 433d9af feat: inject route A memory recall
```

P1 已实现：

```text
用户发消息
→ route A consumer 收到消息
→ consumer 调 /v1/memory/index
→ consumer 根据返回 score 取 topK=5
→ consumer 调 /v1/memory/fetch
→ consumer 拼成 [IO 长期记忆] block
→ consumer 把 block 注入 prompt
→ route A agent 回复
```

同时：

- route A/B 的 `memory.create / patch / supersede / delete` action schema 已通过 conformance test 对齐。
- 真正写 memory 仍然走后端 `/v1/memory/actions` 和 `_execute_memory_action`。
- P1 没改 propose / 判断提示词。
- P1 没做 agentic recall tool-loop。
- P1 没做统一 context assembler。

当前测试：

```bash
pytest tests/test_memory_action_conformance.py tests/test_route_a_memory_recall.py tests/test_memory_m2_write_loop.py tests/test_chat_resident_consumer.py -q
# 108 passed

pytest tests/test_hosted_memory_tools.py tests/test_hosted_memory_tool_loop.py tests/test_model_api_path.py -q
# 54 passed

pytest tests/test_memory_readside_core.py tests/test_memory_readside.py -q
# 14 passed
```

---

## 2. 用户的核心疑问

用户的问题可以拆成四组。

### Q1. 当前读侧是不是只是算法 / fallback 版？

用户理解：

```text
当前 route A 不是 agent 判断 index 后再 fetch，
而是 consumer 自动查 index，再按某种算法拿前几条 fetch。
```

确认：是的。

当前 route A P1 是：

```text
index → topK → fetch → 注入 prompt
```

不是：

```text
agent 看 index → agent 选 ids → agent fetch → 回答
```

这里用户的核心不满是：

```text
如果只是 consumer 自动查一下 memory，再塞给 agent，
这不是真正的“agent 会用 memory”，只是服务端帮它兜底。
```

我的回答是：这个判断成立。P1 是打通链路，不是最终体验。

### Q2. 当前 index 筛选是不是没什么用？

用户担心：

```text
如果只是 index 返回后按 score 取前 5，
没有真正根据 query 做匹配，
那可能什么也查不到，或者查得不准。
```

我的判断：

当前 index 不是完全没用，但确实不是最终高质量 selector。

它目前有这些作用：

- 过滤当前用户的可用 memory。
- 排除 local_only / no K_enclave / archived / deleted / superseded。
- 在 enclave 内生成安全 summary。
- 返回 `score`。

但当前 route A consumer 的最后选择很弱：

```text
按 index 返回 score 排序
取 topK=5
```

这个 score 更偏：

```text
salience / importance / recency / open_thread
```

不等于：

```text
和当前用户问题的语义相关性
```

所以如果用户问：

```text
武松是什么猫？
```

理想 selector 应该命中含“武松 / 猫”的 memory。当前 route A P1 不保证这一点。

所以用户说“现在 index 筛选其实一点用没有”，严格说有点重，但方向是对的：

```text
它有工程价值：少解密、少传正文、过滤不可用卡。
但它还没有足够的产品价值：不能保证更聪明地选中相关 memory。
```

### Q3. route A 的兜底是不是和 route B 兜底一样？

结论：不完全一样。

相同点：

```text
都是不依赖模型主动调 tool 的 fallback 思路。
都是 index → select ids → fetch → 注入上下文。
```

不同点：

```text
route B:
- 优先 agent tool-loop。
- 如果模型不调 memory tool，再 fallback。
- fallback 使用 memory_index_selector 做 query/token 匹配选择。
- 还有 fallback instruction，让模型基于 fallback memory 重新回答。
- 拼接在 hosted route 的 messages 里。

route A P1:
- 没有 agent tool-loop。
- consumer 直接调 index。
- consumer 只按 score 取 topK=5。
- consumer 直接拼 [IO 长期记忆] block。
- 拼接逻辑在 consumer，不在 hosted/context.py 或统一 assembler。
```

所以当前准确表述是：

```text
route A P1 = 简化版 fallback recall
route B = agentic recall + fallback recall
```

这也是为什么用户会觉得“不符合预期”：

```text
route B 已经更像 agent 在用 tool。
route A 现在只是 consumer 自动塞 memory。
```

### Q4. 筛选算法应该放 consumer 里吗？

我的判断：不应该。

短期可以在 consumer 里打通链路，但正式架构不应该这样。

原因：

1. **consumer 是边缘适配器，不该长脑子**

consumer 应该负责：

```text
拿消息
调后端
拼基本上下文
转发 action
```

不应该长期负责：

```text
query tokenizer
中英文匹配
memory ranking
sensitive 策略
fallback 策略
```

2. **selector 应该 A/B 共用**

如果 selector 放 consumer：

```text
route A 一套 selector
route B 一套 selector
```

这会重新分叉。

3. **consumer 部署分散**

route A 是 VPS / self-hosted consumer。selector 如果放在 consumer，每次优化算法都要更新多个 consumer。

放后端则是：

```text
后端升级一次
route A/B 都吃到同一套 selector
```

4. **隐私和状态过滤应该在后端/enclave 控制**

例如：

- 是否 include_sensitive。
- 是否排除 superseded。
- local_only 是否可读。
- fetch 是否允许原文。

这些不能散到 consumer 里。

所以我的结论是：

```text
consumer 里可以短期有一个最小兜底，
但真正的 selector 应该后端化、共享化。
```

### Q5. VPS / route A 里的 agent 到底怎么被约束？

用户新增疑问：

```text
VPS 方式里，consumer 是不是只是一个转发？
如果 consumer 只是转发聊天记录，那怎么约束 VPS 里的 agent？
怎么让它主动读 index，再 fetch，再把 memory 拼进回答？
现在 route A 到底是 consumer 在做事，还是 agent 在做事？
```

这个问题非常关键，因为它决定 route A 能不能真正做到 agentic recall。

我的理解是：当前 route A 要分两层看。

#### 5.1 当前 consumer 更像“边缘转发器 + action 执行器”

当前 consumer 主要职责是：

```text
1. 收到用户聊天消息。
2. 拼接一些上下文，例如 screen context / memory block。
3. 把消息交给 VPS 里的 agent runtime。
4. 如果 agent 返回 action，就转发到后端执行。
```

人话：

```text
consumer 不是完整的大脑。
它更像一个中转站：
把用户消息递给 agent，
把 agent 产出的 action 递给后端。
```

所以如果只靠当前 consumer，它能做的是 fallback：

```text
consumer 自己先查 memory
→ 塞进 prompt
→ agent 被动看到 memory
```

但这不等于：

```text
agent 自己知道什么时候该查 memory
agent 自己会调用 memory.index / memory.fetch
agent 自己根据 index 判断取哪些正文
```

#### 5.2 真正约束 VPS agent，需要三件东西

如果 route A 要做到真正 agentic recall，需要同时具备：

```text
1. 工具暴露：
   VPS agent runtime 必须真的有 memory.index / memory.fetch 这两个 tools。

2. 工具说明 / 系统提示词：
   agent prompt 里必须明确告诉它：
   什么时候先查 index，
   什么时候 fetch，
   什么时候不要乱用 memory。

3. tool-loop 执行机制：
   agent 调 tool 后，consumer 或 runtime 要能真的执行这个 tool，
   把 tool result 再喂回 agent，
   让它继续判断或最终回答。
```

缺一块都不是真正的 agentic recall。

例如只有 prompt，没有 tool：

```text
模型知道“应该查 memory”，但没按钮可按。
```

只有 tool，没有 prompt：

```text
模型有按钮，但不一定知道什么时候按。
```

只有 tool 和 prompt，没有 loop：

```text
模型说“我要查 memory”，但系统没有把结果喂回去，链路断了。
```

#### 5.3 当前 P1 做到的是哪一层？

当前 P1 做到的是：

```text
consumer fallback recall：
  consumer 自动调 /v1/memory/index
  consumer 自动调 /v1/memory/fetch
  consumer 把结果塞进 prompt
```

这能让 route A “用上 memory”，但不是 agent 主动使用 memory。

它的好处是：

```text
不依赖 VPS agent 是否支持 tool。
弱模型 / 不会调工具的模型也能拿到一点长期记忆。
```

它的问题是：

```text
agent 没有真正参与 index 判断。
consumer 当前 selector 也还不够统一。
```

#### 5.4 P1.5 需要 CC 判断 route A agentic recall 怎么接

这里有两个方向，需要 CC 判断哪条更现实。

方案 A：consumer 给 VPS agent 暴露 memory tools。

```text
consumer 注册 memory.index / memory.fetch tools
→ agent 看到 tools
→ agent 调 tool
→ consumer 执行 tool 请求后端
→ consumer 把 tool result 喂回 agent
→ agent 回答
```

优点：

```text
最接近 route B agentic recall。
```

风险：

```text
需要确认当前 VPS agent runtime 是否支持稳定 tool-loop。
不同 agent runtime 能力可能不一致。
```

方案 B：route A 暂时不强求 agentic recall，只做后端 recall fallback。

```text
consumer 调 /v1/memory/recall
→ 后端统一 index/select/fetch
→ consumer 注入结果
→ agent 回答
```

优点：

```text
稳定、可控、实现更快。
不依赖 VPS agent 会不会调 tool。
```

风险：

```text
它还是 fallback，不是 agent 自己选 memory。
```

我的倾向：

```text
P1.5 先把方案 B 做稳：
  route A fallback 改成 /v1/memory/recall，共用后端 selector。

同时设计方案 A：
  明确 route A agentic recall 需要 consumer/runtime 支持哪些 tool-loop 能力。
```

人话：

```text
先让不会用工具的 agent 也能稳定拿到正确 memory；
再推进会用工具的 agent 自己读 index/fetch。
```

---

## 2.5 用户当前想要的真实产品效果

用户要的不是“接口存在”，而是一次聊天里真的能发生下面这件事：

```text
用户问：武松是什么猫？

agent:
  1. 先看 50 条 memory index。
  2. 从里面判断“武松 / 猫”相关。
  3. fetch 那几条正文。
  4. 把正文作为长期记忆上下文。
  5. 回答：武松是橘猫 / 或根据最新 supersede 后的事实回答。
```

如果 agent 不会调 tool，则系统兜底：

```text
consumer / backend 自动走 recall，
但 selector 也应该和 route B 一样，
而不是 route A 自己维护一套弱 topK。
```

用户真正关心的验收不是：

```text
代码里有 /memory/index 和 /memory/fetch
```

而是：

```text
清空最近聊天后，长期记忆仍然能被正确召回。
旧事实被 supersede 后，不再拿旧卡回答。
route A 和 route B 对 memory 的使用方式尽量一致。
```

---

## 3. 读侧最终应该是什么效果

用户期望的最终读侧是：

```text
用户发消息
→ agent 读 memory.index
→ agent 根据当前对话判断哪些 memory 相关
→ agent 调 memory.fetch 获取正文
→ 用统一拼接逻辑把 memory 放进提示词
→ agent 回答
```

这可以拆成三层。

### 3.1 第一层：agentic recall

优先让 agent 自己用 memory 工具。

```text
agent 调 memory.index(query, limit=50)
→ tool result 返回 index items
→ agent 选择 ids
→ agent 调 memory.fetch(ids)
→ tool result 返回 memory cards
→ agent 基于 tool result 回答
```

优点：

- 最符合 agentic memory。
- agent 可以根据上下文判断相关性。
- 不用一开始就把所有 memory 塞进 prompt。

风险：

- 模型可能不调 tool。
- 模型可能调错。
- route A 的 agent runtime 各不相同，tool 能力不稳定。

### 3.2 第二层：shared selector fallback

如果 agent 没调 tool，系统兜底。

但兜底不应该在 consumer 里写算法，而应该在后端共用 selector。

建议新增或扩展服务端能力：

```text
POST /v1/memory/recall
```

职责：

```text
query
→ index
→ shared selector
→ fetch
→ 返回 selected memory cards + trace
```

示例返回：

```json
{
  "items": [
    {
      "id": "mem_cat",
      "summary": "武松是橘猫。",
      "verbatim": "武松其实是橘猫。"
    }
  ],
  "trace": {
    "mode": "fallback_selector",
    "index_count": 50,
    "selected_ids": ["mem_cat"],
    "query_terms": ["武松", "猫"]
  }
}
```

也可以扩展 `/v1/memory/index`：

```json
{
  "query": "武松是什么猫？",
  "limit": 50,
  "select": true,
  "top_k": 5
}
```

返回：

```json
{
  "items": [...],
  "selected_ids": ["mem_cat"],
  "selection_trace": {...}
}
```

我的倾向：

```text
P1.5 先新增 /v1/memory/recall 更清晰
```

因为 index 是“看目录”，recall 是“完整兜底召回”。语义更明确。

### 3.3 第三层：保底 topK

如果 shared selector 没选出结果，才用保底 topK。

```text
selector selected_ids 为空
→ fallback to salience / importance / recency topK
```

当前 P1 基本就在这一层附近。

---

## 4. 统一拼接逻辑应该在哪里

用户问：

```text
agent fetch 正文之后，怎么用和 route B 一样的拼接算法拼进提示词？
拼接算法在哪里定义？
是不是要专门弄一个拼接接口？
```

我的判断：需要一个统一 context assembler，但不一定要在 P1 里做。

最终应该有两种可选形态。

### 方案 A：tool-loop result 直接作为上下文

这是标准 agent loop：

```text
agent 调 memory.index
tool result 追加给 agent
agent 调 memory.fetch
tool result 追加给 agent
agent 回答
```

这种情况下不需要额外拼接接口。tool result 本身就是上下文。

优点：

- 最自然。
- 最少服务端接口。

缺点：

- A/B 的 prompt block 结构还是可能不一致。
- 难统一 token budget / section order。

### 方案 B：新增 `/v1/context/assemble`

服务端专门做上下文拼接。

输入：

```json
{
  "route": "route_a",
  "query": "武松是什么猫？",
  "memory_ids": ["mem_cat"],
  "include_recent": true,
  "include_screen": true
}
```

输出：

```json
{
  "blocks": [
    {"type": "identity", "text": "..."},
    {"type": "long_term_memory", "text": "..."},
    {"type": "recent_chat", "text": "..."},
    {"type": "screen", "text": "..."}
  ],
  "prompt_text": "..."
}
```

优点：

- route A/B 拼接统一。
- 顺序、预算、敏感策略集中管理。
- 容易测试。

缺点：

- 新增后端接口。
- 需要 zhihao 参与评估。

我的建议：

```text
P1.5 先做 /v1/memory/recall 统一 fallback selector
P1.5 或 P2 再做 /v1/context/assemble
```

原因：`memory/recall` 直接解决“筛选不准”；`context/assemble` 解决“拼接不统一”。两者都需要，但可以分步。

---

## 5. 写侧最终应该是什么效果

用户理解：

```text
用户输入之后，调用设定好的判断提示词，执行提取操作，调用 tool，走到服务端，相当于后续走和 route B 一样的逻辑。
```

确认：方向对。

最终写侧应该是：

```text
用户输入
→ route A agent 根据共享 propose 规则判断是否要写 memory
→ 如果要写，输出 memory.create / patch / supersede / delete
→ consumer 规整 action schema
→ POST /v1/memory/actions
→ 后端 _execute_memory_action commit
```

route B：

```text
用户输入
→ hosted controller 根据同一套 propose 规则判断是否写
→ coerce_runtime_action
→ _execute_memory_action commit
```

最终目标：

```text
A/B 使用同一 propose 规则
A/B 输出同一 action schema
后端统一 commit
```

但当前 P1 没做：

- 没统一 propose prompt。
- 没修“蛋子”这类 durable fact 漏记。
- 没改 agent 判断“该不该记”的规则。

这些应属于 P2 / M3。

---

## 6. 约束 agent 的提示词在哪里配置

用户问：

```text
约束 agent 的提示词在哪配置？
怎么让 agent 知道该怎么读 / 写 memory？
```

分 route B 和 route A。

### 6.1 route B

route B 的 agent/controller prompt 在服务端。

主要位置：

```text
backend/hosted_runtime.py
```

例如：

```text
build_background_execution_messages(...)
```

这里会告诉模型：

- 支持哪些 action。
- 什么时候 `memory.create`。
- 什么时候 `memory.supersede`。
- 返回 JSON action。

### 6.2 route A

route A 的 agent 是用户自己的 agent runtime，不完全由服务端控制。

consumer 当前负责：

```text
转发用户消息
注入 screen context / memory block
执行 agent 返回的 action
```

但 route A agent 的“系统提示词 / 工具说明”取决于：

- Hermes 配置。
- Claude Code 配置。
- 用户自己的 agent runtime。
- consumer 注入的额外 prompt。

所以 P2 需要产出一份共享规则，例如：

```text
docs/memory/propose_rules_v1.md
```

或代码化：

```text
backend/memory/propose_rules.py
```

然后：

```text
route B hosted controller 直接引用这份规则
route A consumer 把这份规则注入给 agent，或要求 route A runtime 配置这份规则
```

读侧也需要一份工具使用说明：

```text
memory.index:
  用于先看相关 memory 摘要，不返回原话。

memory.fetch:
  只 fetch 你判断直接相关的 ids。

优先级:
  用户当前明确表达/纠正 > 直接相关 memory > 旧 assistant 草稿。

边缘相关:
  不要硬断言。
```

---

## 7. 建议补一个 P1.5

当前计划应改成：

```text
P1 = route A 最小 recall fallback，已做
P1.5 = route A agentic recall + shared fallback selector + context assembler 设计
P2 = 统一 propose / 修 durable facts 漏记
P3 = eval
```

### P1.5 建议范围

#### 1. 新增 `/v1/memory/recall`

服务端统一做：

```text
query → index → memory_index_selector → fetch → items + trace
```

route A consumer 不再自己 score topK。

#### 2. route A agentic recall tool-loop

让 route A agent 可以调用：

```text
memory.index(query, limit=50)
memory.fetch(ids)
```

优先使用 agent 选择。

#### 3. fallback 顺序

```text
agent tool-loop 成功
→ 用 agent 选择的 fetch 结果

agent 没调 / 调失败 / 模型不支持
→ /v1/memory/recall fallback

recall 也没有结果
→ 不注入 memory，不瞎编
```

#### 4. 统一 context assembler 设计

至少先定接口和输出结构：

```text
identity
long_term_memory
recent_chat
screen/GPS
pending
```

是否 P1.5 就实现 `/v1/context/assemble`，需要 CC / zhihao 再评估。

#### 5. 共享 agent prompt

产出：

```text
memory_tool_rules_v1
propose_rules_v1
```

分别约束：

- 如何读 memory。
- 如何写 memory。

#### 6. 测试

覆盖：

```text
agent 调 memory.index/fetch → 走 agentic recall
agent 不调 tool → fallback recall
fallback selector 命中 → fetch 正文
fallback selector 不命中 → 不瞎编
A/B memory block 结构一致
```

---

## 8. 给 CC 的 review 问题

请 CC 重点判断：

1. 是否认可当前 P1 只是 fallback / 保底数据链路，不是最终读侧统一架构？
2. 是否应该新增 P1.5，而不是把这些塞进 P2？
3. `memory selector` 是否应该放后端 `/v1/memory/recall`，而不是 consumer？
4. `/v1/context/assemble` 是否应该在 P1.5 做，还是先只设计接口，P2 再实现？
5. route A agentic recall 的 tool-loop 应该怎么接：复用 proactive tool executor，还是单独给 resident chat 增加 memory tools？
6. 共享 agent prompt 应该落在哪里：docs 规则、backend 常量，还是 consumer 注入？

补充判断：

7. `/v1/memory/recall` 是否应该成为 A/B 共享 fallback 主入口？
8. route A 的 P1 当前 topK fallback 是否保留为最后保底，还是直接替换成后端 selector？
9. P1.5 是否需要实现最小 `/v1/context/assemble`，还是只先定义结构，等后续统一 prompt 时再做？
10. agentic recall 对 route A 是否现实：consumer 是否有能力把 memory.index/fetch 暴露成模型工具，还是只能先走 backend recall fallback？
11. 当前 VPS agent runtime 到底支持什么级别的 tool-loop：只能收 prompt，还是能声明 tool、执行 tool、接收 tool result 后继续推理？
12. 如果 route A 暂时不能稳定 tool-loop，是否接受 P1.5 先以后端 `/v1/memory/recall` 作为主路径，agentic recall 作为后续增强？

---

## 8.5 建议 CC 输出的结果格式

希望 CC 不只是泛泛评价，而是给一个可执行判断：

```md
## 结论
是否认可 P1.5：
是否认可 /v1/memory/recall：
是否需要 /v1/context/assemble：

## route A 读侧方案
agentic recall 怎么接：
fallback recall 怎么接：
consumer 里保留什么：
后端负责什么：

## route B 对齐点
哪些逻辑复用：
哪些逻辑暂时不同：

## 执行顺序
第一步：
第二步：
第三步：

## 风险
最大风险：
如何测试：
```

---

## 9. Codex 当前建议

我的建议是：

```text
短期不要否定 P1。
P1 作为“route A 能读 IO memory”的最小接入是有价值的。

但不要把 P1 当最终方案。
需要补 P1.5 解决 agentic recall / fallback selector / context assembler。
```

更具体：

```text
P1 已完成：
  route A 最小 readside 接入 + action schema conformance。

P1.5 应做：
  /v1/memory/recall + route A agentic recall + fallback selector + context assembler 设计。

P2 再做：
  统一 propose 规则 + durable facts 捕获。

P3 再做：
  eval + memory 质量。
```

人话总结：

```text
现在只是把水管接上。
下一步要加水龙头和分流阀：
agent 会用时让 agent 自己选；
agent 不会用时后端统一兜底选；
最后所有上下文用同一套规则拼起来。
```
