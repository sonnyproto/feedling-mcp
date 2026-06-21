# IO Memory M1 Agent Tools 决策上下文给 CC

作者：Codex
日期：2026-06-21
目标读者：CC / 后端执行 agent / Seven / zhihao

## 1. 这份文档解决什么问题

我们已经把 IO Memory M1 的 `index -> fetch` 双层读能力接到了 test，并且真实测试通过：

- test 环境可以命中 `model_api_readside_v1`
- 用户问“你还记得我家猫叫什么、名字是怎么来的吗？”
- 系统能通过 memory 找到“猫叫武松”相关记忆并用于回复

但和 Seven 最新沟通后，M1 的核心诉求需要重新对齐：

当前实现更像是“服务端自动帮 agent 查 memory，然后塞进 prompt”。

Seven 更想要的是“把 memory index/fetch 暴露成 agent tools，让 agent 自己决定什么时候查、查哪些、取哪些正文”。

人话：现在像后端替 agent 翻目录和摘内容；Seven 想要 agent 自己拿工具翻目录、取正文。

## 2. 已经完成的能力

### 2.1 后端 readside 接口

已经有两个后端接口：

```http
POST /v1/memory/index
POST /v1/memory/fetch
```

`index` 返回轻量摘要，给 agent 看目录：

```json
{
  "items": [
    {
      "id": "mem_cat_1",
      "summary": "用户家猫叫武松，是一只橘猫，名字来自武松打虎的联想。",
      "bucket_refs": ["宠物", "猫"],
      "status": "active",
      "salience": "high",
      "is_open_thread": false,
      "is_sensitive": false,
      "score": 0.91
    }
  ]
}
```

`fetch` 按 id 返回完整 memory：

```json
{
  "items": [
    {
      "id": "mem_cat_1",
      "summary": "用户家猫叫武松，是一只橘猫，名字来自武松打虎的联想。",
      "verbatim": "我家猫叫武松，因为它是橘猫，看起来像小老虎。",
      "bucket_refs": ["宠物", "猫"],
      "status": "active",
      "salience": "high",
      "context": "来自用户关于猫的对话",
      "source_type": "chat",
      "is_sensitive": false
    }
  ],
  "missing_ids": [],
  "unavailable_ids": []
}
```

### 2.2 MCP tools 已经存在

MCP 路径里已经暴露了：

- `feedling_memory_index`
- `feedling_memory_fetch`

代码位置：

- `backend/mcp_server.py`

人话：VPS / MCP 形态已经有“工具”的雏形。

### 2.3 Hosted API 当前也已经能用 memory

test 上现在走的是：

```text
用户消息
-> 服务端自动构造上下文
-> 服务端自动查 memory index
-> 服务端算法选中相关 memory
-> 服务端 fetch 正文
-> 服务端把 memory 塞进 prompt
-> LLM 回复
```

代码位置：

- `backend/hosted/chat_routes.py`
- `backend/hosted/context.py`
- `backend/enclave_app.py`
- `backend/memory_index_selector.py`

人话：现在用户体验能通，但 agent 没有真正“自己调用 memory tool”。

## 3. Seven 最新表达的核心意思

Seven 认为当前“服务端自动判断并塞上下文”的方向不够对。

他的意思是：

```text
应该给用户的 agent 做 tools，让它自己取 memory。
感知系统也是 agent tools。
API 形式虽然 agent 在我们这里，但用的是用户的 API。
所以可以把 memory 能力作为 tools 暴露给 agent，让它自己调用。
```

我对 Seven 意思的理解：

```text
不要让后端替 agent 决策“哪些 memory 应该进上下文”。
后端应该提供安全、可控、分层的 memory 工具。
agent 自己判断当前对话是否需要查 memory。
agent 自己看 index。
agent 自己决定 fetch 哪些正文。
```

人话：后端提供“书架目录”和“取书接口”，不要替读者把书全摊在桌上。

## 4. 当前分歧点

### 4.1 当前 test 上的 M1 形态

```text
服务端自动 recall
服务端自动 selector
服务端自动塞 prompt
```

优点：

- 快
- 稳
- 每轮只走一次正式回复 LLM
- 工程风险小
- 已经 test 验证通过

问题：

- agent 没有真正掌控 memory 使用
- selector 还是算法，语义能力有限
- 违背 Seven 想要的 “agent tools” 方向
- 后续容易继续堆服务端规则

### 4.2 Seven 想要的 M1.5 形态

```text
用户消息
-> agent 看到 memory_index / memory_fetch tools
-> agent 判断是否需要查 memory
-> agent 调 memory_index 拿最多 50 条目录
-> agent 从 index 里判断哪些相关
-> agent 调 memory_fetch 取正文
-> agent 基于取回 memory 回复
```

优点：

- 符合 agent loop
- 符合“memory 是 agent 可调用能力”的设计方向
- 语义判断由 LLM/agent 做，而不是服务端弱算法
- 后续可以统一 API route 和 VPS / MCP route 的心智

代价：

- 每次需要 memory 时，可能多一次或多轮 LLM 调用
- 延迟增加
- 成本增加
- 工程上要接 hosted foreground chat 的 tool loop
- 测试方式从“看 context 是否塞了 memory”变成“看 tool_calls 是否发生”

人话：更聪明，但更慢、更贵、链路更复杂。

## 5. 关键设计结论

### 结论 1：index 可以给 agent 多看一点

建议默认：

```text
memory_index 返回最多 50 条 index
```

index 是摘要，不是完整正文，所以可以作为“目录”给 agent 看。

### 结论 2：fetch 不能无脑全取

建议默认：

```text
memory_fetch 单轮最多 3-5 条正文
```

原因：

- 防止 prompt 过长
- 防止敏感正文过量暴露
- 防止 agent 把全部 memory 当上下文塞进去

### 结论 3：默认不再服务端算法强行塞 memory

目标形态：

```text
服务端提供 tools
agent 自己判断
```

但为了上线安全，可以保留自动 recall 作为 fallback 或开关：

```text
MEMORY_MODE=auto_readside
MEMORY_MODE=agent_tools
```

或者：

```text
MODEL_API_MEMORY_TOOLS_ENABLED=true
MODEL_API_AUTO_MEMORY_CONTEXT_ENABLED=false
```

### 结论 4：这不是简单“把算法改成 LLM”

更准确说法是：

```text
把 memory recall 从服务端自动流程改成 agent tool loop。
```

LLM 会参与选择，是因为 agent 通过 tool loop 自己看 index、决定 fetch。

人话：不是服务端额外调用一个 LLM 选 memory，而是正常 agent loop 里，agent 自己使用工具。

## 6. 建议 CC 输出方案时重点回答的问题

请 CC 不要只写抽象方案，重点回答下面这些工程边界。

### Q1. Hosted foreground chat 怎么接 tool loop？

当前 `/v1/model_api/chat/send` 不是完整 tool loop。

需要判断：

- 是复用 `proactive.tool_loop_v2.run_tool_loop_v2`
- 还是为 hosted foreground chat 做一个轻量 loop
- 是否继续兼容现有 web_search 二阶段逻辑

相关代码：

- `backend/hosted/chat_routes.py`
- `backend/proactive/tool_loop_v2.py`
- `backend/proactive/tool_executor_v2.py`
- `backend/model_api_runtime/tools.py`

### Q2. Hosted memory tools 放在哪里？

候选方案：

```text
方案 A：复用 proactive tool_executor_v2，加 memory adapters
方案 B：在 model_api_runtime/tools.py 里新增 hosted 专用 memory tool executor
方案 C：抽一个 shared memory_tools.py，MCP / hosted / proactive 共用
```

我倾向先用 B 或 C，避免把 foreground chat 强绑 proactive runtime。

### Q3. tool 名称用什么？

建议 hosted API agent 内部使用：

```text
memory.index
memory.fetch
```

MCP 对外可以继续保留：

```text
feedling_memory_index
feedling_memory_fetch
```

人话：内部 agent tool 名短一点；MCP 对外工具名保留现状。

### Q4. index 返回是否需要先算法筛？

Seven 方向下，建议第一版不要算法强筛，只做安全过滤和排序：

```text
只返回当前用户
只返回 active
排除 local_only
排除不可解密
排除 deleted / archived / superseded
默认 top 50
按 open_thread / salience / importance / recency 排序
```

然后让 agent 自己从 50 条里判断。

人话：后端只负责把目录整理好，不再替 agent 判“哪条一定相关”。

### Q5. fetch 要不要限制数量？

建议必须限制。

默认：

```text
max ids per fetch = 5
```

如果 agent 要更多，需要下一轮再调。

### Q6. 旧自动 recall 是否保留？

建议短期保留，开关控制。

推荐默认：

```text
test: 先开 agent_tools，保留 auto fallback
prod: 先不开，等 test 验证
```

## 7. 建议的实现方向

### Step 1：先加 hosted memory tool executor

提供两个函数：

```python
run_memory_index_tool(store, api_key, args) -> dict
run_memory_fetch_tool(store, api_key, args) -> dict
```

它们内部调用已有接口：

```http
POST /v1/memory/index
POST /v1/memory/fetch
```

或者直接复用已有 backend/enclave 内部能力。

### Step 2：给 foreground chat 加 tool loop

把当前：

```text
provider_client.chat_completion(...)
```

升级成：

```text
call_model(messages)
-> parse tool_calls
-> call memory tools
-> append tool result
-> call_model again
-> until final reply
```

### Step 3：prompt 里明确告诉 agent 怎么用 memory tools

系统提示需要说明：

```text
需要长期记忆时，先调用 memory.index。
只 fetch 明确相关的 memory。
不要一次 fetch 所有 memory。
如果 index 里没有相关项，不要编造。
敏感记忆只有用户当前话题明确相关时才 fetch。
```

### Step 4：保留 trace

返回或 action trace 里要能看到：

```json
{
  "memory_tools": {
    "mode": "agent_tools",
    "index_called": true,
    "fetch_called": true,
    "fetched_ids": ["mem_cat_1"]
  }
}
```

人话：产品测试时要能确认“这次真的是 agent 自己调了工具”。

## 8. 验收标准

### 必须通过的用户测试

准备一条 memory：

```text
用户家猫叫武松，是橘猫，名字来自武松打虎。
```

清空最近聊天后问：

```text
你还记得我家猫叫什么、名字怎么来的吗？
```

期望：

```text
agent 调用 memory.index
agent 从 index 中发现猫/武松相关摘要
agent 调用 memory.fetch 取对应正文
回复里正确说出武松和名字来历
trace 里能看到 index/fetch tool calls
```

### 不能接受的情况

```text
没有调用 memory tools，但回复却说记得
一次 fetch 50 条正文
index 里返回 verbatim / her_quote 等原话
敏感正文默认进入 index
tool 失败后模型编造记忆
```

## 9. 给 CC 的重点判断题

请 CC 给方案时明确回答：

1. hosted foreground chat 的 tool loop 是复用 proactive 还是新建轻量 loop？
2. memory tools executor 放哪个模块？
3. 是否关闭当前 auto readside recall，还是保留 fallback？
4. index 给 50 条时是否只做安全过滤，不做相关性算法筛选？
5. fetch 单次最多几条？
6. trace 怎么返回，方便产品测试？
7. 这次改动怎么最小化，不影响 Memory Garden 和现有写入？

## 10. Codex 后续执行注意事项

我后续执行时需要注意：

- 不要先做 selector 小优化，优先做 hosted memory tools。
- 不要删除现有 `index/fetch` 接口。
- 不要破坏 test 已经跑通的 M1 readside 能力。
- 不要动旧 Memory Garden 展示。
- 不要迁移历史数据。
- 不要改写入 MemoryCard v1，那个是下一阶段。
- 工程上要先写测试，再改代码。

推荐新增测试：

```text
tests/test_hosted_memory_tools.py
```

或扩展：

```text
tests/test_model_api_path.py
```

测试重点：

```text
foreground chat can execute memory.index tool
foreground chat can execute memory.fetch tool
tool result is fed back into second model call
fetch ids are capped
auto memory context can be disabled when agent tools mode is enabled
trace records memory tool calls
```

## 11. 一句话结论

M1 的底座已经打通：`index -> fetch` 可以用了。

但 Seven 最新确认的核心方向是：不要让服务端自动替 agent 塞 memory，而是把 `memory.index` / `memory.fetch` 变成 hosted agent 可调用 tools，让 agent 自己完成“看目录、取正文、再回复”的流程。

人话：目录和取书能力已经有了，现在要把它交到 agent 手里。
