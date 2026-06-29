# A-full 计划 — 退役 proactive 双路,memory/screen 原生插件化

> 立项 2026-06-26。承接 A-lite(perception 已统一到 `/v1/agent/perception` 真源)。
> 前置阻塞「工程师的 memory v1」已落地(`f7e3db7` 等),已审计无 resident 回归。
> 协作:Claude(插件/CLI/docs/审计 + 把关)+ Codex(backend 主体)。

## 0. 目标与背景

今天 proactive wake 有**两条平行工具路**:

| 路 | 触发 | 工具执行 | 问题 |
|---|---|---|---|
| **原生**(chat/CLI)| `call_agent` | OpenClaw 插件 `feedling-io-tools` → `io_cli.py` → `/v1/agent/perception` | 只有 perception |
| **模拟**(proactive V2)| `run_tool_loop_v2` | agent 返 JSON tool_calls → `/v1/proactive/tool/execute`(`tool_executor_v2`)| 把 agent 当裸模型;扛 memory+screen+action |

A-full = 把 memory+screen 也做成原生插件,proactive 全走原生 `call_agent`,**退役 `run_tool_loop_v2` + `_resident_run_agent_v2` + `_resident_call_tool_v2` + `/v1/proactive/tool/execute`**。

**为什么现在能做**:① memory v1 后端读写已落地;② Seven 澄清 agent_runtime = 给 API-key 老用户在 CVM 里替跑同一个 resident consumer → **原生插件接一次,VPS + API-key 用户全覆盖**。

## 1. 现状关键事实(已审计,带 file:line)

- 选路:`tools/chat_resident_consumer.py:3671` `use_runtime_v2 = _resident_runtime_v2_enabled_for_job(job)` → V2 走 `_resident_run_agent_v2`(2518–2562)→ `run_tool_loop_v2`(2562)。
- HTTP 执行口:`backend/proactive/routes.py:555` `/v1/proactive/tool/execute`,**唯一生产调用方**是 consumer `_resident_call_tool_v2`(2490–2515)+ 测试。
- 原生插件:`deploy/openclaw-plugins/feedling-io-tools/{index.js,openclaw.plugin.json}`;`tools/io_cli.py` 子命令仅 `perception` / `perception-trend` / `perception-history`(stdlib-only,urllib)。
- 工具目录:`backend/proactive/tool_catalog_v2.py:72-99` —— memory.index/fetch、screen.read/recent、action* 都在,但**只在 HTTP 路实现**。memory 写动作(add/supersede/delete/retype)**proactive 未接**。
- 加密边界:memory 读明文安全;memory 写必须客户端 `build_envelope`(`backend/content_encryption.py:91-138`;consumer 已 import,见 `chat_resident_consumer.py:109-120`)。`/v1/memory/actions` 需 `runtime_auth.authorize_scope("memory")`。
- screen:`screen.recent` 读本地 `db.frame_list_meta`;`screen.read` 走 enclave caption(`backend/screen/caption.py:59-123`)。
- 丢失项:退役 loop 会失去后端 budget/cost 门控(FAST/SLOW、`foreground_chat_fast`、`needs_background` 早停)。

## 2. 两个关键决策(已定默认,Seven 可否决)

- **D1 — 加密边界放哪**:io_cli 做 memory.add 需要 X25519+ChaCha20。**默认:不破坏 io_cli 的 stdlib-only**,memory.add 由 **consumer 侧**用已有 `build_envelope` 封装后调 `/v1/memory/actions`(io_cli 只做读 + 透传 plaintext draft 给 consumer 封装)。备选:io_cli 引入 `cryptography`(破 stdlib-only)。
- **D2 — budget/cost 门控**:退役后端 loop 后,FAST/SLOW 引导移到 **skill.md**(io-onboarding)+ 插件工具描述里标注 cost,靠 agent 自律 + 插件对 slow 工具返回可用性提示。不再做硬 budget 拦截(符合「中立、不预设」)。

## 3. 分阶段(安全增量,绝不为退役牺牲功能)

### Phase 0 ✅ 已完成(commit 94c2f69)
- **Claude**:`io_cli.py` 读子命令 `memory-index/memory-fetch/screen-recent/screen-read`(明文安全)+ 插件注册原生工具 + 文档;VPS 实测通过。
- **Codex**:memory 写动作 add/supersede/delete/retype 接进 `tool_catalog_v2` + `tool_executor_v2` HTTP 路(写入原语),floors/anchor 透传 + 明文拒绝(`needs_client_encryption`)。测试 executor 18 + resident 主线 253 passed。

---

### Phase 1(本阶段)— 落卡 Capture Lane(对齐《落卡+Dream 完整方案》)

**核心认知(Seven 2026-06-26 + double check):** 落卡的主力机制 = **会话断点触发的回顾落卡**,不是 agent 每轮主动调工具。当前 VPS 用户**几乎没有自动落卡**(hosted turn-cadence capture 走不到、resident 只有少见的当轮 emit、断点回顾没建、Dream 没建)。Phase-1 修这个。

**架构裁定(Claude + Codex 对齐):独立 capture lane,复用 job 原语,不复用 proactive reach-out 语义。**
- 不要"proactive_jobs 加 kind 然后到处 if";不要完全重搭队列。
- 物理可短期复用 `proactive_jobs` stream(log/wake_bus/claim/lease/stale-reclaim),但**逻辑上是独立 lane**:typed job (`job_kind=memory_capture` + `capture_key` 幂等 + window)、独立 trigger gate、独立 handler、独立 status 字段、独立测试。
- **不变量(写测试钉死):关「AI 主动找我」≠ 停记忆。** capture gate 绝不看 ambient/scheduled/delivery/user_state/broadcast。

**PR 顺序(Codex 建议,采纳):**
- **PR A — Capture job 基座**(Codex backend):typed job doc;enqueue helper;`_resident_pollable_pending_jobs()` 对 `job_kind=memory_capture` **跳过 wake gate**;poll 按 kind 标类型;consumer 第一层按 kind 分发到 `_process_capture_jobs` stub;`update_*_job` 扩展 capture status 字段(`capture_result/cards_added/cards_superseded/noop_reason` 等,不塞进 reach-out 字段)。**测试:ambient off 不阻止 capture poll/claim;proactive wake 仍被 ambient gate 阻止(invariant)。**
- **PR B — 触发**(Claude iOS 显式信号 + Codex 后端 coordinator):iOS 报 `app background/screen lock/explicit close/晚安` → `/v1/device/events`;后端 `memory/capture_scheduler.py`(或 `capture/service.py`)在 chat append 后更新 `capture_state` window + 轮数兜底,resident/timer tick 处理**静默超时兜底**(不能只靠 iOS);`capture_state` blob 去重(`last_captured_until_message_id/pending_capture_key/...`)。**测试:同一 window 多事件只 enqueue 一次;无新消息 noop。**
- **PR C — 原生 capture handler**(Claude 落卡 prompt + Codex/Claude consumer):`_process_capture_jobs` 新函数,走原生 `call_agent`(**不走 run_tool_loop_v2**),用方案的落卡 prompt 回看 window + 现有桶/线索 + identity → 产出卡(并入/新增/覆盖/不动)→ consumer 封信封 → `/v1/memory/actions`。**Hard rule:不 post_reply、不过 delivery gate、忽略 agent 返回的 messages。测试:不写 chat;不触发投递;success/noop/failure 都落 status。**
- **PR D — Dream 复用同一 lane**(后):`job_kind=memory_dream`,不同 trigger(夜间/攒量)/prompt(纯整理:合并/厚化/消矛盾)/cadence,同样不走 reach-out gate;红线:只 superseded 不删、重构前备份、不发消息。

**并发红线**:同用户单飞(最多一个 pending/running capture)+ `capture_key` 幂等(app background + 静默 + 轮数同时触发不重复落卡)。

### Phase 2 — 退役模拟工具路(审计后重定义,Seven 2026-06-26 确认方向)

**审计关键发现(`docs/CAPTURE_LANE_VERIFICATION` 第 7 节 + 深审):不能直接删。**
VPS 上 proactive **主动唤醒(reach-out)目前走的就是模拟路**(`FEEDLING_RUNTIME_V2_DEFAULT_ON=true`),
且模拟路是**功能完整**的那条:给唤醒 agent 提供 perception/memory/screen 工具 + send_message/sleep/
schedule_wake/cancel_wake + perception digest 预载。当前 non-V2 "native/legacy" 分支是**退化旧桩**
(无工具、不处理 schedule_wake、不转 send_message)。**直接删 = 主动陪伴回归(丢感知+定时)。**

**正确做法:先把 reach-out 的原生 CLI 路补到同等能力,再退役。**(和落卡同套路:补 handler→切→删)
保留 `tool_executor_v2`/`tool_catalog_v2`(hosted/dashboard/runtime_v2 仍用),只删模拟驱动。

- **P2-1 原生 reach-out 补缺口**(Codex backend + Claude 验):
  - 唤醒走原生 `call_agent`(OpenClaw CLI 运行时,**io_cli 插件已含 perception/memory/screen**,agent 自己拉)。
  - 在 native 路加动作解析:`send_message` / `sleep` / `schedule_wake` / `cancel_wake`(目前 4227 行 schedule 仅 V2)。
  - perception digest 注入 native 唤醒 prompt(或靠 agent 用 CLI 自拉)。
  - 测试:native 唤醒能发消息、能 schedule_wake、能拉感知;invariant 不变。
- **P2-2 切换 + 退役**(Codex):proactive 默认切 native;删 `run_tool_loop_v2`(tool_loop_v2.py)、
  `/v1/proactive/tool/execute` 路由、`_resident_run_agent_v2`、`_resident_call_tool_v2`;
  更新/删测试(test_tool_loop_v2.py、test_proactive_tool_execute_route.py、test_chat_resident_consumer 内 V2 专项);
  ci.yml 同步移除已删测试文件(EP1 同款)。3555 行 `_resident_call_tool_v2("perception.now")` 健康探针改 io_cli。
- **P2-3 VPS e2e**:真机 proactive 唤醒走 native → 发消息/定时/感知都在 + 不回归。

- **Claude**:插件收尾 + skill.md(io-onboarding,**test 分支**)文档化新工具 + cost 引导(D2)。

### Phase 3(联测,EP1 同款四方)
1. Claude 本地 → 2. Claude VPS e2e → 3. Codex 本地 → 4. Codex VPS e2e。

## 4. 测试矩阵(每阶段)
- 感知:perception pull / trend / history(回归,A-lite 不能坏)
- memory:index/fetch 读 + add/supersede/delete/retype 写(含 floors/anchor 非法卡被拒)
- screen:read(caption)/ recent
- proactive:manual+auto tick → enqueue → consumer claim → 工具调用 → send_message → 投递门
- **capture lane**:断点触发 → enqueue(单飞+幂等)→ poll **跳过 wake gate** → handler 落卡(不写 chat/不投递)→ /v1/memory/actions;**invariant: ambient/proactive off 仍落记忆**
- 三开关 gating + timer fire
- 退役回归:删 loop/route 后上述全部仍通

## 5. 风险与红线
- 红线:**绝不为测试通过牺牲功能**;**绝不误删 VPS 用户数据**(用合成/可恢复数据,改状态必恢复)。
- 退役 route 前必须确认 `/v1/proactive/tool/execute` 零生产调用(已确认唯一调用方是将被删的 loop)。
- agent_runtime(zhihao)托管的 API-key 用户跑同一 consumer → 插件改动对其同样生效,联测需覆盖(或与 zhihao 对齐其 roster 测试用户)。
