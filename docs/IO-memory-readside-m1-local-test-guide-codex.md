# IO Memory Readside M1 本地测试说明（Codex）

## 结论

这份文档用于测试 IO Memory Readside M1：agent 先看记忆目录 `index`，再按 id 取正文 `fetch`。

人话：以前 agent 像是在一堆完整记忆里硬翻；现在先看“目录摘要”，觉得哪条有用，再打开那条记忆正文。

## 这轮用户/产品能感知到什么

这轮主要不是 UI 变化，而是 agent 使用记忆的方式变化。

- `index`：只返回安全摘要，不返回用户原话、亲密/XP 细节、具体 sensitive_scope。
- `fetch`：agent 命中某条记忆后，按 id 取完整正文。
- 旧的 Memory Garden 接口不动，iOS 现有展示不应该坏。
- 本地 Docker 沙箱可以复用，后续测试 readside、insert、supersede 都可以沿用这套方式。

人话：用户不一定马上在界面上看到新按钮，但 agent 以后会更像“先翻目录，再打开卡片”，不是一次性把所有隐私内容都摊开。

## 本地 Docker 沙箱测法

在项目根目录执行：

```bash
cd /Users/hx/Projects/io/feedling-mcp
uv run --with-requirements backend/requirements.txt python tools/memory_readside_docker_e2e.py
```

这个命令会自动做这些事：

1. 启动本地 Postgres、backend、enclave。
2. 用 dev-only key provider 启动 enclave，不需要 Phala simulator。
3. 创建一个本地测试用户。
4. 写入几条加密 memory。
5. 调用 `POST /v1/memory/index`。
6. 调用 `POST /v1/memory/fetch`。
7. 打印产品视角验收结果。

如果想跑完自动清理容器：

```bash
uv run --with-requirements backend/requirements.txt python tools/memory_readside_docker_e2e.py --down
```

## 看到什么算通过

输出里重点看三段：

```text
=== 1. index: agent 先看到的安全摘要目录 ===
```

这里应该能看到多条 memory 摘要，例如“用户情绪崩溃时，先需要被陪着和确认感受”。这里不应该看到 `verbatim`、`her_quote`、`follow_up`、具体 `sensitive_scope`。

```text
=== 2. fetch: agent 命中后拿到的完整正文 ===
```

这里应该能看到按 id 取回来的正文，例如 `verbatim` 和 `follow_up`。

```text
=== 3. 产品验收结论 ===
index_no_raw_quote=PASS
missing_ids=[]
unavailable_ids=[]
```

人话：`index_no_raw_quote=PASS` 是关键，说明目录里没有泄露原话；`fetch_count > 0` 说明正文能按需取到。

## iOS 怎么连本地沙箱

脚本会打印：

```text
Local sandbox api_key: ...
iOS self-hosted API URL: http://127.0.0.1:5001
```

如果 iOS 模拟器在同一台 Mac 上，可以尝试用：

```text
http://127.0.0.1:5001
```

如果是真机，需要把 `127.0.0.1` 换成 Mac 的局域网 IP，例如：

```text
http://192.168.x.x:5001
```

然后在 iOS 的 self-hosted/API 设置里填：

- API URL：本地 backend 地址。
- API Key：脚本打印的 `Local sandbox api_key`。

注意：这套本地沙箱是一次性测试环境。加 `--down` 会清掉容器；不加 `--down` 会保留服务，方便 iOS 继续连。

## 为什么不需要 Phala simulator

沙箱 compose 里只给 enclave 设置了：

```text
FEEDLING_DEV_DSTACK_SEED=feedling-memory-readside-sandbox-v1
```

这会走 dev-only deterministic key provider。生产/test 环境不会设置这个变量，所以仍然走真实 dstack KMS。

人话：本地测试不需要真的模拟 TEE，只要能稳定生成同一套测试密钥，把“加密写入、解密读取、index/fetch 流程”跑通。

## 后续继续测什么

M1 readside 通过后，下一步建议按这个顺序推进：

1. 和 zhihao 对齐后端接口字段是否接受。
2. 接 `insert`，让新记忆能按 MemoryCard v1 写入。
3. 接 `supersede`，让旧记忆能被新记忆替换/降权。
4. 再让 agent 的 recall 真正从旧 list 改成 `index -> fetch`。
5. 最后再做测试环境联调和小范围真用户测试。

人话：现在证明“读记忆”能跑通；下一步要证明“一条新记忆从写入到被 agent 用上”能完整闭环。

## route A / MCP 怎么测

这次还补了 route A / self-hosted agent 可以用的两个 MCP 工具：

```text
feedling_memory_index
feedling_memory_fetch
```

测试方式不是看 iOS UI，而是让 MCP agent 调工具：

```text
1. 先调用 feedling_memory_index(query="猫咪最近不吃饭，我有点担心")
2. 看返回的 items、suggested_ids、selector_trace
3. 再调用 feedling_memory_fetch(ids=[suggested_ids 里的 1-3 个 id])
4. 看是否拿到完整正文
```

你应该看到类似：

```json
{
  "recall_flow": "index_first_fetch_later",
  "items": [
    {
      "id": "mem_cat",
      "summary": "用户担心猫咪生病时，需要先共情再给具体观察建议。",
      "bucket_refs": ["猫咪", "宠物照顾"],
      "is_sensitive": false
    }
  ],
  "suggested_ids": ["mem_cat"],
  "selector_trace": {
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
  }
}
```

然后 fetch 返回类似：

```json
{
  "items": [
    {
      "id": "mem_cat",
      "summary": "用户担心猫咪生病时，需要先共情再给具体观察建议。",
      "verbatim": "猫咪今天不怎么吃饭，我有点慌。",
      "follow_up": "先安抚情绪，再建议观察饮水、精神状态和是否持续拒食。"
    }
  ],
  "missing_ids": [],
  "unavailable_ids": []
}
```

人话：你要看的不是页面变了，而是 agent 终于能展示“我先看了哪些目录、建议打开哪些记忆、最后打开了哪些正文”。

## 当前边界

这次 route A 补的是 MCP 工具入口。

还没有做：

```text
自动把主 chat recall 切到 index -> fetch
自动更新公开 io-onboarding skill.md
自动改 memory 写入格式
```

人话：明早 test 可以验证新按钮是否可用；但不要期待普通聊天已经 100% 自动走新链路。
