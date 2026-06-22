# Resident 感知能力 CLI — 设计 + 合同

> 目标:让 resident(VPS)上的自主 agent(OpenClaw / Hermes / Claude Code)能**原生调用**
> Feedling 感知/记忆等工具(真 agentic pull),而不是靠"prompt 让它吐 JSON"那个对自主
> agent 不成立的 hack。方式 = 一个**薄 CLI**,agent 把它注册成自己的工具。
>
> 状态:2026-06-23 启动。CLI(tools/)= Claude 做;后端 agent-facing verbs = Codex 做;
> 本文件是两边对齐的**合同**。
> 后端 MVP:`GET /v1/agent/perception` 已接入 test 分支(2026-06-23);Phase 2 verbs 仍按 §3/§4 单开。

## 0. 形态与配置
- 一个 CLI 脚本(`tools/io_cli.py`),随官方 consumer(feedling-mcp `test` 分支)分发——
  resident 机器已有这份 checkout。agent 注册成工具:`python <repo>/tools/io_cli.py <verb> [...]`。
- 配置走现成 3 个 env(consumer 已在用):`FEEDLING_API_URL`、`FEEDLING_API_KEY`、`FEEDLING_ENCLAVE_URL`。
- 鉴权:`X-API-Key: $FEEDLING_API_KEY`(两头都用,已确认 enclave 也认 X-API-Key)。
- 输出:**JSON only**(agent 解析)。错误也是 JSON(`{"ok":false,"error":...}`)。

## 1. 两头路由(架构必须,不是方便)
| verb | 打哪头 | 说明 |
|---|---|---|
| `perception now/location/weather/motion/calendar/steps/sleep/workout/vitals` | **主后端** `FEEDLING_API_URL` | 粗值,不需解密;agent 自己接受延迟,无软交棒 |
| `memory index` / `memory fetch --ids ...` | **enclave** `FEEDLING_ENCLAVE_URL` | 已有 `/v1/memory/index`、`/v1/memory/fetch`;必须直连 enclave(过主后端=破边界) |
| `photo recent`(内容/caption) | **enclave** | 照片走帧通道 `/v1/screen/frames/<id>/{decrypt,caption,image}`(id=photo_id) |
| `send "<text>"` | **主后端** | (Phase 2)agent 主动发消息 |
| `wait-for-wake` | **主后端** | (Phase 2)§1.4 合并 inbox,见 §3 |
| `schedule-wake --at --note` | **主后端** | (Phase 2)agent 自埋定时器 |

**解密读(memory/photo/frame)只能 CLI 直连 enclave,绝不过主后端。** enclave 鉴权 = X-API-Key
(consumer 现在就是 `httpx verify=False` + X-API-Key;CLI 照搬即可跑通)。

## 2. 后端 MVP 合同(Codex 做) — perception 读
新增一个 agent-facing 读端点(建议 `GET /v1/agent/perception`,或复用 `/v1/proactive/tool/execute`):
- 入参:`?signals=now,location,weather,motion,calendar,steps,sleep,workout,vitals`(或单个 verb)。
- 出参:`{"ok":true,"signals":{"<name>":{...coarse fields...}}}`;字段沿用 catalog outputs
  (weather=condition/temperature_bucket/is_daylight;location=place_label/wifi_label/country;
  health=各桶值;motion=motion_state;now=time/battery/place_label/motion/now_playing/broadcast 等)。
- **开关门控(服务端)**:某信号被用户关闭/未授权 → 该信号返回 `{"disabled":true,"reason":"<switch_off|not_permitted>"}`,
  **不报错**,让 agent 透明告知用户(D8)。**iOS 一翻开关,下次调即生效,CLI/agent 不用重配。**
- 数据来源:复用现有 `perception.service.pull_snapshot` / `ToolExecutorV2` 的 perception 适配器
  (粗状态已明文在 state,不碰 enclave);**注意:这是 agent 直接 pull,不是 hosted 聊天那个快档软交棒——slow 信号直接返回,不软交棒。**

## 3. Phase 2 合同 — wait_for_wake = §1.4 合并 inbox(Codex,**别做成双 poll**)
⚠️ **不要**把 `wait-for-wake` 做成"同时 poll `/v1/chat/poll` + `/v1/proactive/jobs/poll`"——那会让 resident
重新长出多源口吃(agent 从两条流各拿一份、各起 turn、各发气泡)。

**正确**:wait_for_wake 是 §1.4 `RuntimeSpineV2`/`WakeInboxV2`(已存在)在 resident 路的**唯一对外面**:
- **所有 wake 源(含 `user_message`、`perception_event`、`scheduled_wake`、`background_result`)submit 进这一个 inbox**,不再走分开的 chat/jobs poll。
- agent 跑 turn 期间持 single-flight lease(`DBTurnLeaseRegistryV2`);新 wake 在服务端缓冲;
  agent 报完一轮释放 lease → 下次 `wait-for-wake` **把缓冲合并成一批返回**(`merged_triggers`)。
- 即:**单飞 + 合并住在 wait_for_wake 后面**,这是 §1.4 在 resident 的落点。infra 现成,缺的是
  这个"基于 inbox + turn lease 的 resident 端点"。
- 已知限制(开着、不卡发布):单飞**不抢占**——agent 正跑 turn 时来的 user_message 得等这轮跑完
  才被下次 wait_for_wake 合并带出(resident agent 慢时较明显)。真抢占要 runtime 支持中断 turn,后议。

`send` / `schedule-wake`:agent 主动发消息 / 自埋定时器,落到现有 proactive 写路 + `ScheduledWakeServiceV2`。

## 4. 分期
- **MVP(先做)**:perception 读 verbs(§2)后端端点 + CLI 的 `perception` 子命令 + 服务端开关门控。
  → 实现"聊天里 agent 能调感知"。
- **Phase 2**:`wait-for-wake`(§3 合并 inbox)+ `send` + `schedule-wake`——让 VPS agent 真"主动";
  以及 `photo recent`(走 enclave 帧通道)。
- **不在 CLI 范围**:**记忆召回是工程师的 readside 路径(不重复造)**。注意 enclave 的
  `/v1/memory/index|fetch` 是"调用方传密文 moments 进去解"——要 CLI 自己做记忆,还得先有个
  "列密文 moments"的后端端点 + CLI 编排(GET 密文 → POST enclave 解);MVP 不做,保持记忆归工程师。

## 5. 安全注意(非阻塞)
- consumer/CLI 连 enclave 用 `verify=False`(不校验 enclave attestation,信 URL)——既有弱点,
  CLI 照搬即同样不校验;更严应发 key 前验 `/attestation`。单独 hardening,不卡。

## 6. 验收(Claude)
- MVP:CLI `perception now/weather/location` 经主后端返粗值 + 开关关时返 disabled;`memory fetch`
  经 enclave 返明文(主后端日志无明文)。真机:agent 注册 CLI 后聊天里能调、debug 可见。
- Phase 2:wait_for_wake 多源合并(造 2 个近同时 wake → 一次 turn、一条/合并气泡,不口吃);
  单飞(并发不双发)。
