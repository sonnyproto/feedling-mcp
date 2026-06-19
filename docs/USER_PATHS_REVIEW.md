# 两类用户的运行总览与缺漏盘点（BPS · API）

> 撰写日期 2026-06-16，branch: `test`。
> 聚焦**功能层面**——Onboarding、Chat/Memory、Proactive 在两条用户路上
> 各自怎么跑、哪里不对称。**不含加密/部署细节**（那些见
> `docs/DESIGN_E2E.md` / `deploy/DEPLOYMENTS.md`）。
>
> 本文 Part 2 的缺漏来自一次系统性代码阅读，按"是否真问题"分级。
> ⚠️ **文中文件位置是阅读时的近似定位，动手修复前请先就地核对行号。**
> 已在 `docs/OPTIMIZATION_BACKLOG.md` 记录的条目会标 `[backlog 已知]`。

---

## 术语对齐

| 简称 | 全称 | 谁是大脑 | 谁跑 agent 循环 |
|------|------|---------|----------------|
| **BPS** | 自建服务器 / Resident Consumer（路由 B） | 用户自己的 agent（VPS / Hermes / Claude Code） | 用户机器上的常驻进程 `tools/chat_resident_consumer.py` |
| **API** | Model API 托管（路由 C） | 用户提供的 provider key（OpenAI/Anthropic/Gemini） | **后端进程内线程**（后端自己当 consumer） |

> 两条路最根本的区别就一句话：**谁跑 agent 循环**。后文所有差异都从这一点
> 派生。还有一条 `official_import`（仅导入）路由基本是历史遗留，见 Part 2 §11。

---

# Part 1 · 功能总览

## 1. Onboarding

两条路共享同一个骨架——**记忆门 → 身份门 → 实时连接门**——但实现完全不同。

| 阶段 | BPS（resident） | API（model_api） |
|------|----------------|------------------|
| **配置** | 无（用自己的 agent） | `POST /v1/model_api/setup` 存 provider/model/key，**立刻试调一次**，`test_status=ok` 才算过 |
| **灌记忆** | agent 手动逐条 `/v1/memory/add`（bootstrap 四阶段） | `POST /v1/history_import/start` 一次性导入历史 → 后端蒸馏成记忆卡 |
| **记忆门槛** | **按关系天数分档**（见 §3 floors 表） | **硬编码 `story≥1, about_me≥1`，不分档** ⚠️ |
| **身份卡** | agent 派生，`identity_init` 带 `days_with_user` + 证据，**做天数错配校验（差 >1 天即 409）** | history import 渲染，**不做天数错配校验** ⚠️ |
| **实时连接验证** | 必须跑 `/v1/chat/verify_loop`——后端发合成 ping，等 30s 真回复（**证明环活着**） | 只看"有没有 greeting 或一次 user→agent 交换"（**只证明尝试过，没证明能用**） ⚠️ |
| **consumer 心跳** | 要求带 `X-Feedling-Consumer` 头、180s 内轮询过 | 无（后端托管，无心跳概念） |
| **完成判定** | `/v1/onboarding/validate`：记忆 + 身份 + 锚点 + consumer + live_loop + 首条问候 + 真实交换 | 8 步顺序门：config → test → runtime → import → memory → identity → anchor → hosted_chat |

⚠️ 标记的是 Part 2 要展开的不对称点。

**Onboarding 各门返回码（BPS 侧，`backend/bootstrap/gates.py`）：**

| stage | 触发 | 含义 |
|-------|------|------|
| `needs_memory` | identity_init / chat_response | Story 或 About_me 档位未达标 |
| `needs_identity` | chat_response | 身份卡未写 |
| `needs_resident_consumer` | chat_response | consumer 未带官方头 / 超 180s 未轮询 |
| `needs_live_connection` | chat_response | `chat_loop_verified` 未翻 |
| `main_loop` | — | 全部通过，放行 |

## 2. Chat + Memory

### Chat 数据面（共用）
每用户 5000 条环形缓冲、`seq` 自增、长轮询用 `threading.Event` waiter、落库即唤醒。

- **BPS**：consumer 长轮询 `/v1/chat/poll`（带 ~120s claim 租约防重复领取）→ 调自己 agent → `/v1/chat/response` 回写。支持多气泡、thinking 元数据、屏幕上下文自动注入（消息提到屏幕 / `SCREEN_CONTEXT_MODE=always`，帧需 <300s）。
- **API**：`POST /v1/model_api/chat/send` → **用户消息先落库** → 组 prompt（身份 + 记忆 + 屏幕 + 待确认项）→ 调 provider（默认 `max_tokens=2048`，90s 超时）→ 助手消息后落库。**不是 agentic loop**：模型返回一个 JSON（`reply` + 可选 `tool_requests`），后端执行后**不把执行结果回喂给模型**。

### Memory 模块（共用，6 类型 → 3 tab）

| 类型 | Tab | 语义 |
|------|-----|------|
| `moment` / `quote` | Story 故事 | 你们之间发生的事 / 原话 |
| `fact` / `event` | About me 关于我 | 偏好关系习惯 / 生活里的具体事件（密度燃料） |
| `insight` / `reflection` | TA 在想 | 锚定的理解 / 独立思考 |

- **写入门**：`insight` 需 ≥1 anchor；`reflection` 需 ≥2 anchor + 按关系档位的频控（违反返回 429）。anchor 一律服务端校验存在性 + 归属。
- **检索**（喂给 agent 的 ~8 张卡，`backend/context_memory_selection.py`）：
  - **resident 宽松**：3 转折卡（`转折｜` 前缀）+ 2 最近创建 + 3 与最新消息相关，去重封顶 8。
  - **model_api 严格**：实体/强短语命中才入选；泛词、中英停用词召不动 persona 卡。
- **生命周期**：删除走归档（`is_archived`）不走物删；`retype` 可改类型（转 reflection 豁免频控）。

### API 路特有的后台机制（BPS 没有——BPS 的 agent 自己干这些）
- **记忆捕获**：每 24 轮一次（单独 provider 调用蒸馏新卡）。
- **状态动作**：每轮后台规划 `identity.patch` / `dimension_nudge` / memory 动作，按置信度分"直接执行 / 待用户确认"（待确认项 24h 过期）。
- **recap**：每 80 轮、最短间隔 12h。

## 3. Memory Floors（按关系天数分档）

> BPS 与 official_import 用这套分档；**model_api 不分档**（见 Part 2 §1）。

| 关系时长 | story | about_me | ta_thinking |
|---------|-------|----------|-------------|
| ≥6 个月（≥180d） | 15 | 60 | 12 |
| ≥1 个月（≥30d） | 8 | 25 | 5 |
| ≥2 天 | 3 | 8 | 2 |
| 刚认识（<2d） | 1 | 1 | 0 |

Story + About me 达标 = `identity_init` 硬前置；三 tab 全达标是建议目标。

## 4. Proactive（两条路差最大的地方）

平台只递"醒来的机会"，判断权交给 agent（V2 原则，两边都守住）。

| 维度 | BPS（resident） | API（model_api） |
|------|----------------|------------------|
| **谁触发** | consumer 自己定时 `PROACTIVE_TICK`（录屏开 5min / 关 30min） | 后端 tick loop 每 60s 扫，每 ~30min 判一次心跳 wake |
| **谁执行** | consumer 轮询 `/v1/proactive/jobs/poll` 领 job，本地调 agent | 后端 append hook 立刻起进程内线程消费 |
| **job 状态机** | `pending → claimed → realizing → posted / completed` | 同语义 + 并发槽（每用户 2 / 全局 16） |
| **卡死回收** | **无超时回收** ⚠️（见 Part 2 §4） | **有 lease**：claimed/realizing 超 600s 被 reconcile 标失败 |
| **wake 后捕获记忆** | agent 自行决定 | **不捕获**——只有前台 chat 轮捕获 ⚠️ |
| **agent 响应动作** | send_message / set_ai_state / sleep / request_broadcast | send_message / set_ai_state / sleep（**不能写 memory/identity**） |

---

# Part 2 · 缺漏盘点

按"是否真问题"分级。`[backlog 已知]` = 已在 `OPTIMIZATION_BACKLOG.md`；`[新发现]` = 本次盘点新增。

## 🔴 高优先级：会导致"假完成"或静默失败

### 1. API 用户的记忆门槛不分档 —— 假完成 `[新发现]`
`backend/hosted/onboarding_validation.py` 对 model_api 硬编码 `story≥1, about_me≥1`，不随关系天数变化。一个声称"在一起 200 天"的 API 用户，导入 1 张 story + 1 张 fact 就能通过 onboarding；同样关系的 BPS 用户要 15/60/12。**这是两条路最刺眼的不对称**——记忆是 proactive 和"TA 还记得"的燃料，API 用户的花园可能极浅却显示"完成"。

### 2. API 用户的"实时连接"没真验证过 `[新发现]`
BPS 强制 `verify_loop`（合成 ping + 等真回复）。API 路只检查"greeting 存在 OR 有过一次 user→agent"，**从没证明 provider 调用真的能成功**。若 key 配错 / provider 长期超时，greeting 可能是 import 阶段写的，用户第一次真聊天就挂——而 onboarding 已绿。

### 3. provider 失败 → 用户消息变孤儿 + 无退避 `[新发现]`
`backend/hosted/chat_routes.py`：用户消息先落库，provider 调用失败就返回 502，**没有助手消息**——用户消息悬空。且 429 / 超时都当普通 ProviderError，**无退避、无重试队列**。配合 §2，API 用户的失败体验比 BPS 差很多。

### 4. resident proactive job 无回收超时 `[新发现，且与 hosted 不对称]`
chat poll 有 ~120s claim 租约，hosted wake 有 600s lease reconcile，**唯独 resident 的 proactive job 没有任何超时回收**。consumer 在 `claimed`/`realizing` 状态崩溃 → job 永久卡死，下次 poll 因 `status != pending` 不会重发，那次主动关怀永远不来，只能去 `/debug/proactive` 手动改。典型的"三处实现两套策略"遗漏。

## 🟡 中优先级：状态错乱 / 隐藏失败

### 5. 中途切路由会搁浅状态 `[新发现]`
用户先选 resident 写了一堆记忆/身份，再调 `/v1/model_api/setup` → **自动切到 model_api**。旧记忆按 resident 档位写、却走 model_api 的门，且无 history_import job。用户困惑"明明有记忆身份，为什么 onboarding 不过"。缺"路由切换清理/迁移"逻辑。

### 6. 记忆删除无引用完整性 `[新发现]`
`insight`/`reflection` 锚在别的卡上，但 `/v1/memory/delete` 删被锚的卡时**不检查谁引用了它**（锚校验只在写入/retype 时做）。结果：agent 拿到锚指向已删卡的 insight，静默读空。

### 7. 天数锚点会随后续导入漂移 `[新发现]`
`days_with_user` 在 `identity_init` 时一次性从最早记忆算、之后冻结。agent 后续补了更早的记忆 → 锚点过时，且 `identity_replace` 不重算。API 路尤甚（import 一次性）。

### 8. 后台记忆捕获无每用户锁 → 可能重叠 `[新发现]`
`backend/hosted/turn.py`：状态动作和 recap 都有每用户锁，**唯独记忆捕获没有**。第 24 轮捕获还在跑（provider 慢），第 48 轮又触发 → 两窗口重叠，可能产出重复卡。

### 9. `tool_action_enabled` 这个门形同虚设 `[新发现，需核实]`
`backend/hosted/config_store.py` 把它默认设 `True`，而**没有任何代码会把它设成 False**；onboarding 却拿"它必须为 True"当一个验证步。等于这步永远通过——要么是没写完的校验，要么是冗余门。值得确认意图。

### 10. history import 卡住的 job 无恢复路径 `[新发现]`
后台线程崩 / 重启 → job 卡在 processing，超 1h 被标 error，但**客户端只能重新开一个**，不能 resume/abandon，也没防双开 → DB 里可能堆两个半截 job。

### 11. official_import 路由像死代码 `[新发现]`
`onboarding_validation.py` 为 official_import 定义了一套简化校验（仅记忆 + 身份 + 锚点），但代码里**没有端点真正驱动它 / 与官方 App 导入对接**。选了这条路的用户无指引。确认是否该删或补全。

## 🟢 已在 backlog（列出对账）

| backlog # | 条目 | 摘要 |
|-----------|------|------|
| #14 | hosted tick 全量饿加载 ✅ | 已修（dc4138f）：tick 改走 `_hosted_keyholder_user_ids()`，只遍历进程内缓存 + 持 key 用户，不再全量 `get_store` |
| #4 | memory 写放大 | 加一张卡 DELETE + 重插全部行，老用户近百行 |
| #11 | verify-loop 修复是否部署 | 三层修复曾"已修未部署"，需核对线上版本 |
| #10 | 孤儿账号恢复 | prod 孤儿 lineage 需跑 `recover_orphan_accounts.py` |
| #12 | 常红测试麻木 | 依赖 enclave 的测试长期红 |
| #13 | user_logs 膨胀 | 确认高频 stream 都有 `log_trim` 调用点 |

---

## 元观察：系统性不对称

**几乎每个"三处都该有"的机制，都只实现了一两处：**

| 机制 | chat | resident proactive | hosted（model_api） |
|------|------|--------------------|--------------------|
| claim 租约 / lease | ✅ ~120s | ❌ 无回收 | ✅ 600s |
| 记忆分档门 | — | ✅ 分档 | ❌ 硬编码 1/1 |
| 活性真验证 | — | ✅ verify_loop | ❌ 只看尝试 |
| wake 后捕获记忆 | — | （agent 自理） | ❌ 不捕获 |
| 每用户锁（后台作业） | — | — | 状态/recap ✅、捕获 ❌ |

这不是单点 bug，是**两条用户路并行演进、共享骨架但各写各的实现**留下的系统性不对称。根因是**没有一张统一的 capability matrix 强制两条路对齐**。建议后续以"对齐表"为单位逐项收口，而非逐个 bug 修。

---

## 下一步建议

1. 把 🔴/🟡 条目逐项落进 `OPTIMIZATION_BACKLOG.md`，并补一节"resident vs model_api 对齐表"作为收口清单。
2. 修复前先就地核实文件行号（本文定位为阅读近似值）。
3. 优先级建议：§1（假完成）、§2（连接没验证）、§4（job 卡死）三项先动。
