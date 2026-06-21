# IO Memory M1.5 · Agent Tools 测试方案

> 2026-06-21 · 作者:Claude(CC) · 配套:`IO-memory-M1.5-agent-tools-CC方案与决策.md`
> 范围:hosted(route B / model_api)把 memory 升级成 agent tools + 降级兜底。**不含**写入/Garden/MemoryCard v1。
> 目标:既证明"agent 真的自己调工具召回"(功能),又证明"不比现状差 + 不漏 + 不泄露 + 任何模型都不丢召回"(质量与安全)。

---

## 0. 分层总览与验收门(gate)

| 层 | 测什么 | 通过门 |
|---|---|---|
| L1 单元 | core / 工具执行器 / 上限 / 安全字段 | 全绿 → 可进 loop 测试 |
| L2 tool loop | 多轮 index→fetch→answer 编排 | 全绿 → 可进降级测试 |
| L3 降级兜底 | no-tool-call 回填(Q1 设计) | 全绿 → **test 可放量** |
| L4 eval 质量 | 召回正确率 / 误召回 / 回归红线 | agent_tools ≥ auto_readside → **prod 灰度前置** |
| L5 e2e | test 环境真机 | 冒烟过 → **prod 放量** |
| L6 回归 | Garden / 写入 / 旧接口 / web_search | 零回归 |
| L7 隐私安全 | 原话不漏 / 敏感门控 / 跨用户 / 日志 | 全绿(硬门) |
| L8 性能 | 往返数 / token / 延迟上限 | 在预算内 |

**总原则:先写测试再改代码(Codex §10)。** 下面每条给出文件位置、用例、断言。

---

## 1. L1 单元测试 · core 与工具执行器

文件:`tests/test_memory_readside_core.py`、`tests/test_hosted_memory_tools.py`

### 1.1 core(`memory_readside_core.py`)
| 用例 | 断言 |
|---|---|
| index 返回安全字段 | 只含 `id/summary/bucket_refs/status/salience/is_open_thread/is_sensitive/score`;**不含** `verbatim/her_quote/follow_up/sensitive_scope` |
| index 状态过滤 | `deleted/archived/superseded/local_only/无 K_enclave` 不进 index |
| index 排序 | 按 `is_open_thread → salience → importance → recency → id` 稳定排序 |
| index top-N | 默认 ≤50;>50 时截断且 trace 记 `user_card_count` |
| fetch 正文 | 返回完整字段(含 verbatim);顺序与入参 ids 一致 |
| fetch 缺失 | 不存在 → `missing_ids`;解不开/local_only/无 K_enclave → `unavailable_ids` |
| 跨用户 | 他人 memory 一律 `missing_ids`(不暴露存在性) |

### 1.2 工具执行器(`model_api_runtime/memory_tools.py`)
| 用例 | 断言 |
|---|---|
| 工具名 | 暴露 `memory_index` / `memory_fetch`(**下划线**,非点号) |
| in-process 调用 | 调 core,**不走 HTTP 自调**(mock 验证无自调 app.py 路由) |
| fetch 单次上限 | ids >5 → 截断到 5,trace `capped=true` |
| fetch 累计上限 | 跨多轮累计 >8 → 拒绝/截断,trace 记录 |
| fetch 去重 | 重复 id 只取一次 |
| 参数非法 | `ids` 非数组 / 空 → 安全报错,不崩 |
| 敏感门控 | `is_sensitive` 项默认不进 index;`include_sensitive=false` 时不可 fetch 敏感正文 |

---

## 2. L2 tool loop 测试 · 多轮编排(脚本模型)

文件:`tests/test_model_api_path.py`(扩展)
做法:用**脚本化 fake model**,按轮次返回预设输出,验证编排,不依赖真实 LLM。

| 用例 | 脚本模型行为 | 断言 |
|---|---|---|
| happy path 3 轮 | 轮1 `memory_index`→轮2 `memory_fetch`→轮3 final | 模型被调 3 次;index/fetch 各执行 1 次;tool 结果喂回下一轮;最终答含该记忆;trace 有 memory_tools |
| 查了目录但无相关 | 轮1 `memory_index`→轮2 final(不 fetch) | 不 fetch;回答**如实说不确定**,不编造 |
| fetch 不存在 id | 轮2 fetch 一个假 id | 进 `missing_ids`;模型据此不编造 |
| 一次想 fetch 很多 | 轮2 fetch 10 个 id | 截断到 5;trace `capped=true` |
| max_iters 触顶 | 模型每轮都要工具、不收口 | 到 `max_iters=3` 优雅出最终答,不死循环 |
| 工具失败 | core 抛错 | loop 不崩;返回降级答;trace 记 `tool_error` |
| 畸形 tool_calls | 模型输出非法 JSON / 缺字段 | parser 容错;无法解析则当普通回复,不崩 |

---

## 3. 降级兜底测试(Q1 设计,重点)

文件:`tests/test_memory_tools_fallback.py`(新增)
目标:证明**任何模型都不会丢召回**(M1.5 prompt-level,唯一兜底 = no-tool-call 回填)。

**M1.5 必测(prompt-level):**
| 用例 | 脚本模型行为 | 断言 trace.mode | 断言行为 |
|---|---|---|---|
| 默认挂工具 | 任意模型 | — | prompt 里默认都带 memory 工具说明 |
| 没调工具+有候选 | 模型不输出 tool_calls;库里有相关记忆 | `fallback` + `fallback_reason=no_tool_call_backfilled` | 跑 selector→注入→**重答一次**;最终答含记忆 |
| 没调工具+无候选 | 模型不输出 tool_calls;库里无相关 | `agent_tools`(无回填) | selector 空→不回填→直接用原答 |
| 正常调工具 | 模型输出 index/fetch tool_calls | `agent_tools` | **不触发回填**(用取回的) |
| 不双塞 | 模型调了工具 | — | 同一轮不再 auto 注入(去重,无重复记忆) |
| 弱模型≈现状 | 模型从不输出 tool_calls | `fallback` | 召回结果与纯 auto_readside **一致**(不回归) |

> 关键覆盖:**弱模型(不按格式调工具)召回率必须 = 现状 auto_readside**(不回归);会调工具的才升级。这就是 hx 要的"工具化后弱模型不失忆"。

**推迟到原生 function calling 阶段(M1.5 不测,设计先存档):** provider 拒绝 tools 参数 / 拒绝阈值不决绝 / 429-5xx 不计数 / 自学跳过+定期重探自愈。prompt-level 下没有 tools 参数可被拒,这组用例届时再补。

---

## 4. L4 eval 质量测试(prod 灰度前置)

文件:`evals/memory_recall_v0/`(probe 集 + 打分脚本)

### 4.1 probe 集(先手写 12–20 条)
每条 6 段:
```text
1. 背景记忆(预置进库的卡:title/summary/verbatim)
2. 用户问题(新会话,清空近期聊天)
3. 期望召回的 memory id(ground truth)
4. 期望回答要点(必须命中的事实)
5. 不该召回 / 不该出现(干扰卡 + 敏感卡)
6. 类型标签(直球 / paraphrase / 关系 / 情绪 / 项目 / 敏感 / 多卡)
```
覆盖分布建议:直球 4、paraphrase 4、关系/情绪 4、敏感 3、多卡/无关干扰 3。

### 4.2 两种 grader
- **code grader(确定性)**:fetch 的 id 是否命中 ground truth(召回正确率 / 误召回率 / 敏感误取率)。
- **model grader(语义)**:最终回答是否用对记忆、有无编造("先说理由再打分" + 关键事实命中)。

### 4.3 对比与红线
| 指标 | 口径 | 红线 |
|---|---|---|
| 召回正确率 | 命中 ground truth 的比例 | **agent_tools ≥ auto_readside** |
| 误召回率 | fetch 了不该取的 | agent_tools ≤ auto_readside |
| 敏感误取率 | 非相关却取敏感正文 | **= 0** |
| 编造率 | 库里没有却声称记得 | **= 0** |
| paraphrase 子集 | 仅 paraphrase 类 | agent_tools **显著高于** auto(这是 agentic 的价值证明) |

> 跑法:同一 probe 集分别在 `agent_tools` 与 `auto_readside` 跑,出对比表。**paraphrase 子集是否提升 = 这次升级到底值不值的核心证据。**

---

## 5. L5 e2e 真机验收(test 环境)

| 用例 | 步骤 | 成功标准 |
|---|---|---|
| 猫(Codex 基线) | 预置"猫叫武松"→清空近期聊天→问"还记得我家猫叫什么、名字怎么来的吗" | trace 出现 `memory_index`+`memory_fetch`;回答正确;`memory_tools.fetch_called=true`(**不是** `context.memory_selection.mode=model_api_readside_v1`) |
| 关系 | 预置一条关系记忆→隔会话提问 | 正确召回并自然引用 |
| 情绪模式 | 预置"崩溃时先要陪伴别给步骤" | 回答风格符合,引用该记忆 |
| 敏感不主动 | 预置一条敏感卡→问无关问题 | **不** fetch 敏感卡;index 只见 `is_sensitive` 粗标 |
| 敏感相关才给 | 直接问该敏感话题 | 才允许 fetch,且回答克制 |
| 降级真机 | test 切一个弱/不支持 tool 的模型,问猫 | 自动走 auto_readside,**仍答对**;trace.mode=auto_readside |

---

## 6. L6 回归测试(零回归硬门)

| 对象 | 断言 |
|---|---|
| Memory Garden | iOS 展示、`/v1/memory/list` 不受影响 |
| 写入路径 | capture / 写记忆 完全不动,行为一致 |
| `/v1/chat/history` | 除召回来源外其他字段、`context_memory_trace` 不变 |
| web_search 二阶段 | 本轮保留,仍正常工作(未被 memory loop 干扰) |
| flag 全关 | `MODEL_API_MEMORY_TOOLS_ENABLED=false` 时,行为与现状**逐字节一致** |
| MCP 已有工具 | `feedling_memory_index/fetch` 不动,仍可用 |

---

## 7. L7 隐私 / 安全测试(硬门)

| 用例 | 断言 |
|---|---|
| index 不漏原话 | 任何路径下 index 都不含 `verbatim/her_quote/follow_up/具体 sensitive_scope` |
| 敏感默认不进 index | 仅返回 `is_sensitive`/`sensitivity_class` 粗粒度 |
| 敏感 fetch 门控 | 非当前话题明确相关,不返回敏感正文 |
| backend 日志 | 不打印 `summary/verbatim/follow_up`(沿用 readside 约束) |
| 跨用户隔离 | 拿别人 id fetch → `missing_ids`,不泄露存在性 |
| 工具失败不泄露 | enclave 解密失败按 item 进 `unavailable_ids`,不抛整体明文错误 |

---

## 8. L8 性能 / 成本测试

| 指标 | 上限/断言 |
|---|---|
| 单轮模型往返 | ≤ `max_iters=3` |
| 累计 fetch 正文 | ≤8 条,超出截断 |
| 回填触发率 | 只在"没调工具+有候选"触发;正常调工具路径**不触发** |
| 闲聊轮开销 | 不涉及记忆的轮次:模型可不调工具,**不强制每轮查 memory** |
| 延迟回归 | agent_tools 相对 auto_readside 的 P50/P95 增量在可接受预算内(记录基线,设阈值告警) |

---

## 9. 可观测(trace 断言贯穿各层)

每个回复的 action trace 必须含:
```json
{
  "memory_tools": {
    "mode": "agent_tools | auto_readside | fallback",
    "fallback_reason": "provider_rejected_tools | no_tool_call_backfilled | null",
    "index_called": true,
    "fetch_called": true,
    "tool_calls": [{"name":"memory_index","ok":true,"item_count":21},
                   {"name":"memory_fetch","ok":true,"ids":["mem_cat_1"],"item_count":1,"capped":false}],
    "user_card_count": 12,
    "fetched_ids": ["mem_cat_1"]
  }
}
```
断言:模式与实际路径一致;降级用例的 `fallback_reason` 正确;`user_card_count>50` 时有盲区告警标记。

---

## 10. 不可接受清单(任一出现 = 阻断上线)

```text
- 没调工具也没回填,却声称记得(编造)
- 一次 fetch 远超上限的正文
- index 漏出 verbatim / her_quote / 敏感细节
- 敏感正文默认进 index 或被无关问题取出
- 工具失败后模型编造记忆
- 弱模型(不按格式调工具)直接丢召回(没回落 auto)
- flag 关闭时行为与现状不一致(污染了主路径)
```

---

## 11. 执行顺序(给 Codex)

```text
Step 0  写 L1+L2+L3 测试(先红)
Step 1  实现 core + 工具执行器 → L1 绿
Step 2  实现 foreground tool loop → L2 绿
Step 3  实现 no-tool-call 回填 → L3(降级)测试绿  →  test 放量
Step 4  手写 probe 集 + grader → L4 跑对比  →  过红线才 prod 灰度
Step 5  e2e 真机(猫+扩展+降级)→ L5  →  prod 放量
全程    L6/L7/L8 作为每次提交的回归/硬门
```

---

## 12. 一句话收口

> **测试分八层,三个硬门:① L3 降级——任何模型都不许丢召回(弱模型召回率必须 = 现状);② L4 eval——agent_tools 召回率 ≥ auto_readside 且 paraphrase 子集显著提升,才证明这次升级值;③ L7 隐私——原话不漏、敏感不乱取。先写测试再改代码,flag 全关时与现状逐字节一致。**
