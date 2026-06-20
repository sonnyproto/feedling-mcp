# IO Memory Readside M1 Plan 演进与代码交接（Codex）

> 日期：2026-06-20  
> 作者：Codex 整理  
> 当前分支：`feat/memory-readside-m1`  
> 目标读者：Claude Code / 其他协作 agent / zhihao / Seven  
> 重点：说明这个 plan 是怎么一步步演进到当前 M1 readside 的，以及本轮代码到底改了什么。

---

## 0. 一句话结论

这轮最终没有做“完整 IO Memory v1”，也没有改写入记忆，而是收敛成 **IO Memory Readside M1**：

```text
先把 agent 读记忆的链路做成两步：
index 看安全摘要目录
fetch 按 id 取完整正文
```

人话：这轮先让 agent 能安全地“翻目录、再打开卡片”，不是一上来重做记忆写入、整理、衰减、合并和 UI。

---

## 1. 当前状态

### 已实现

- 新增 `POST /v1/memory/index`。
- 新增 `POST /v1/memory/fetch`。
- backend 做候选预筛和权限校验。
- enclave 解密候选 memory，并生成 index/fetch 结果。
- index 不返回用户原话、`follow_up`、`sensitive_scope`。
- fetch 才返回完整正文。
- 新增本地 Docker 沙箱：Postgres + backend + enclave。
- 新增真实加密 e2e：创建本地用户、写入加密 memory、调用 index、调用 fetch、打印 trace。
- 新增 dev-only enclave key provider，方便本地 Docker 不依赖 Phala simulator。

### 没有实现

- 没有改 memory 写入。
- 没有实现 MemoryCard v1 insert。
- 没有实现 supersede / merge / contradict / decay。
- 没有改 iOS UI。
- 没有改 Memory Garden 展示。
- 没有把主聊天 recall 完整切到 index/fetch。
- 没有做 eval 自动化。
- 没有完整收口 route A。

人话：底座已经铺出来，但主路上的车还没全部切过去。

---

## 2. Plan 是怎么演进的

### 阶段 1：从“IO 记忆系统整体方向”开始

最开始讨论的是完整 IO 记忆系统，包括：

- 人机恋关系记忆。
- 情绪和亲密关系边界。
- 敏感 / XP / 隐私内容怎么记。
- agent 该怎么提取、整理、使用记忆。
- eval 题库怎么设计。
- MemoryCard v1 数据结构。
- 旧 Memory Garden 怎么兼容。

这个阶段的核心判断是：

```text
IO memory 不是普通事实库，而是长期关系里的“关系连续性系统”。
```

人话：它不是只记“用户喜欢猫”，还要记“用户什么时候需要陪伴、什么边界不能越、怎么说话更像长期伴侣”。

相关文档：

- `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-记忆-背景-迭代-定稿.md`
- `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-记忆-新对话上下文交接.md`
- `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-memory-core-产品价值说明.md`

### 阶段 2：曾经考虑先做 eval，但暂时搁置

一开始有一个方向是先做 eval，用题目和答案来验证：

- 什么样的记忆该被提取。
- 什么样的记忆不该被提取。
- 人机恋里的情感连续性怎么评估。
- 隐私、亲密、XP 边界怎么评估。
- recall 后模型回答是否真的更贴合用户。

这个方向和 Seven 的分工有关：

```text
Z 负责架构和评估框架。
Seven 负责找真实案例 / 题目素材。
```

后来判断：eval 很重要，但不是当前最快的工程产出。  
所以先暂停 eval，把当前主线切到工程 readside。

人话：考试题要做，但得先有一条可以被考试的链路。

相关文档：

- `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-Memory-Eval-v0-交接(草稿+分工).md`
- `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-Memory-Eval-v0-人机恋关系记忆题目答案草稿.md`

### 阶段 3：Codex 版和 Claude 版 spec 对齐

中间出现过两版工程 spec：

- Claude 版更偏“完整 memory core”，包括 commit / index / fetch / decay / bucket resolve。
- Codex 版更偏“线上兼容约束”，强调现有 `memory_moments.doc(JSONB)`、加密 envelope、Memory Garden、enclave 边界。

最后合成了联合 spec：

```text
Claude 版负责“核心系统应该怎么完整运转”。
Codex 版负责“它必须接得上现在的线上系统”。
```

人话：Claude 版像目标系统蓝图，Codex 版像落地时别把线上房子拆了。

相关文档：

- `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-memory-core-工程spec-Claude版.md`
- `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-MemoryCard-v1-Codex-工程契约草案.md`
- `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-memory-core-v1-联合工程spec.md`

### 阶段 4：和 zhihao 对齐前，先整理后端边界问题

进入后端之前，先整理了一份给 zhihao 的问题清单，核心是确认 readside 该怎么落地。

当时需要确认的问题：

- `index()` 应该放在 backend 还是 enclave？
- 第一版是否不新增表，只扩 JSONB / envelope？
- `MemoryIndexItem` 可以返回哪些字段？
- `fetch(ids)` 怎么处理权限、missing、unavailable？
- route A 是否接同一套 index/fetch？
- 第一版是否先不做 commit？

当时倾向：

```text
index 放 enclave，因为 summary/title/description 都在密文里。
第一版不新增表，不迁移历史数据。
index 不返回原话，只返回摘要。
fetch 只取当前用户自己的 active memory。
M1 先做 readside，不做 commit。
```

人话：先让后端知道我们要的是“安全读记忆”，不是让它立刻重做完整记忆系统。

相关文档：

- `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-memory-readside-zhihao-backend-questions.md`

### 阶段 5：zhihao 回复后，M1 范围进一步收敛

根据 zhihao 回复和后续确认，M1 收敛成：

```text
可以有两个接口。
top-N 先用 50。
先做 readside。
暂不做 insert / supersede / merge / decay。
```

最终接口：

```text
POST /v1/memory/index
POST /v1/memory/fetch
```

M1 的定义：

```text
MemoryMoment -> MemoryIndexItem
MemoryMoment -> MemoryFetchResult
```

人话：先把“目录能看、正文能取、旧卡能兼容、隐私不炸”证明出来。

相关文档：

- `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-memory-v1-M1-zhihao-plan.md`

### 阶段 6：Codex 开始执行工程实现

进入 `feedling-mcp` 后，当前分支切到：

```text
feat/memory-readside-m1
```

实现策略：

- 不改表。
- 不迁移历史数据。
- 不改旧 Memory Garden 接口。
- 新增两个 readside API。
- backend 只做候选筛选和权限判断。
- enclave 解密正文，并生成 index/fetch。
- 用 Docker 沙箱证明本地完整链路。

人话：在最小范围里做一个能跑、能测、能讲清楚数据流的版本。

---

## 3. 和 Seven 沟通时应该怎么说

Seven 更关心方向和产品价值，不需要陷入接口细节。

建议说法：

```text
我们先没有做完整 memory v1，也没有先做 eval。
当前先把 readside M1 打通：agent 先看记忆目录，再按 id 取正文。
这样后续做情感陪伴、亲密边界、XP 隐私记忆时，不会每次把完整隐私正文全摊给模型，而是先用安全摘要做判断。
```

对 Seven 的关键解释：

- memory 对人机恋不是“事实收藏夹”，而是关系连续性。
- 但关系连续性不能靠一次性塞全部记忆给模型。
- 所以第一步是把 recall 变成可控链路：`index -> fetch`。
- eval 仍然重要，后续 Seven 找真实案例后，可以用来评估：
  - 该不该记。
  - 记成什么。
  - 什么时候该召回。
  - 召回后回答是否更像长期伴侣。

人话：先把“记忆怎么被安全拿出来用”做出来，再考它是不是用得好。

---

## 4. 和 zhihao 沟通时应该怎么说

zhihao 更关心后端边界和实现可控性。

建议说法：

```text
M1 只做 readside，不做写入。
backend 负责鉴权、user_id 校验、候选预筛和排序。
enclave 负责解密正文并生成 index/fetch。
第一版不新增表，不迁移历史数据，不破坏 Memory Garden。
```

需要 zhihao 重点 review：

- `POST /v1/memory/index` 的字段是否合适。
- `POST /v1/memory/fetch` 的失败语义是否合适。
- top 50 排序逻辑是否接受。
- backend 是否应该透传 enclave index/fetch，还是内部接口再调整。
- `include_sensitive` 的默认行为后续是否要收紧。
- 主 chat recall 什么时候切到新 readside。

人话：这轮先不让 zhihao 背完整 memory core 的复杂度，只让他 review“读记忆接口边界”。

---

## 5. 当前 M1 真实数据流

以“猫咪”问题为例：

```text
用户问：你还记得我喜欢什么样的猫吗？
```

agent 可能理解为：

```text
关键词：猫 / 猫咪 / cat / 喵喵 / 宠物
```

目标链路：

```text
用户消息
  -> backend / agent 判断需要 recall memory
  -> POST /v1/memory/index
  -> backend 从 memory_moments 筛 top 50 候选
  -> enclave 解密候选正文
  -> enclave 返回安全 index
  -> agent 判断哪些 index 命中
  -> POST /v1/memory/fetch
  -> enclave 解密命中的完整正文
  -> backend / agent 拼进 prompt
  -> LLM 回复用户
```

index 返回例子：

```json
{
  "items": [
    {
      "id": "mem_cat_001",
      "summary": "用户喜欢圆脸、黏人、安静一点的猫，尤其偏爱橘猫和布偶。",
      "bucket_refs": ["宠物偏好", "猫咪"],
      "status": "active",
      "salience": "medium",
      "is_open_thread": false,
      "is_sensitive": false,
      "score": 0.86
    },
    {
      "id": "mem_cat_002",
      "summary": "用户把猫叫作“喵喵”，看到猫会明显放松，适合用猫咪话题做轻度安抚。",
      "bucket_refs": ["情绪安抚", "猫咪"],
      "status": "active",
      "salience": "high",
      "is_open_thread": true,
      "is_sensitive": false,
      "score": 0.93
    }
  ]
}
```

fetch 请求：

```json
{
  "ids": ["mem_cat_002", "mem_cat_001"]
}
```

fetch 返回例子：

```json
{
  "items": [
    {
      "id": "mem_cat_002",
      "summary": "用户把猫叫作“喵喵”，看到猫会明显放松，适合用猫咪话题做轻度安抚。",
      "verbatim": "我看到那种慢吞吞趴着的喵喵会很放松。",
      "bucket_refs": ["情绪安抚", "猫咪"],
      "status": "active",
      "salience": "high",
      "follow_up": "用户紧张时，可以轻轻用猫咪画面或猫咪表情做安抚，但不要强行转移话题。",
      "context": "来自一次用户聊压力和宠物偏好的对话。",
      "source_type": "chat",
      "is_sensitive": false
    }
  ],
  "missing_ids": [],
  "unavailable_ids": []
}
```

人话：

```text
index 是目录，不给原话。
fetch 是打开正文，只打开命中的几条。
```

---

## 6. 加密 / 解密边界

数据库里的 memory 仍然是 envelope：

```json
{
  "id": "mem_cat_001",
  "owner_user_id": "usr_123",
  "visibility": "shared",
  "status": "active",
  "salience": "medium",
  "importance": 0.72,
  "body_ct": "encrypted-body...",
  "nonce": "nonce...",
  "K_user": "sealed-key-for-user...",
  "K_enclave": "sealed-key-for-enclave..."
}
```

backend 能看：

```text
id
owner_user_id
visibility
status
salience
importance
created_at / updated_at / occurred_at
有没有 K_enclave
```

backend 不能看：

```text
summary
verbatim
follow_up
context
sensitive_scope
用户原话
```

enclave 负责：

```text
用 K_enclave 解出正文密钥
用正文密钥解 body_ct
生成 index 或 fetch 正文
```

人话：backend 负责找盒子，enclave 负责打开盒子。

---

## 7. top 50 是怎么来的

M1 的 top 50 不是数据库随便前 50 条。

backend 会先过滤：

```text
只查当前 user_id
默认 active
排除 local_only
排除没有 K_enclave 的 memory
排除 archived / deleted / superseded
```

再排序：

```text
is_open_thread 优先
salience 高优先
importance 高优先
last_active / updated_at / occurred_at / created_at 新优先
id 兜底稳定排序
```

人话：优先拿“还没结束的、高重要性、最近活跃”的记忆来生成目录。

---

## 8. LLM 调用次数的结论

这个新链路不要求每次走两次 LLM。

推荐 M1：

```text
用户消息
  -> recall 用规则 / keyword / embedding / agent selection
  -> fetch 正文
  -> 最终 LLM 回复
```

不推荐 M1 默认：

```text
LLM 先 query rewrite
LLM 再选 index
LLM 再最终回复
```

人话：先别把 M1 做复杂。`index/fetch` 是工程底座，LLM 是否参与 query rewrite 是下一层优化。

---

## 9. 本轮代码改动

### 9.1 `backend/app.py`

新增 readside backend 逻辑：

- `_MEMORY_READSIDE_LIMIT = 50`
- `_MEMORY_READSIDE_SALIENCE_WEIGHT`
- `_MEMORY_READSIDE_INACTIVE_STATUSES`
- `_memory_readside_status(...)`
- `_memory_readside_salience(...)`
- `_memory_readside_float(...)`
- `_memory_readside_time_key(...)`
- `_memory_readside_available(...)`
- `_memory_readside_score(...)`
- `_memory_readside_candidates(...)`
- `_memory_readside_post_enclave(...)`
- `POST /v1/memory/index`
- `POST /v1/memory/fetch`

行为：

- `index`：
  - `require_user()`
  - 读取当前用户 memory。
  - backend 预筛 top 50。
  - 调 enclave `/v1/memory/index`。
  - 返回 enclave 生成的 index items。

- `fetch`：
  - 校验 ids。
  - 只允许当前用户 memory。
  - 找不到放 `missing_ids`。
  - 不可读放 `unavailable_ids`。
  - 调 enclave `/v1/memory/fetch`。
  - 按输入 ids 顺序返回正文。

人话：backend 现在有了两个新门：一个看目录，一个取正文。

### 9.2 `backend/enclave_app.py`

新增 dev-only key provider：

- `FEEDLING_DEV_DSTACK_SEED`
- `_dev_seed_bytes(...)`
- `derive_keys_from_dev_seed(...)`
- `dev_attestation(...)`
- `bootstrap()` 支持 dev seed。
- `_get_or_derive_content_sk()` 优先复用启动时缓存的私钥。

原因：

```text
本地 Docker 沙箱不应该依赖 Phala simulator。
生产/test 不设置 FEEDLING_DEV_DSTACK_SEED，仍然走真实 dstack KMS。
```

新增 readside enclave 逻辑：

- `_memory_readside_text(...)`
- `_memory_readside_list(...)`
- `_memory_readside_status(...)`
- `_memory_readside_salience(...)`
- `_memory_readside_is_sensitive(...)`
- `_build_memory_index_item(...)`
- `_build_memory_fetch_item(...)`
- `_memory_readside_auth_context(...)`
- `_memory_readside_decrypt_items(...)`
- `POST /v1/memory/index`
- `POST /v1/memory/fetch`

行为：

- index 解密正文后只返回：
  - `id`
  - `summary`
  - `bucket_refs`
  - `status`
  - `salience`
  - `is_open_thread`
  - `is_sensitive`
  - `score`

- index 不返回：
  - `verbatim`
  - `her_quote`
  - `follow_up`
  - `sensitive_scope`

- fetch 返回：
  - `id`
  - `summary`
  - `verbatim`
  - `bucket_refs`
  - `status`
  - `salience`
  - `follow_up`
  - `context`
  - `source_type`
  - `is_sensitive`

人话：enclave 现在能打开密文，但 index 只吐安全摘要，fetch 才吐正文。

### 9.3 Docker 沙箱

新增：

- `deploy/docker-compose.memory-sandbox.yaml`

包含：

- `postgres`
- `backend`
- `enclave`

关键设计：

```text
backend image: feedling-memory-sandbox-backend:dev
enclave image: feedling-memory-sandbox-enclave:dev
```

曾经踩过的坑：

```text
backend 和 enclave 如果共用同一个 image name，Docker build 会撞。
```

已修正为两个不同 image name。

### 9.4 工具脚本

新增：

- `tools/memory_readside_sandbox.py`
- `tools/memory_readside_smoke.py`
- `tools/memory_readside_docker_e2e.py`

用途：

- `memory_readside_sandbox.py`
  - 不起后端。
  - 用固定样例展示 index/fetch 产品形态。

- `memory_readside_smoke.py`
  - 可对真实 backend + API key 做 smoke。

- `memory_readside_docker_e2e.py`
  - 起 Docker 沙箱。
  - 创建本地用户。
  - 写入加密 memory。
  - 调 index。
  - 调 fetch。
  - 打印 trace。

trace 里会展示：

```text
当前用户问题是什么
backend 怎么筛候选
enclave 怎么生成 index
agent 看到了哪些 index
agent 选择了哪些 ids
fetch 返回了哪些正文
```

### 9.5 测试

新增：

- `tests/test_memory_readside.py`
- `tests/test_memory_readside_sandbox.py`
- `tests/test_memory_readside_docker_e2e.py`
- `tests/test_enclave_dev_seed.py`
- `tests/test_memory_index_selector.py`

覆盖：

- index 不泄露 forbidden fields。
- index/fetch 基本形状。
- backend readside 过滤和排序。
- dev seed 不走 DstackClient。
- Docker e2e 脚本基础逻辑。
- selector 能从 index summary / bucket_refs 里选 ids。
- 普通 query 默认不打开 sensitive index。
- 泛词如 `project` 不应误召回专有记忆。

已跑过：

```text
8 passed
```

### 9.6 文档

新增：

- `docs/IO-memory-readside-m1-local-test-guide-codex.md`
- `docs/IO-memory-readside-M1-plan-evolution-and-code-handoff-codex.md`

当前这份就是第二个。

### 9.7 MemoryIndexSelector

新增：

- `backend/memory_index_selector.py`

职责：

```text
query + MemoryIndexItem[]
  -> selected_ids
  -> trace.selected / trace.skipped_sample
```

第一版策略：

- 复用 `context_memory_selection.memory_relevance_details(...)` 的中英文相关性算法。
- 通过 adapter 把 `MemoryIndexItem.summary / bucket_refs` 转成旧算法能理解的 `title / description / linked_dimension`。
- 不使用 `verbatim / her_quote / follow_up / context`，因为 index 阶段不应该接触正文。
- 普通 query 默认跳过 `is_sensitive=true`。
- query 明确包含“隐私 / 私密 / 敏感 / 亲密 / XP / kink / private”等词时，才允许 sensitive index 进入选择。
- 增加 index 专用 topic guard，避免只因为“喜欢 / project / memory / server”这类泛词就 fetch 正文。

人话：它是“从 50 条目录里挑哪几条正文”的第一版算法，不是 LLM reranker。

---

## 10. 本地怎么验证

完整 Docker e2e：

```bash
cd /Users/hx/Projects/io/feedling-mcp
uv run --with-requirements backend/requirements.txt python tools/memory_readside_docker_e2e.py --trace-query '我不是服务端，我想知道这次 memory 改动真实发生了什么，数据怎么流动。'
```

如果跑完自动清理：

```bash
uv run --with-requirements backend/requirements.txt python tools/memory_readside_docker_e2e.py --down --trace-query '我不是服务端，我想知道这次 memory 改动真实发生了什么，数据怎么流动。'
```

看输出：

```text
=== 0. trace: 这次 agent 读 memory 的真实数据流 ===
=== 1. index: agent 先看到的安全摘要目录 ===
=== 2. fetch: agent 命中后拿到的完整正文 ===
=== 3. 产品验收结论 ===
```

通过标准：

```text
index_count > 0
fetch_count > 0
index_no_raw_quote=PASS
missing_ids=[]
unavailable_ids=[]
```

人话：只要 trace 能看到“先 index、再 fetch”，并且 index 没原话，这条 readside 链路就成立。

---

## 11. 当前分支状态和注意事项

当前分支：

```text
feat/memory-readside-m1
```

当前有未提交改动：

```text
backend/app.py
backend/enclave_app.py
deploy/docker-compose.memory-sandbox.yaml
docs/IO-memory-readside-m1-local-test-guide-codex.md
docs/IO-memory-readside-M1-plan-evolution-and-code-handoff-codex.md
tests/test_enclave_dev_seed.py
tests/test_memory_readside.py
tests/test_memory_readside_docker_e2e.py
tests/test_memory_readside_sandbox.py
tools/memory_readside_docker_e2e.py
tools/memory_readside_sandbox.py
tools/memory_readside_smoke.py
```

注意：

```text
AGENTS.md 是未跟踪文件，不属于本次改动，不要误提交。
```

---

## 12. 给下一个 agent 的建议

### 先读这些文档

按顺序：

1. `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-记忆-背景-迭代-定稿.md`
2. `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-memory-core-v1-联合工程spec.md`
3. `/Users/hx/Projects/io/feedling-mcp-ios/Docs/IO-memory-v1-M1-zhihao-plan.md`
4. `/Users/hx/Projects/io/feedling-mcp/docs/IO-memory-readside-m1-local-test-guide-codex.md`
5. `/Users/hx/Projects/io/feedling-mcp/docs/IO-memory-readside-M1-plan-evolution-and-code-handoff-codex.md`

### 再看代码

重点看：

```text
backend/app.py
  /v1/memory/index
  /v1/memory/fetch

backend/enclave_app.py
  /v1/memory/index
  /v1/memory/fetch
  FEEDLING_DEV_DSTACK_SEED

tools/memory_readside_docker_e2e.py
```

### 不要误解当前范围

当前不是完整 memory v1。

不要把下面这些当作已经完成：

```text
insert
supersede
merge
decay
eval
主 chat recall 已切换
route A 已接入
```

当前完成的是：

```text
readside M1 接口和本地可测闭环
```

---

## 13. 下一步建议

### 技术下一步

1. 让 CC / zhihao review 当前 readside M1 代码。
2. 确认 API response 字段是否最终接受。
3. 确认主 chat recall 何时接入 `index -> fetch`。
4. 如果 readside OK，再做 `insert`。
5. `insert` OK 后再做 `supersede`。
6. 最后恢复 eval，用 Seven 的真实案例验证效果。

### 产品下一步

给 Seven 看时，不要说“我们已经完成 memory v1”。  
应该说：

```text
我们先完成了 memory recall 的安全读底座。
下一步才是新记忆怎么写入、怎么替换旧记忆、怎么用真实题库评估。
```

人话：现在是把“怎么读”修清楚了，后面再修“怎么写”和“怎么判断写得好”。

---

## 14. 2026-06-21 计划修正：CC review 后的下一步

### 14.1 CC review 的核心结论

Claude Code review 认可 readside M1 方向：

```text
readside M1 是低风险底座，可以保留。
但它本身不是“召回更准”的完整方案。
```

CC 提醒了 3 个重点：

1. **eval 是切主聊天链路的闸门。**
   - 现在 readside 只是铺铁轨，没切主链路，所以没 eval 可以接受。
   - 一旦要把主 chat recall 真切到 `index -> fetch`，必须先有最小 eval。

2. **route A / route B 价值不同。**
   - route B / API 形式用算法 pick，覆盖稳定，但精准不一定提升，主要价值是减负、控隐私、可观测。
   - route A / 自建 VPS + agent skill 可以让 agent 语义挑 index，质量上限更高，但 best-effort，不保证每轮都查。

3. **当前 top 50 是查询无关的。**
   - 现在 backend 只按 metadata 取 top 50：open_thread、salience、importance、时间。
   - 用户 memory 很多时，相关旧卡可能排到第 51 条，index 永远看不到。
   - M2 前需要补“按 query 相关性预筛”或 embedding。

人话：M1 不是错，而是只完成了底座。下一步要补“从 50 条目录里选哪几条”的 selector，并且切主链路前要能测试。

### 14.2 当前重新排序后的优先级

不建议马上做：

```text
insert
supersede
merge
decay
完整 MemoryCard v1 写入
```

当前更应该先做：

```text
1. MemoryIndexSelector：从 index_items 里选 selected_ids。
2. 最小 eval：对比老 recall vs 新 index selector。
3. route A skill：让自建 agent 能走 index -> pick -> fetch。
```

人话：别急着造更多车厢，先确认这条新铁轨上的“换轨器”能用。

更新：Codex 已在本分支新增 `backend/memory_index_selector.py` 作为第一版换轨器。它还没有切进主 chat recall，只用于本地测试、trace 和后续集成准备。

### 14.3 API route 的执行口径

API / route B 第一版不建议默认再调一次 LLM 来选 index。

推荐：

```text
用户消息
  -> /v1/memory/index
  -> MemoryIndexSelector 算法选择 3-8 个 ids
  -> /v1/memory/fetch
  -> 拼 prompt
  -> LLM 回复
```

原因：

- 少一次 LLM 调用。
- 延迟和成本更可控。
- 结果更稳定。
- 没有 eval 前，不应该引入更难解释的 LLM rerank。

这条路的产品定位：

```text
更安全、更省、更可 debug。
不承诺精准度立刻提升。
```

人话：API route 先做稳定保底，不把“变聪明”吹过头。

### 14.4 route A / 自建 VPS 的执行口径

route A 更适合 agentic recall：

```text
agent 调 memory.index
agent 读 index 摘要
agent 自己判断选哪些 ids
agent 调 memory.fetch
agent 用 fetch 正文回答
```

这里可以允许 LLM 参与选择，因为：

- 这是用户自己的 agent runtime。
- 不占官方服务器 LLM 成本。
- 更符合 skill/tool 使用方式。
- 语义判断上限更高。

但要接受：

```text
best-effort，不保证每轮都查。
```

人话：route A 像让一个聪明助理自己翻目录；route B 像服务端固定流程帮你挑几条。

### 14.5 MemoryIndexSelector 第一版怎么做

第一版 selector 建议复用旧的 `context_memory_selection.py` 核心相关性算法，但不能原封不动复用整个选择策略。

可复用的部分：

- 中英文 phrase/entity 匹配。
- generic term 过滤。
- weak term 过滤。
- `model_api` strict 模式。
- trace 结构。

不能直接复用的原因：

旧算法吃的是完整明文 memory：

```text
title
description
her_quote
context
linked_dimension
```

新 readside index 只有：

```text
summary
bucket_refs
status
salience
is_open_thread
is_sensitive
score
```

所以需要 adapter：

```text
MemoryIndexItem
  -> selector memory shape
  -> context_memory_selection.memory_relevance_details(...)
  -> selected_ids + trace
```

人话：旧算法能当发动机，但输入要从“正文卡片”换成“目录卡片”。

### 14.6 切主链路前置条件

主 chat recall 不能直接切。

前置条件：

```text
1. MemoryIndexSelector 有单元测试。
2. 至少 10 道 probe eval，能对比 old recall vs new index selector。
3. trace 能展示：
   - index 返回了哪些 items
   - selector 选了哪些 ids
   - 为什么跳过某些 item
   - fetch 打开了哪些正文
4. 敏感 memory 默认不因普通 query 被打开。
```

人话：没考试不要换主路，先小样本证明不比旧路差。

## 15. 2026-06-21 route A / MCP 补充

### 15.1 为什么要补 route A

上一版 M1 已经有后端接口：

```text
POST /v1/memory/index
POST /v1/memory/fetch
```

但 route A / self-hosted agent 侧还没有直接可调用的 MCP 工具。也就是说：

```text
后端有新接口
agent 还没有新遥控器
```

所以这次补了两个 MCP tool：

```text
feedling_memory_index
feedling_memory_fetch
```

人话：之前像是仓库里已经有“目录”和“正文接口”，但自建 agent 手上还没有按钮。这次把按钮补上。

### 15.2 feedling_memory_index 做什么

`feedling_memory_index` 调用后端 `/v1/memory/index`，返回最多 50 条安全摘要。

如果传入 `query`，MCP server 会额外跑 `MemoryIndexSelector`，输出：

```json
{
  "items": [
    {
      "id": "mem_cat",
      "summary": "用户担心猫咪生病时，需要先共情再给具体观察建议。",
      "bucket_refs": ["猫咪", "宠物照顾"],
      "status": "active",
      "salience": "high",
      "is_open_thread": true,
      "is_sensitive": false
    }
  ],
  "suggested_ids": ["mem_cat"],
  "selector_trace": {
    "mode": "memory_index_selector_v1",
    "selected": [
      {
        "id": "mem_cat",
        "reason": "topic_supported..."
      }
    ],
    "skipped_sample": [
      {
        "id": "mem_private",
        "reason": "sensitive_not_allowed_for_query"
      }
    ]
  },
  "recall_flow": "index_first_fetch_later"
}
```

人话：agent 先看目录；如果问的是“猫咪”，工具会建议优先打开猫咪相关记忆；普通问题不会自动建议打开私密/敏感记忆。

### 15.3 feedling_memory_fetch 做什么

`feedling_memory_fetch` 接收 index 里选出来的 ids，调用后端 `/v1/memory/fetch`。

它会：

```text
去掉空 id
去掉重复 id
保持输入顺序
返回 items / missing_ids / unavailable_ids
```

人话：agent 不应该一口气打开全部记忆，而是先挑 1-5 条最相关的，再取正文。

### 15.4 这次没有改什么

这次没有改：

```text
memory 写入
主 chat recall 链路
iOS Memory Garden
旧 feedling_chat_get_history 的 context_memories
公开 io-onboarding skill 仓库
```

人话：这是 route A 的新工具入口，不是把线上主回复链路直接换掉。

### 15.5 明早 test 前需要确认

如果要在 test 环境验证 route A 新工具，需要确认：

```text
MCP server 部署包含本分支代码
backend 部署包含 /v1/memory/index 和 /v1/memory/fetch
backend 能访问 FEEDLING_ENCLAVE_URL
测试账号有带 K_enclave 的 memory
```

如果 agent 端依赖公开 skill 文档，而不是只看 MCP tool descriptions，还需要把 `io-onboarding` 的 skill.md 同步更新。

人话：代码里按钮已经补了，但 test 环境要同时部署 backend 和 MCP；如果 agent 只读远程 skill 文档，还要更新那份公开说明。
