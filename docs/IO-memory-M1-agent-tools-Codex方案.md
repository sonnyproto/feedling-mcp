# IO Memory M1 Agent Tools Codex 方案

作者：Codex
日期：2026-06-21
状态：已按 CC 修订方案执行到 test 前验证阶段

> 命名说明：CC review 文档里写的是 `M1.5`，但当前决策已经收口为 M1 的一部分。
> 人话：这不是另起一个版本，而是把 M1 从“只提供 index/fetch 接口”推进到“Hosted API 的 agent 真的能用 index/fetch 工具”。

## 1. 方案结论

我建议并已实现当前 Hosted API 的 memory 使用方式，从“服务端自动 recall 后塞 prompt”推进到“agent tools 优先，auto readside 兜底”。

目标形态：

```text
用户消息
-> hosted agent 正常进入 tool loop
-> agent 需要长期记忆时调用 memory.index
-> 后端返回最多 50 条 index 摘要
-> agent 自己判断哪些 index 相关
-> agent 调用 memory.fetch(ids)
-> 后端返回少量完整 memory
-> agent 基于取回 memory 回复用户
```

人话：后端不再替 agent 决定“哪条记忆该被使用”；后端只提供安全的目录和取正文工具。

实际落地名采用下划线：

```text
memory_index
memory_fetch
```

原因：当前是 prompt-level tool loop，不是 provider 原生 function calling；下划线命名是为了以后接原生 tools 时更兼容。

## 2. 为什么这么改

当前 test 版本已经证明 `index -> fetch` 能跑通，但它还是服务端自动做 recall。

当前形态：

```text
服务端自动查 index
服务端用算法选 memory
服务端 fetch 正文
服务端塞进 prompt
LLM 回复
```

这个形态的好处是快、稳、成本低，但它不是 Seven 最新确认的方向。

Seven 要的是：

```text
memory 是 agent 的 tool
agent 自己决定是否查记忆
agent 自己决定 fetch 哪些正文
```

所以这次应该把 M1 从“readside 能力已提供”推进到“hosted agent 真正使用 readside tools”。

## 3. 不建议的做法

### 3.1 不建议只优化 selector

selector 查多一点确实有问题，但不是当前最大问题。

如果继续优化 selector，本质还是：

```text
服务端替 agent 选 memory
```

这和 Seven 要的 agent tools 方向不一致。

### 3.2 不建议直接删除自动 recall

自动 recall 已经在 test 通过，应该先保留成 fallback。

否则如果 agent tools 在某些模型上不稳定，test 会直接失去可用 memory。

### 3.3 不建议让 memory.index 返回完整正文

index 只能是目录。

不要在 index 里返回：

```text
verbatim
her_quote
follow_up
具体 sensitive_scope
完整正文
```

完整正文必须通过 `memory.fetch` 按 id 取。

## 4. 推荐开关设计

新增两个环境变量：

```text
MODEL_API_MEMORY_TOOLS_ENABLED=false
MODEL_API_AUTO_MEMORY_CONTEXT_ENABLED=true
```

含义：

```text
MODEL_API_MEMORY_TOOLS_ENABLED
是否让 hosted foreground chat 暴露 memory.index / memory.fetch tools 给 agent。

MODEL_API_AUTO_MEMORY_CONTEXT_ENABLED
是否保留服务端自动 readside recall，作为 no-tool-call fallback。
```

推荐环境：

```text
test:
MODEL_API_MEMORY_TOOLS_ENABLED=true
MODEL_API_AUTO_MEMORY_CONTEXT_ENABLED=true

prod 初始保守：
MODEL_API_MEMORY_TOOLS_ENABLED=false
MODEL_API_AUTO_MEMORY_CONTEXT_ENABLED=true
```

人话：test 先让 agent 自己试着用工具；如果模型没调工具，但旧算法能找到相关记忆，就自动回填再答一次。prod 可以先不开 tools，保留旧路径。

不建议长期这样：

```text
MODEL_API_MEMORY_TOOLS_ENABLED=true
MODEL_API_AUTO_MEMORY_CONTEXT_ENABLED=true
```

如果实现成“同时塞旧 memory + agent 再 fetch”，会重复。但当前实现不是长期双塞，而是：

```text
优先不塞旧 memory
先让 agent tool loop 自己调 memory_index / memory_fetch
如果 agent 完全没调 memory tool，才跑一次 auto_readside 回填
```

所以它是兜底，不是重复上下文。

## 5. Tool 设计

### 5.1 memory_index

用途：给 agent 看记忆目录。

输入：

```json
{
  "query": "用户当前问题或 agent 自己整理的检索意图",
  "limit": 50,
  "include_sensitive": false
}
```

输出：

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
  ],
  "count": 1,
  "limit": 50
}
```

第一版建议：

```text
query 可以记录 trace，但不要用 query 强算法筛选。
后端只做安全过滤、状态过滤、排序、top 50。
让 agent 自己读 index 判断相关性。
```

排序规则：

```text
is_open_thread 优先
salience 高优先
importance 高优先
last_active / updated_at / occurred_at / created_at 新优先
id 兜底稳定排序
```

### 5.2 memory_fetch

用途：按 id 取完整 memory。

输入：

```json
{
  "ids": ["mem_cat_1"],
  "include_archived": false,
  "include_superseded": false
}
```

输出：

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

限制：

```text
单次最多 fetch 5 条
默认只允许 active
找不到放 missing_ids
解不开 / local_only / no K_enclave 放 unavailable_ids
敏感 memory 需要显式相关才允许 fetch
```

## 6. Prompt 设计

需要在 hosted foreground chat 的 system / tool instruction 中加入：

```text
You have access to memory_index and memory_fetch.

Use memory_index when the user asks you to remember something, refers to past preferences, relationships, pets, emotional patterns, ongoing projects, or prior context that may live in long-term memory.

memory_index returns safe summaries only. Read the summaries and fetch only the few memories directly relevant to the current user message.

Do not fetch every memory. Usually fetch 1-3 memories, max 5.

If memory.index has no relevant item, answer honestly without inventing.

Do not expose raw sensitive details unless the current user message clearly asks for that topic and the fetched memory is necessary.
```

中文人话：

```text
该查记忆时先查目录。
只取真正相关的正文。
不要为了显得记得而乱取。
找不到就承认不确定。
敏感内容不要主动展开。
```

## 7. 工程切法

### Step 1：新增 hosted memory tools 模块

已新增：

```text
backend/model_api_runtime/memory_tools.py
```

提供：

```python
def memory_tool_specs() -> list[dict]:
    ...

def execute_memory_tool(store, api_key: str, name: str, args: dict) -> dict:
    ...
```

实际支持：

```text
memory_index
memory_fetch
```

内部调用共享 core，不走 HTTP 自调：

```text
backend/memory_readside_core.py
```

HTTP 路由 `/v1/memory/index`、`/v1/memory/fetch` 也改成调用同一份 core。

### Step 2：新增 foreground chat tool loop

当前 `backend/hosted/chat_routes.py` 里是直接：

```python
provider_client.chat_completion(...)
```

已在 `backend/hosted/chat_routes.py` 增加轻量 foreground tool loop：

```python
_run_model_api_memory_tool_loop(...)
```

行为：

```text
call model
parse reply/tool_calls
if memory tool call:
  execute tool
  append assistant tool_call + tool result
  call model again
until final reply or max_iters reached
```

第一版 `max_iters=3`：

```text
round 1: model asks memory_index
round 2: model asks memory_fetch
round 3: model final reply
```

### Step 3：复用现有 tool call 解析能力

当前代码已有：

- `model_api_runtime.tools.extract_web_search_requests`
- `proactive.agent_protocol_v2.parse_agent_response_v2`
- `proactive.tool_loop_v2.run_tool_loop_v2`

实际处理：

```text
复用 parse_agent_response_v2 / agent_tool_calls_v2 的解析能力。
foreground chat 使用独立轻量 loop。
web_search 仍保留原有二阶段，不在本轮强迁。
```

原因：

```text
proactive tool loop 面向 wake / background / screen。
foreground chat 面向即时用户回复，错误处理、trace、返回格式不同。
```

### Step 4：调整 context 构建

当：

```text
MODEL_API_MEMORY_TOOLS_ENABLED=true
MODEL_API_AUTO_MEMORY_CONTEXT_ENABLED=false
```

`hosted_context._model_api_context_messages(...)` 不自动塞 `context_memories`。

但仍然可以保留：

```text
identity
recent messages
screen context
pending state
tool instructions
```

人话：最近聊天历史继续给；长期 memory 交给工具。

### Step 5：trace

action trace 和接口返回里需要能看到：

```json
{
  "memory_tools": {
    "mode": "agent_tools",
    "index_called": true,
    "fetch_called": true,
    "tool_calls": [
      {
        "name": "memory_index",
        "ok": true,
        "item_count": 21
      },
      {
        "name": "memory_fetch",
        "ok": true,
        "ids": ["mem_cat_1"],
        "item_count": 1
      }
    ]
  }
}
```

测试时用户能确认：

```text
这次不是服务端一开始偷偷塞 memory，而是 agent 先真调了 memory tool。
如果 agent 没调工具但旧算法找到相关记忆，会显示 fallback。
```

## 8. 测试计划

### 8.1 单元测试

已新增：

```text
tests/test_hosted_memory_tools.py
tests/test_memory_readside_core.py
```

覆盖：

```text
memory_index calls shared readside core
memory_index returns only index-safe fields
memory_fetch caps ids to 5
memory_fetch returns missing/unavailable
tool trace records index/fetch calls
```

### 8.2 foreground loop 测试

已新增/扩展：

```text
tests/test_model_api_path.py
tests/test_hosted_memory_tool_loop.py
```

脚本模型返回：

第一轮：

```json
{
  "tool_calls": [
    {
      "name": "memory_index",
      "args": {
        "query": "用户问家猫叫什么"
      }
    }
  ]
}
```

第二轮：

```json
{
  "tool_calls": [
    {
      "name": "memory_fetch",
      "args": {
        "ids": ["mem_cat_1"]
      }
    }
  ]
}
```

第三轮：

```json
{
  "reply": "记得，你家猫叫武松，名字来自武松打虎的联想。"
}
```

断言：

```text
model called 3 times
memory_index executed once
memory_fetch executed once
tool result was fed back into model messages
final reply returned to user
trace contains memory_tools
```

### 8.3 test 环境真实验收

用户侧测试：

```text
1. test 环境登录
2. Garden 里确认有“猫叫武松”记忆
3. 清空最近聊天或换新会话，避免 recent messages 命中
4. 问：你还记得我家猫叫什么、名字怎么来的吗？
5. 看回复是否正确
6. 看 runtime / action trace 是否出现 memory.index 和 memory.fetch
```

成功标准：

```text
不是只看 context.memory_selection.mode = model_api_readside_v1
而是 memory_tools.index_called = true
并且 memory_tools.fetch_called = true

如果模型没有调工具但仍答对，要看：

```text
memory_tools.mode = fallback
memory_tools.fallback_reason = no_tool_call_backfilled
```

## 9. 当前代码改动摘要

已实现：

```text
backend/memory_readside_core.py
```

把 readside 的候选筛选、排序、index、fetch 抽成共享 core。

```text
backend/memory/routes.py
```

`/v1/memory/index`、`/v1/memory/fetch` 改成 core 的薄封装。

```text
backend/model_api_runtime/memory_tools.py
```

新增 hosted prompt-level memory tools：`memory_index` / `memory_fetch`。

```text
backend/hosted/context.py
```

支持 `include_memory_context=false`，让 agent tools 模式下不要一开始就自动塞长期 memory。

```text
backend/hosted/chat_routes.py
```

新增 foreground memory tool loop、no-tool-call fallback、trace 输出。

```text
backend/hosted/history_import.py
```

把 prompt 里的来源标签从 `relevant memory cards` 改为 `candidate memory context`，避免暗示候选卡已经被判定为相关。

人话：这次不是改 UI，也不是改写入记忆。它改的是 hosted API 聊天时“怎么用已有长期记忆”。

## 10. 当前验证结果

已跑：

```bash
uv run --with-requirements backend/requirements.txt --with pytest python -m pytest tests/test_memory_readside_core.py tests/test_hosted_memory_tools.py tests/test_hosted_memory_tool_loop.py tests/test_memory_readside.py tests/test_enclave_routeb_readside.py tests/test_model_api_path.py -q
```

结果：

```text
53 passed
```

已跑：

```bash
uv run --with-requirements backend/requirements.txt --with pytest --with requests python -m pytest tests/test_model_api_prompts.py tests/test_provider_client.py tests/test_tool_loop_v2.py tests/test_context_memories.py tests/test_memory_index_selector.py tests/test_multi_tenant_isolation.py -q
```

结果：

```text
117 passed
```

已跑：

```bash
uv run --with-requirements backend/requirements.txt --with pytest --with requests python -m pytest tests --ignore=tests/test_api.py -q
```

结果：

```text
636 passed, 3 subtests passed
```

没有跑通的：

```bash
uv run --with-requirements backend/requirements.txt --with pytest --with requests python -m pytest tests -q
```

原因：

```text
tests/test_api.py 在 collection 阶段直接请求 localhost:5001。
本地没有启动完整 HTTP 服务，所以连接被拒绝。
```

人话：自动化测试基本过了；唯一没跑的是“需要你本地先起服务”的老式 API 脚本，不是这次 memory tools 的单测失败。

## 11. 还没做的事

本轮还没做：

```text
prod eval probe 集
test 环境真机 trace 验收
线上灰度
原生 function calling
embedding 召回进窗
MemoryCard 写入规则重做
Garden UI 改造
```

下一步建议：

```text
1. 合到 test 分支或发 test 环境。
2. 打开 MODEL_API_MEMORY_TOOLS_ENABLED=true。
3. 保持 MODEL_API_AUTO_MEMORY_CONTEXT_ENABLED=true 作为兜底。
4. 用“猫叫武松”这类真实账号记忆做真机冒烟。
5. 看 action_trace 里的 memory_tools.mode / index_called / fetch_called / fallback_reason。
```
```

## 9. 风险和处理

### 风险 1：模型不按 JSON tool_calls 输出

处理：

```text
prompt 强约束工具调用格式
parser 兼容 tool_calls / tools / function-like 结构
失败时 fallback 到普通回复
```

### 风险 2：延迟变长

处理：

```text
max_iters=3
tool timeout 控制
只在 agent 主动调用时走 memory
默认不每轮强制查 memory
```

### 风险 3：memory 重复进入上下文

处理：

```text
agent_tools 开启时，默认关闭 auto_memory_context
短期可用开关双开，但测试必须区分
```

### 风险 4：agent fetch 太多正文

处理：

```text
服务端硬限制 max 5
超出截断，并在 trace 里记录 capped=true
```

### 风险 5：敏感内容被过度取出

处理：

```text
index 只给粗粒度 is_sensitive
fetch 默认不展开敏感细节，除非 query 明确相关
后续可以加 sensitive_scope gating
```

## 10. 我建议的最小可上线范围

本轮只做：

```text
hosted foreground chat memory.index tool
hosted foreground chat memory.fetch tool
foreground chat tool loop max_iters=3
agent_tools / auto_context 两个开关
trace 可观测
test 环境启用 agent_tools
```

不做：

```text
重写 MemoryCard v1 写入
改 Garden UI
做本地小模型 rerank
做复杂敏感策略
删除现有自动 recall
改 VPS / MCP 已有工具
```

## 11. 最终产品效果

用户不会直接看到一个新按钮。

用户能感知到的是：

```text
当用户问过去的事，agent 更像“自己想起来去查记忆”
而不是服务端每次提前把一堆记忆塞给它
```

测试者能看到的是：

```text
trace 里有 memory.index
trace 里有 memory.fetch
fetch 的 memory 被喂回第二/第三轮模型调用
最终回复使用了那条 memory
```

人话：这次不是做 UI，而是把“记忆”从后端自动拼 prompt，升级成 agent 真正可调用的能力。

## 12. 给 CC 的 review 点

请重点 review：

1. Foreground chat 是否应该复用 `proactive.tool_loop_v2.run_tool_loop_v2`，还是单独实现轻量 loop？
2. `memory_tools.py` 放在 `model_api_runtime` 是否合适？
3. `MODEL_API_MEMORY_TOOLS_ENABLED` / `MODEL_API_AUTO_MEMORY_CONTEXT_ENABLED` 两个开关是否够清楚？
4. index 是否应该完全不做相关性筛选，只做安全过滤和 top 50 排序？
5. fetch 单次 cap 5 是否合适？
6. trace 返回结构是否足够产品测试？
7. 是否需要保留当前 `model_api_readside_v1` 作为 fallback？
