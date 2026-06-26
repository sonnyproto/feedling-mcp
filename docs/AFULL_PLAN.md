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

### Phase 0(本轮,并行)
- **Claude**:① 本 plan;② `io_cli.py` 加**读子命令** `memory index/fetch`、`screen read/recent`(明文安全,无加密);③ 插件 `index.js` 注册 `memory_index/memory_fetch/screen_read/screen_recent`(provider-safe 名);④ 本地自测。
- **Codex**:backend —— ① 把 memory **写动作** add/supersede/delete/retype 接进 `tool_catalog_v2` + `tool_executor_v2` 的 HTTP 路(**作为 interim,即刻可用可测**),floors/anchor 校验透传为工具可读错误;② 补/改对应测试;③ 不动选路逻辑。

### Phase 1(Phase 0 全绿后)
- **Claude**:io_cli/consumer 协作做 `memory add`(走 D1 加密方案)+ 插件 `memory_add` 工具。
- **Codex**:proactive 选路:读类工具先切原生 `call_agent`,写类暂留 loop,直到原生写验证通过。

### Phase 2(退役)
- **Codex**:原生覆盖全部工具后,proactive 全切原生;删 `run_tool_loop_v2`/`_resident_run_agent_v2`/`_resident_call_tool_v2`/`/v1/proactive/tool/execute` + 相关测试;按 D2 处理 budget。
- **Claude**:插件收尾 + skill.md(io-onboarding,**test 分支**)文档化新工具 + cost 引导。

### Phase 3(联测,EP1 同款四方)
1. Claude 本地 → 2. Claude VPS e2e → 3. Codex 本地 → 4. Codex VPS e2e。

## 4. 测试矩阵(每阶段)
- 感知:perception pull / trend / history(回归,A-lite 不能坏)
- memory:index/fetch 读 + add/supersede/delete/retype 写(含 floors/anchor 非法卡被拒)
- screen:read(caption)/ recent
- proactive:manual+auto tick → enqueue → consumer claim → 工具调用 → send_message → 投递门
- 三开关 gating + timer fire
- 退役回归:删 loop/route 后上述全部仍通

## 5. 风险与红线
- 红线:**绝不为测试通过牺牲功能**;**绝不误删 VPS 用户数据**(用合成/可恢复数据,改状态必恢复)。
- 退役 route 前必须确认 `/v1/proactive/tool/execute` 零生产调用(已确认唯一调用方是将被删的 loop)。
- agent_runtime(zhihao)托管的 API-key 用户跑同一 consumer → 插件改动对其同样生效,联测需覆盖(或与 zhihao 对齐其 roster 测试用户)。
