# Bug 报告：托管(model_api)用户的记忆/上下文/时间问题

- **作者**：Claude（审计侧）
- **面向**：codex + zhihao（后端）
- **触发**：生产用户 `usr_efacd3b5b21bf7cf` 反馈"记忆不看、不读上下文、时间错乱"；结合 `usr_dded91ac54fb1f58`(海豹) 等托管用户
- **分支**：test（改动只落 test，永不动 main）
- **调查方式**：生产 admin data-track 只读探查 + 代码追踪(未改任何运行时代码)

---

## 0. 先摆一个架构事实(后面所有结论的前提)

`model_api`(API key 托管)用户的每一轮聊天，**后端不是内联调模型**，而是在 CVM 里跑一个**进程外的 CLI agent**(`claude -p` / `codex exec`)，由 `tools/chat_resident_consumer.py` 驱动。
- 送信路径：`hosted/chat_send_core.py:32 model_api_chat_send_core` → `:118 store.append_chat("user",…)` → `:134 agent_runtime_cutover.handle_send(...)` → `hosted/agent_runtime_cutover.py:318 handle_send` **只等回复、不组 prompt、不调模型**。
- 因此 **prompt/context 是在 resident consumer 里拼的**，`hosted/turn.py`/`hosted/context.py`/`model_api_runtime/*` 那套内联组装是**已废弃死路径**(`_run_model_api_memory_tool_loop` 无调用者；`_model_api_context_messages` 仅在 `app.py:477` legacy 别名)。

给模型的东西实际只有：① system prompt = `agent_runtime/agent_tools_prompt.md`(工具用法)；② 记忆 = **拉取式 CLI 工具**(不预注入)；③ 聊天历史 = 消息串，可能被前置一段转录；④ 时间锚点/worldbook/screen。**身份也不作为结构化 context 注入**。

---

## 1. 【真 bug】时间错乱：无日期记忆被回退成"导入当天"

**现象**：`usr_efacd3b5…` 关系起点 `rel_started=2026-05-15`，但所有导入记忆 `earliest_occurred=2026-06-30T15:21` = `first_created`(导入时刻)。→ AI 眼里"所有事都发生在 6-30"，推时间就乱。

**根因(两处都把无日期回退成 now/today)**：
- `backend/genesis/service.py:376`：`"occurred_at": _text(item.get("occurred_at") or _now_iso(), 80)`
- `backend/hosted/history_import.py`(落盘路径)：`envelope["occurred_at"] = str(card.get("occurred_at") or date.today().isoformat())`

**约束**：`occurred_at` 是**必填排序键**(`backend/memory/actions.py:190` `occurred_at_required` "required as plaintext metadata for memory ordering")；`context_memory_selection.py` 和 `hosted/turn.py` 多处按 `occurred_at` 排序。所以**不能直接删**。

**建议修法(解耦"排序时间"与"事件时间")**：
1. 无真实事件日期时，**不要用 now()/today() 伪造**；记为 `undated`(occurred_at 留空或打标记)。
2. 排序需要时间戳 → 用 `created_at` 作为 undated 记忆的排序 fallback(代码内部)，**但不把它当"事件发生时间"**。
3. 喂给 AI/渲染时：只对**有真实日期**的记忆显示日期；undated 的不给日期(或"时间不详")。产品语义上：需要精确时间的，文本里通常已写；不需要的，本就不该标一个假日期。

---

## 2. 【非功能 bug，纠正之前判断】type=unknown 是遗留显示，不是记忆读不到的原因

**先纠正**：我最初把 `type=unknown` 当成功能 bug，**过头了**。核实后：
- `type` 是**旧分类**(`memory/service.py` `MEMORY_TYPES`=moment/quote/fact/event/insight/reflection → story/about_me/ta_thinking tab)。`f7e3db7`「clean v1 schema」(6-25) 起，新模型是 **`bucket + threads`**，import 落盘路径**有意不再写 `type`**(改写 `bucket`)。
- **记忆检索不依赖 `type`**：`context_memory_selection.py` 按 `occurred_at`+相关性+内部桶(turning/recent/query)选，`type` 只出现在调试 trace(`:303`/`:449`)。
- 所以 `type=unknown` 只是**老 data-track 视图**按已死的 type/tab 分组的显示残留，**不影响 AI 读记忆**。

**真正的小问题(值得清理，非用户可见)**：
- **schema 漂移**：live-capture 写入仍要求/写 `type`(`memory/actions.py:323` + 校验)，import 走 `bucket`。两条写路径不一致。
- data-track 的 memory 列该从死 `type/tab` 改成显示 `bucket`(这是**看板侧**的事，Claude 处理)。

---

## 3. 【需后端定位】记忆不读：拉取式 + codex 沙箱(仅非默认配置)

托管路径记忆是**拉取式**：AI 必须自己调 `io_cli memory-index` / `memory-fetch`(`agent_tools_prompt.md:18-19,37-58`；`tools/io_cli.py:199-266` → POST `/v1/memory/index|fetch`)。**不预注入。**

失败模式：
- **(a) 模型压根没调**：memory 是自由裁量的("purely current-turn questions … answer directly — don't query memory"，`agent_tools_prompt.md:41-42`)。误判相关性就不读。
- **(b) codex 沙箱杀掉 Bash 调用(静默且严重)**：`spawners.py:250-256` —— codex 的 bwrap 沙箱在 TDX CVM 里起不来(用户命名空间被禁)，**每个 io_cli 命令都无法启动，agent 报"读不到记忆"尽管数据在**。
  - **但默认 codex 命令已带 `--dangerously-bypass-approvals-and-sandbox`(`spawners.py:273`)** → 默认配置下沙箱不是问题。
  - **只有当某用户的 `cli_cmd` 被覆盖、丢了这个 bypass 时才复现。**
- **(c) 读取失败静默**：io_cli 失败返回 `{ok:false}`，prompt 叫 agent"优雅降级、别暴露错误"(`agent_tools_prompt.md:77-79`) → 用户只看到"没记忆",看不到报错。

**请后端确认(data-track 现看不到，见 §5)**：`usr_efacd3b5…` 的 `agent_runtime_driver`/provider、以及**有没有覆盖 `cli_cmd`**。若是默认 codex(很可能，非 anthropic provider)→ 沙箱排除，锅在 (a) 或 §4。

---

## 4. 【真 bug，静默】聊天上下文缺失：历史注入 best-effort 降级

上一轮修复 = `7f3ff26 feat(chat): implement foreground chat context injection for continuity`(zhihao, 07-03)，把**最近 ≤8 轮**对话转录**前置**到消息里；**确实接到了 hosted 路径**：
- `tools/chat_resident_consumer.py:6376` `content = _foreground_agent_message(content, current_ts=ts)`
- `:3374 _foreground_agent_message` → `:3348 _recent_chat_context_for_foreground`；上限 `FOREGROUND_CHAT_CONTEXT_LIMIT=8`(`:306`)。

**问题：它 best-effort，会静默降级成"没有历史"**：
- 依赖 runner 的解密源 `FEEDLING_ENCLAVE_URL` → GET `/v1/chat/history`(`:995,:999`)；**没配好/不可达 → 返回空 → 只 `log.warning` 就把历史丢了**(`:3359-3363`)。
- poll-lease 会把消息行 claim 走(`:931-933`)→ 转录可能取回空/短。
- claude 的 `--resume` 兜底在"已注入历史"时被抑制(`:2968-2974`)；若注入静默失败,两边都没历史。

**建议**：① 注入失败要**可观测**(至少计一条 tracking/metric，别只 log.warning)；② 复核所有托管 runner 的 `FEEDLING_ENCLAVE_URL` 可达性；③ 8 轮上限是否够(长对话会丢更早上下文)。

---

## 5. 【看板侧，Claude 已做】补 driver/provider 盲区

之前 data-track 看不到用户的 driver/provider，导致 §3 无法自查。已在 test 加：detail 视图新增 `runtime` 块 = `{provider, model, driver, codex_transport, cli_cmd_custom, test_status}`(全非密，无 api_key/base_url)。部署后 `GET /v1/admin/data-track/users/<id>` 即可看这用户是不是默认 codex、走不走 gateway。

---

## 汇总 · 行动项

| # | 症状 | 定性 | 归属 | 位置 / 修法 |
|---|------|------|------|------------|
| 1 | 时间错乱 | ✅ 真 bug | 后端(genesis) | `genesis/service.py:376` + `history_import` 的 occurred_at 回退；解耦排序/事件时间 |
| 2 | type=unknown | ⚠️ 非功能 | 看板(Claude) + 后端清 schema 漂移 | data-track 改显示 bucket；live/import 写路径统一 |
| 3 | 记忆不读 | 需定位 | 后端 | 查该用户 driver/cli_cmd(见 §5)；(a)模型未调 /(b)非默认丢 bypass |
| 4 | 上下文缺失 | ✅ 真 bug(静默) | 后端 | `chat_resident_consumer.py:3348-3392`；注入失败要可观测 + 查 ENCLAVE_URL 可达 |

**给后端的最小请求**：(1) 修 §1 的 occurred_at 回退；(2) 查 `usr_efacd3b5b21bf7cf` 的 driver/provider/cli_cmd 回给我(或等 §5 部署后自查)；(3) 给 §4 的历史注入失败加一条可观测埋点。
