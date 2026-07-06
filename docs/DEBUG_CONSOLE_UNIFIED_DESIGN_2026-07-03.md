# io 统一 Debug 面板（DebugConsole）设计方案 · v2（定稿）

> 2026-07-03 · CC 起草 → hx review 收紧 → 定稿 · 状态：**M1 已 ship**（backbone/emit 端点/trace_id glue 均已落地，见 `diagnostics/routes_asgi.py`、`chat/chat_core.py`）；**M2/M3 为待做 roadmap**。文中 `*/routes.py` 行号为 ASGI 迁移前旧位置。
> 跨仓：`feedling-mcp`（后端 backbone + 埋点）· `feedling-mcp-ios`（面板 UI）
>
> **v2 相对 v1 的收紧（hx review）**：① 不往 ring blob 硬塞大段明文，改"可读内容快照 + excerpt"；
> ② 明文默认记的是"够看懂的快照"不是全量明文审计；③ M1 不做 `hosted/turn.py` 工具级埋点，只到
> turn/阶段级；④ 分组/过滤放客户端；⑤ proactive 后移 M2；⑥ resident reply 的 trace_id glue 必须补。

## 1. 背景与目标

现在 io 对开发者像**黑盒**：一条用户消息触发一整条链（存消息 → 拉 history → 拉 identity → 挡 worldbook → 选 memory → 拼 prompt → 调 agent → 出 reply → 写回 → capture），出问题时**不知道卡在哪一步**。观测被切成三摊、互不相通，现有 flow trace 只存 metadata、看不到实际内容。

**目标**：一个统一的、per-user 的 Debug 面板，把全流程关键节点都上报，内测阶段直接看**可读内容快照**，让"哪一步断了 / 慢了 / 报错了"一眼可见，且**每条事件是人话，不是 JSON**。

**第一痛点（hx 强调）= LLM 黑盒**：喂给模型的上下文/输入是什么、模型输出什么、以及**在哪卡住了**（模型调用发起了但没返回）现在完全看不见。所以 `context`(注入了什么) + `agent`(模型输入/输出/是否挂起) 是 M1 的重中之重，见 §4.4、§5。

**硬不变量（hx 强调"绝不能影响业务流程"）**：所有埋点经 `debug_trace.trace_event`——best-effort、**任何异常必吞、绝不上抛**、绝不进主 return 路径、门控关时零成本 no-op、明文只进 per-user ring blob 不碰主数据。这是验收项（§12），不是口号。

**非目标**：不是生产级 APM、不是全量明文审计、不是跨用户日志聚合。这是**内测自测工具**。admin 拉全量崩溃日志仍走已有 `diagnostics` 通道。

## 2. 现状盘点（三套割裂的观测）

| 系统 | 位置 | 用途 | 处置 |
|---|---|---|---|
| **flow trace** | `backend/debug_trace.py` + `diagnostics/routes_asgi.py`(`/v1/debug/trace`，body 在 `diagnostics_core.py`) | per-user 环形缓冲、双门控、**仅 metadata** | **升级为唯一 backbone** |
| **diagnostics 上传** | `backend/diagnostics/routes_asgi.py` + iOS `DiagnosticLog` | iOS 把 `diagnostics.log` 整文件传 R2，admin 拉 | **保留不动**（崩溃日志留证，用途不同） |
| **proactive 指标** | `backend/proactive/observability_v2.py`（`MetricEventV2`） | proactive runtime eval 指标流 | **保留**；M2 再薄桥接进面板 |

现状埋点极稀（~16 处）：读侧(`memory_readside`/`index_selector`/`context_memory_selection`)、agent turn 内部、genesis worker 几乎零覆盖。

**ring buffer 存储事实（决定 v2 收紧）**：`debug_trace` 事件存在 `user_blobs` 的 JSONB 单行里，每次 `db.set_blob`（见 `backend/db.py:747`）都**重写整份 doc**——读旧 blob、append、写回整块。因此**明文越大越亏**：测试一多，debug 工具自己会变成性能问题。→ content 必须小、只存 excerpt。

## 3. 设计总览

```
                         ┌──────────────── backbone（唯一事件总线）──────────────┐
  route ─┐               │  debug_trace.trace_event(store, subsystem, type,      │
  context┤  emit         │     status, summary, explain=人话, detail=metadata,   │
  memory ┼──────────────▶│     content_excerpt=短明文, trace_id, turn_id,        │
  agent  ┤               │     job_id, dur_ms)                                   │
  reply  ┤               │  → per-user 环形缓冲(ring blob, verbose 时上限收紧)   │
  ...    ┘               └───────────────────────────────────────────────────────┘
                                    │ GET /v1/debug/trace  (返回扁平 events)
                                    ▼
                     ┌──────────── iOS DebugConsoleView ──────────────┐
                     │  模块 chips(10, 默认全选) · 分组⇄扁平(客户端)   │
                     │  · 搜索 · 状态筛选 · 复制(单条/整 turn/筛选报告)│
                     │  每条: 人话解释 + 技术 detail + 明文 excerpt     │
                     └────────────────────────────────────────────────┘
```

**核心原则**：不重造轮子——`debug_trace.py` 扩字段 + 铺埋点即可。**分组/筛选全在客户端**，服务端只返回扁平 events。

## 4. 数据模型

### 4.1 事件形状

```jsonc
{
  "ts": 1720000000.123,
  "subsystem": "context",       // = 模块名（见 §5）
  "type": "context.build.done", // <module>.<stage>.<phase>, phase ∈ start|done|error
  "actor": "host_agent_runtime",// ∈ host_agent_runtime|vps_resident|backend|ios|agent
  "status": "ok",               // ∈ ok|blocked|error
  "summary": "context built",   // 一行技术摘要（英文短句 OK）
  "explain": "本轮上下文已构建完成：注入 identity、6 条 memory、2 条 worldbook；无附加屏幕。", // 【新增·一等公民】人话
  "detail": { "history": 18, "memory_n": 6, "worldbook_n": 2, "persona_v": "v3" }, // 结构化 metadata，保持小
  "content_excerpt": { "prompt_head": "You are ...(前~800字)", "memory_titles": ["蛋子是狗", "..."] }, // 【新增】短明文 excerpt，verbose 时才填
  "trace_id": "<msg_id>",       // 分组键，填充规则见 §4.2
  "turn_id": "<msg_id>",
  "job_id": "",                 // 后台链用
  "dur_ms": 42                  // 【新增】本步耗时（*.done/*.error 带）
}
```

**四个字段的分工（关键）**：
- `summary`：一行技术摘要（给搜索/扁平流）。
- `explain`：**人话解释**——"发生了什么 + 是否符合预期"，面板默认显示这行。这是让 Console 不沦为"JSON 盒子"的核心。
- `detail`：结构化 metadata（ids/counts/reasons/hash/tokens），**小**。
- `content_excerpt`：短明文快照，**只 excerpt**，verbose 门控开时才填（见 §6）。

### 4.2 `trace_id` 分组规则

- **主聊天链**：`trace_id = 用户消息 id`（client envelope `id`，`store.append_chat` 落库为 `msg["id"]`）。天然贯穿全链，各埋点统一填它。
- **VPS resident 路径必须补 glue**【已修，M1 落地】：consumer 读消息拿 `msg["id"]`、回写 reply 传 `reply_to_message_id`（`tools/chat_resident_consumer.py`）。`/v1/chat/response` 的 trace_event 现在在 `backend/chat/chat_core.py`（含 gated 分支 `trace_response_gated`），两处均已显式 `trace_id = reply_to_message_id or msg["id"]`。
- **后台链**（genesis worker、定时 capture、proactive）：`trace_id = job_id`。
- **面板分组**：客户端按 `trace_id` 折叠成 turn 卡片，卡内按 `ts` 升序；无 trace_id 归 "ungrouped"。

### 4.3 命名规范

`type = <module>.<stage>.<phase>`，`phase ∈ start|done|error`。成对埋点：`*.start`（入参）→ `*.done`（`dur_ms`+输出 excerpt）或 `*.error`（`dur_ms`+异常明文）。一次性节点（如 `route.chat.message`）可无 phase，视作 done。

### 4.4 "卡在哪" = start/done 配对未闭合（LLM 挂起可见）

这是解决"在哪卡住也不知道"的核心机制：每个关键步骤都成对埋 `*.start`/`*.done`。面板分组视图里：
- `agent.model.call.start` 有、对应 `.done`/`.error` **没有** → 该步渲染为 **"⏳ 挂起中/未返回"**——模型调用发起了但没回来，一眼定位到卡在模型这一步。
- `.done` 带 `dur_ms` → 哪步**慢**（如 model.call 8.2s）直接可见。
- 后续步没有任何事件 → 渲染为**灰缺口**（链路没走到这里）。

所以"卡在哪"不需要额外机制，就是分组视图里第一个"只有 start 没有 done"的步，或第一个 error/缺口。

## 5. 模块清单与埋点覆盖

11 个模块 = iOS 勾选框集合，**默认全选**。**【M1】** = 首期打通"发消息卡哪步"主链；其余 M2/M3。

| 模块 | 期 | 关键埋点 | explain 举例 |
|---|---|---|---|
| **route** | **M1** | `route.chat.message`(生成 trace_id)、`route.chat.response`(补 trace_id glue)、`route.chat.response.gated`、`route.poll.delivered` | "收到用户消息，已入库并唤醒 consumer" |
| **context** | **M1(consumer 附加)** M2(后端读) | consumer: `context.build`(附加屏幕上下文/注入 history)；M2 后端: `context.identity.load`、`context.memory.injected`(io_cli 读) | "本轮附加了屏幕上下文（微信页）+ 注入 history 18 条" |
| **agent** | **M1** | consumer CLI 边界: **`agent.model.call.start/done/error`**（复用 `_log_cli_turn_timing`/`_codex_turn_metrics`；start 带 prompt excerpt，done 带输出 excerpt + dur_ms/tokens，只有 start 无 done = 挂起）；后端: `reply.stored` | "模型调用发起（claude, prompt 3.2k tok）→ 返回（输出 1120 字，2.3s，4 步）" |
| **memory** | **M1(写)** M2(读细节) | **M1**: `memory.capture.queued/done/error`；**M2**: `memory.read.select.start/done/error`、`memory.read.baseline`、`memory.read.injected`、`memory.write.migrate/dream` | "本轮抓取到 1 条新记忆：蛋子是狗" |
| **worldbook** | M2 | `worldbook.match`、`worldbook.injected` | "命中世界书 2 条：X、Y" |
| **genesis** | M2 | `genesis.foreground.start/done`、`genesis.checkpoint`、`genesis.worker.stage.*`、`genesis.distill.identity/voice/memory` | "入驻蒸馏完成 identity 阶段" |
| **identity** | M2 | `identity.read`、`identity.write` | "identity 已更新 persona v3→v4" |
| **proactive** | M2 | `proactive.wake`、`proactive.turn.*`、`proactive.delivery.decision`、`proactive.job` | "主动唤醒（heartbeat），决定投递" |
| **perception** | M2 | `perception.snapshot.report`、`perception.frame.phash`、`perception.screen.analyze.start/done/error`、`perception.upload`（含权限/开关状态）；done 带**感知到的内容 excerpt**（屏幕/场景摘要） | "感知上报：识别到微信聊天页；相似帧已去重；已喂给 proactive" |
| **push** | M3 | `push.apns.sent/suppressed`、`push.liveactivity.update` | "APNs 因前台活跃被抑制" |
| **account** | M3 | `account.whoami`、`account.attestation`、`account.keys.init`、`account.bootstrap.gate` | "attestation 通过，内容密钥已建" |

> **M1 主链 = `turn.received → context.build.* → agent.turn.* → reply.stored → memory.capture.*`**。回答的就是"存消息、构建上下文、调 agent、写回复、触发 capture，卡在哪"。
> 埋点全走 `debug_trace.trace_event`（best-effort、不抛异常、门控关时零成本 no-op）。

### 5.1 M1 埋到哪 / 不埋到哪（降噪 vs LLM 可见）

**真实 live 路径**：iOS 存消息 → `tools/chat_resident_consumer.py`（每用户一进程，跑 CVM）poll 到 → subprocess 调 `claude`/`codex exec` CLI 出回复 → POST `/v1/chat/response`。所以模型调用发生在 **consumer 的 CLI subprocess 边界**，不在后端（FastAPI/ASGI）。

**M1 埋**：consumer 的 **CLI subprocess 边界一处**(`agent.model.call.start/done/error`，复用现成 `_log_cli_turn_timing`/`_codex_turn_metrics` 的 duration/tokens，带 prompt/输出 excerpt + 挂起可见) + consumer 的 `context.build`(附加屏幕/history) + 后端边界(`route.chat.message`/`reply.stored`/`memory.capture.*`)。这正好回答"LLM 输入输出是什么、卡没卡"，是第一痛点。

**M1 不埋**（放 M2/M3，避免噪音）：工具级 `agent.tool.call`（一个 turn 内 io_cli 多次 HTTP 读逐个埋）、每个内部 model round 的**完整原文**、io_cli 读 memory/identity/worldbook 的后端细节（M2 在后端端点侧补）。**hosted/turn.py（退役中）M1 一律不埋。**

## 6. verbose = 可读内容快照（不是全量明文日志）

**决策（hx 2026-07-03）**：内测阶段，面板开着**默认记快照**，纯自测用。但记的是"够看懂"的**快照/excerpt**，不是全量明文审计。

**默认记录（快照）**：
- 用户消息摘要 / 前 500–1000 字
- prompt 结构摘要（system 头 + 注入项计数，非完整 prompt）
- memory 标题 / 摘要 / 短正文
- worldbook 命中名 + 短正文
- agent reply 前 ~1000 字
- **error 明文**（完整，错误要看全）

**默认不记录**：
- 完整 prompt
- 完整 history
- 完整工具出参
- 大段 onboarding 原始上传材料

**门控**：
- `debug_trace.is_enabled(store)`（现有双门控：`FEEDLING_V1_FLOW_TRACE` 硬开关 + per-user blob）——是否**记录事件**。
- `debug_trace.verbose_enabled(store)`【新增】——是否填 `content_excerpt`。默认 `is_enabled` 为真即真；留 env 硬开关 `FEEDLING_DEBUG_VERBOSE=0` 可全局强制剥离 excerpt（防未来误开，平时不用管）。
- 埋点调用方**总是把明文传进 `content_excerpt=`**；`trace_event` 内部按 `verbose_enabled` + 截断决定落多少。单一决策点，调用点不判门控。

**大小护栏（v2 收紧）**：
- verbose 模式下 `_MAX_EVENTS` 降到 **150–200**（非 verbose 保持 500）。
- `content_excerpt` 每 event 总量 **≤ 4KB**，单字段 **≈ 1KB**。
- 面板复制报告里若截断，标注 `…(content truncated)`，**不在 ring 里硬塞完整大文本**。
- 200 × 4KB ≈ 800KB/user blob，远健康于 v1 的 8MB。
- 若未来真需要完整 prompt/reply → **单独设计 debug artifact stream（append-only log，非 ring blob）**，本期不做。

## 7. 后端改造点（文件级）

1. **`backend/debug_trace.py`**：
   - `trace_event(...)` 增参 `explain: str = ""`、`content_excerpt: dict | None = None`、`dur_ms: float | None = None`。
   - 新增 `verbose_enabled(store)`（env `FEEDLING_DEBUG_VERBOSE` + 复用 is_enabled）。
   - 新增 `_safe_content_excerpt()`（每字段 ~1KB、每 event ≤4KB 截断，截断打标）。
   - verbose 模式 `_MAX_EVENTS` 降到 150–200（可按 `verbose_enabled` 动态取）。
   - **不做服务端分组**——`read_trace` 保持返回扁平 events + `limit` + 单 subsystem 兼容。
2. **新增 emit 端点 `POST /v1/debug/trace/event`**（现已落地：`backend/diagnostics/routes_asgi.py` + `diagnostics_core.emit_trace_event_payload`）：`require_user`(复用 FEEDLING_API_KEY) + 门控 + best-effort，收 consumer 推来的一条事件（body = subsystem/type/status/summary/explain/detail/content_excerpt/trace_id/turn_id/dur_ms），内部转调 `debug_trace.trace_event`。**这是 live 路径的关键**：consumer 是 HTTP-only、无 DB，只能靠这个端点上报。
3. **`backend/chat/chat_core.py`**（原 `chat/routes.py`，ASGI 迁移后逻辑在 chat_core）：`chat.response` 与 gated（`trace_response_gated`）两处补 `trace_id = reply_to_message_id or msg["id"]`（glue）——**已落地**。
4. **M1 埋点分布（真实 live 路径 = resident consumer 驱动 claude/codex CLI）**：
   - **后端可见的边界**（backend 直接 `trace_event`）：`chat/chat_core.py` 的 `route.chat.message`(生成/带 trace_id) + `route.chat.response`(reply.stored)；memory capture（`memory/service.py`/`actions.py` 或 consumer 的 capture job，取实际触发处）`memory.capture.queued/done/error`。
   - **consumer 端**（`tools/chat_resident_consumer.py`，经 emit 端点上报）：`context.build`(附加的屏幕上下文/注入 history，`_screen_context_for_message` 一带) + **`agent.model.call.start/done/error`**（CLI subprocess 边界，**复用现成的 `_log_cli_turn_timing` + `_codex_turn_metrics`** 拿 duration/tokens/steps；start 带 prompt excerpt，done 带 reply excerpt + dur_ms/tokens，只有 start 无 done = 挂起）。trace_id = poll 到的用户消息 id。
   - **降级不做**：`hosted/turn.py` / `hosted/context.py` 是退役中的 hosted model_api 路（`/v1/model_api/chat/send`），**M1 不埋**；仅当 test 上确有用户仍走 hosted 才后补。
5. **`/v1/debug/trace` 返回体**：回显 `verbose` 标志位（面板提示 excerpt 是否生效）。

**耗时**：backend 侧每 `*.start` 记 `t0=time.monotonic()`，`*.done/.error` 算 `dur_ms`；consumer 侧 CLI turn 直接用 `_log_cli_turn_timing` 已算的 wall_ms/duration_ms。turn 级用消息 id 关联首尾。

**运行时安全（前提①）**：consumer 侧 emit 必须 fire-and-forget（短超时 httpx、异常吞掉、绝不阻塞/拖慢 CLI turn 或 reply 回写）；backend emit 端点 best-effort。观测坏了业务不能坏。

## 8. iOS 面板 UX（`DebugConsoleView`）

替换现有 `FlowTracePanel`，接进 `DebugTool.swift` 工具 tab（"查看 v1 flow trace" 入口改指它）。

- **顶部**：deploy 硬关警告条 + verbose 是否生效提示 + 刷新 / 自动 tail 开关。
- **模块 chips**：11 个多选，**默认全选**，"全选/全不选" 快捷。
- **视图段控** `分组 ⇄ 扁平`（客户端）：
  - *分组*：按 `trace_id` 折叠 turn 卡片，标题 = 首事件 explain + 总耗时 + 终态(✓/✗ 断在哪步)；展开逐步列 `✓/✗ type  dur_ms  explain`，未到达的后续步显示灰缺口。
  - *扁平*：所有事件时间倒序。
- **搜索框**：按 `explain`/`summary`/`type`/`content_excerpt` 过滤。
- **状态筛选**：ok / blocked / error。
- **每条事件（关键）**：默认显示 **explain（人话）**；折叠区展开 **技术 detail(JSON) + 明文 excerpt**。
- **复制**：单条 event(explain+detail+excerpt) / 整 turn(该 trace_id 全部) / 当前筛选结果报告。
- **实时**：下拉刷新 +（可选）2s tail。

**iOS 侧改动**：`FeedlingAPI.FlowTraceEvent` 增 `explain`、`contentExcerpt`、`durMs`；`fetchFlowTrace` 拉全量后**客户端**按模块/状态筛选、按 trace_id 分组；新增 `DebugConsoleView` + `TurnCard`；复制走 `UIPasteboard`。

## 9. API 契约（不变，仅扩返回体）

```
GET    /v1/debug/trace?limit=&subsystem=  → { enabled, deploy_enabled, verbose, events:[ ...含 explain/content_excerpt/dur_ms ] }
POST   /v1/debug/trace/enable  {enabled}   → { enabled, deploy_enabled }
DELETE /v1/debug/trace                      → { status:"ok" }
POST   /v1/debug/trace/event  {event}      → { status:"ok" }   【新增】consumer 上报一条事件（require_user + 门控 + best-effort）
```

- 分组/过滤/多模块 = **客户端**，服务端只 `limit` + 单 subsystem 兼容。
- `POST /v1/debug/trace/event` body：`{ subsystem, type, status, summary, explain, detail, content_excerpt, trace_id, turn_id, dur_ms }`。是 live 路径（HTTP-only consumer）上报 `context.build` / `agent.model.call.*` 的唯一通道。

## 10. 隐私与安全

- excerpt 只在 `FEEDLING_V1_FLOW_TRACE` 允许 + per-user 面板开关开时记录；`FEEDLING_DEBUG_VERBOSE=0` 全局强制剥离。生产用户从不开 DebugTool → 天然 off。
- 只进 per-user ring blob，**不落 R2、不进 admin diagnostics**，随 TTL / 150–200 条上限自然过期。
- `trace_event` best-effort、绝不抛异常——观测永不弄坏真实请求路径（现有不变量）。
- 只记快照/excerpt，不记全量明文（§6 不记录清单）。

## 11. 分期实施（定稿）

- **M1（骨架 + 主链，唯一目标：发一条消息卡哪步 + 看到 LLM 输入输出）**
  - 后端 backbone：`debug_trace` 扩 `explain`/`content_excerpt`/`dur_ms`/`verbose_enabled` + 护栏；新增 `POST /v1/debug/trace/event` emit 端点；resident reply trace_id glue；后端边界埋点 `route.chat.message`/`reply.stored`/`memory.capture.*`。不做服务端分组。
  - consumer（`tools/chat_resident_consumer.py`）：`context.build` + `agent.model.call.start/done/error`（复用 `_log_cli_turn_timing`/`_codex_turn_metrics`），经 emit 端点上报，fire-and-forget。
  - iOS：`DebugConsoleView`（模块默认全选 / 搜索 / 状态筛选 / 分组⇄扁平 / 复制单条·整 turn·筛选报告 / 每条"人话+detail+excerpt"）。
  - proactive/perception 模块 chip 保留但显示空态 "暂未接入 (M2)"。
- **M2**：memory 读侧细节、worldbook、genesis(前台+worker)、identity、proactive 桥接、**perception(感知：屏幕分析/phash/上报，含感知内容 excerpt)**。
- **M3**：push、account/attestation、tool 级 `agent.tool.call` + 完整 model call、自动 tail。

## 12. 测试与验收

- **运行时安全（前提①硬验收）**：每个埋点必须包在 try 里、异常吞掉不上抛、不在主 return 路径上；注入一个"故意抛异常的 trace_event"用例，验证**业务请求仍正常返回**（观测坏了业务不坏）。门控关时 `trace_event` 完全 no-op（不读不写 blob）。
- **单测**：content_excerpt 截断（字段/event 上限 + 截断打标）；`verbose_enabled` 开关；门控关时 `trace_event` no-op；verbose 模式 `_MAX_EVENTS` 降档。
- **门控行为**：`FEEDLING_V1_FLOW_TRACE=0` → 警告条 + 零事件；`FEEDLING_DEBUG_VERBOSE=0` → 有 event 无 excerpt。
- **e2e（真实 test 部署，遵循 io 加密 e2e 铁律）**：发一条消息 → 分组视图出现 turn 卡片，含 route→context→agent→reply→capture 全步 + 各步 explain + excerpt；人为制造一步失败 → 该步红条、后续缺口。**特别验 VPS resident 路径下 reply 与用户消息串进同一 turn（glue 生效）。**
- **验收口径**："我做了 X → 分组卡片第 N 步出现/变红 → 链路客观跑到/断在这里"，取代凭感觉。

## 13. 不做（YAGNI）

- 不做跨用户全局日志聚合 / 后台面板（admin 走已有 diagnostics）。
- 不做服务端分组/过滤（全客户端）。
- 不把 proactive eval 指标系统重写进 backbone（M2 薄桥接）。
- 不记全量明文、不落 R2/DB、不在 ring blob 塞大文本（需要时另设 artifact stream）。
- M1 不做 tool 级 / 完整 model call 埋点。

## 14. 决策记录（§14 原开放问题已定）

1. 分组/过滤 → **客户端**。服务端返回扁平 events，仅 `limit` + 单 subsystem 兼容。
2. `hosted/turn.py` 埋点密度 → **M1 = turn 级 + 模型调用边界一处(`agent.model.call.*` 带 prompt/输出 excerpt + 挂起可见)**；工具级 `agent.tool.call` + 每个内部 round 完整原文放 M2/M3。（回应 hx 第一痛点：LLM 输入输出/卡在哪必须 M1 可见。）
3. content 护栏 → verbose `_MAX_EVENTS` 150–200、excerpt ≤4KB/event ~1KB/字段、只 excerpt；大文本另设 artifact stream。
4. `trace_id` 复用消息 id → **成立，但必须补 glue**：`/v1/chat/response`(418) 与 gated(286) 显式 `trace_id = reply_to_message_id or msg["id"]`。
5. proactive 桥接 → **M2**；M1 保留 chip 空态。
