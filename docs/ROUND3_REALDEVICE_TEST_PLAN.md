# Round 3 (Proactive/Perception V2) — 真机测试计划 + 实时状态

分支:`test`(部署在 `https://test-api.feedling.app`)。
本文件**实时更新**:Seven 真机测一项、报一项,审查员(Claude)更新对应行的状态。

状态图例:⬜ 未测 · 🟡 进行中 · ✅ 通过 · ❌ 失败 · 🚫 阻塞(依赖未建/未开)

锚点:`docs/PROACTIVE_PERCEPTION_SPEC_V2.md` 附 A 走查 + §3.1 信号 + §8 三开关 +
§1.4 合并 + §7 定时器 + §6 异步 + §2.1 照片 + §8.4 透明。

---

## 实时状态表

| ID | 测试 | 锚点 | 能否测 | 状态 | 结果/备注 |
|----|------|------|--------|------|-----------|
| **A1** | 前台聊天 user_message 有回应 | §1.1 | ✅ | ⬜ | |
| **A2** | manual 召唤穿透三层(三开关全关也回) | §1.1/§8.4 | ✅ | ⬜ | |
| **A3** | 心跳→睡(稳定时 sleep、不打扰) | 附A①/§4 | ✅ | ⬜ | |
| **B1** | 新照片 → perception_event | §2.1/§3.1 | ✅ | ⬜ | |
| **B2** | 照片连拍 30s 去重(连拍10张=1 wake) | c2e2d45/#2 | ✅ | ⬜ | |
| **B3** | WiFi 锚点:到新地点 wake(后台到达) | 附A②/§3.3 | ⚠️ 需后台定位真机验 | ⬜ | 后端已接(`wifi_anchor_id`→differ,仅 changed=true);iOS 已发指纹+后台 SLC/Visit,**需工程师真机验后台定位 + 装新包** |
| **B4** | 久别解锁 unlock_after_absence | §3.1 | ✅ | ⬜ | iOS 已发(前台回归>30min,gap 端上算)+ 后端已接;**装新包后可测** |
| **B5** | 连续信号**不** wake(步数/位移/电量/在播/天气/健康) | §3.1/D5 | ✅ | ⬜ | |
| **C1** | broadcast 开 → scene_change + pHash 去重 | 附A③/§5 | ⚠️ 部分 | ⬜ | screen.read 未建,只验帧变化+pHash |
| **D1** | 多 wake 单飞(不双发);窗口合并仅非hosted | 附A④/§1.4 | ⚠️ | ⬜ | hosted merge_window=0,验单飞 |
| **D2** | 异步后台 needs_background(用"明天安排"替代步数) | 附A⑤⑧/§6 | ⚠️ | ⬜ | 步数版测不了 |
| **E1** | agent 自己 schedule_wake(带 tz/origin_refs) | 附A②/§7 | ✅ | ⬜ | |
| **E2** | 定时器到点 fired(可设1分钟后) | §7 | ✅ | ⬜ | |
| **F1** | 陪伴关→自发停,但前台/manual 照常 | 附A⑦/§8.2 | ✅ | ⬜ | |
| **F2** | 陪伴关+定时开→闹钟仍响(不连坐) | 附A⑦/§8.3 | ✅ | ⬜ | 最伤信任点,务必验 |
| **F3** | 定时任务关→不能设/不触发+透明告知 | §8.4/PR8 | ✅ | ⬜ | |
| **F4** | 提醒关→醒+写chat但不buzz+透明告知 | 附A⑥/§8.4 | ✅ | ⬜ | |
| **F5** | 深夜 Delivery 自掐(Wake/Voice 照旧) | §8.4/D4 | ✅ | ⬜ | 依赖 iOS 写时区 |
| **G1** | 天气 pull(agent 拉到粗天气 condition/温桶/昼夜) | §2.1 | ✅ | ⬜ | 经 debug 页或问 agent 验;**Apple 后台需开 WeatherKit** |
| **G2** | 健康 pull(步数/睡眠/运动/体征 桶值) | §2.1 | ✅ | ⬜ | 同上;**Apple 后台需开 HealthKit** |
| **G3** | 音频路由 pull(连耳机/车机→agent 知道) | §3.3 | ✅ | ⬜ | 蓝牙锚点的可行子集;含设备名 |
| **G4** | focus pull(开专注模式→`in_focus`=true,agent 拉到) | §3.1/D6 | ✅ | ⬜ | 仅 pull 在场提示,不 wake、不复活 user_state |

> **三个用户开关入口已就位(2026-06-20)。** 后端三字段已在 `test`、`/v1/proactive/state` 都认;iOS Settings 三开关 UI 已 push(陪伴/定时任务/提醒,Scheduled 独立)。装新包后 F1–F5 可测(F5 另需 iOS 写时区)。
>
> **感知打通已完成(2026-06-20,后端 `dad4900` 已部署到 test 环境)。** weather/health/focus/audio_route = 加密 **pull-only**(G 组,装新包后可验);**久别解锁**(B4)iOS 已发+后端已接(装新包即可测);**WiFi 锚点 wake**(B3)后端已接,但实时后台到达**依赖 iOS 后台定位真机验证 + Apple 开 HealthKit/WeatherKit capability**(见 iOS 仓库 `PERCEPTION_HANDOFF_2026-06-20.md`)。
>
> **2026-06-22:** ① test 的 V2 flag **默认开**(见 Part 0.1),不用逐账号翻;② **聊天里 pull 感知**已上(`e14c373`,见 Part 0.5);③ 本轮测试走的是 **resident(VPS)路**,VPS consumer 须最新 test(Part 0.4)。
>
> **2026-06-23(resident 感知端到端打通 + 真机实测):** resident(OpenClaw)经 io_cli 原生工具 + `feedling-io-tools` 插件调 `/v1/agent/perception` **端到端验证通过**——真实聊天里 agent 报出真实**睡眠 390min/位置 outdoor·home/电量 70%**,有真值报真值、无数据如实说空(不编)。修了插件 config 交付崩溃(零硬编码:config→env→报错)+ iOS 聊天 typing 指示器/自递归崩溃。新增 **city `locality`**(iOS `4edf2bd` + 后端 `31ae6c9` 已部署)、**±14天 `calendar_events`** 列表、**日历本地时区**(`a97a73a`)——装新包(⌘R/TestFlight)后可验城市名 + 两周日程真值。仍待:**weather**(WeatherService 抛错,WeatherKit capability,工程师)、**steps 上报节奏**、`focus`/`audio_route` 后端暴露。详见 `PERCEPTION_FIELD_RECONCILIATION_2026-06-23.md` + CHANGELOG 2026-06-23。
>
> **建议起测顺序(由易到难,先打通最短链路):**
> 1. **聊天 pull(最快验)**:聊"我现在在哪?"→ agent 应调 `perception.location`(debug `v2_tool_traces` 可见)。用 location/motion,**不需要 Apple capability**;天气/健康要 WeatherKit/HealthKit 开了才有数据。同时确认记忆召回照常(没被动)。
> 2. 照片 wake(B1)→ 久别解锁 wake(B4)→ 三开关(F 组)→ 其余。
> 3. WiFi 后台到达(B3)/天气健康(G1/G2)等工程师把后台定位真机验 + Apple capability 开了再测。

---

## Part 0 · 前置(否则白测)

1. **V2 上线 flag**(2026-06-22 更新):**test 环境现在默认 ON**(`FEEDLING_RUNTIME_V2_DEFAULT_ON=true` 写进 test compose,镜像已部署)——`perception_ingress_runtime_v2_enabled` / `resident_wake_runtime_v2_enabled` / `resident_chat_runtime_v2_enabled` 无 per-user 显式值时即落到这个基线。**所以一般不用再找工程师逐账号翻。** per-user blob 仍可覆盖(回滚)。
   用 debug 页确认:做一次会触发 V2 的动作后,看到 `v2_wakes/v2_turns/v2_tool_traces` 有数据 = 真生效。(空着不代表没开,可能只是还没触发过。)
2. **Debug 页**:`https://test-api.feedling.app/debug/proactive`(HTML)、`/v1/proactive/debug`(JSON)。
   **需要带 api_key 鉴权——浏览器直开会 `{"error":"unauthorized"}`。** 三种方式:
   - **浏览器最省事**:URL 后加 `?key=<你的_api_key>`,即
     `https://test-api.feedling.app/debug/proactive?key=<你的_api_key>`(legacy query 鉴权,会进访问日志,**仅测试环境自用、别分享该 URL**)。
   - curl/JSON:`curl -H "X-API-Key: <api_key>" https://test-api.feedling.app/v1/proactive/debug`(或 `-H "Authorization: Bearer <api_key>"`)。
   - 浏览器插件设 `X-API-Key` header 也行。
   - api_key = 你测试账号的 Feedling API key(iOS app 里那把,不是 OpenRouter 那个)。
   区块:V2 health / wake→turn 时间线 / action·tool / background·scheduled,每条带 status+reason。
3. **memory 重做未完** → 只验机制通断,不评内容质量。
4. **(resident / VPS 路)VPS 上的 consumer 必须是最新 `test` 分支(含 `e14c373` 聊天感知 + resident 修复)+ 重启**。否则就算 flag 开,resident 聊天也没有感知工具、且可能解析不出回复。注:resident 的 turn 在 VPS 上跑,后端 debug 的 `v2_turns` 可能为 0 属正常;但**聊天里 pull 感知会经 `/v1/proactive/tool/execute` 在后端留 `v2_tool_traces`**,可据此验。
5. **(2026-06-22 新增)聊天里 pull 感知**(`resident_chat`/`hosted_chat_full_tool_loop` flag):在聊天里问相应问题,agent 应**叠加**调对应 `perception.*` 工具(看 debug `v2_tool_traces`),**且不影响工程师的记忆召回**。只暴露**快档**:`perception.now/location/calendar/motion/weather`;慢档(步数/睡眠/体征/屏幕)聊天里有意暂不开放。

## Part 1 · 测试用例详情(对应状态表)

（组 A 基础唤醒、组 B 感知、组 C 屏幕、组 D 仲裁/异步、组 E 定时器、组 F 三开关——
每条的"做什么/期望/在哪验"见会话中给出的清单;状态表为权威进度记录。）

## 已知缺口 / 现在测不了

| 项 | 原因 | 影响 |
|----|------|------|
| ~~三个用户开关~~ | ✅ 已补齐;装新包后 F 组可全验 | — |
| ~~天气/健康/focus/audio_route pull~~ | ✅ 已接(后端 `dad4900` 部署在 test);装新包 + Apple 开 capability 后可验(G 组)| — |
| ~~久别解锁 wake~~ | ✅ iOS 已发 + 后端已接;装新包可测(B4)| — |
| **WiFi 锚点 wake 的"实时后台到达"** | 后端 differ 已接、iOS 已加低功耗后台定位(SLC/Visit),但**整条后台链我无法 build/真机验**;Always 授权流程 / 后台唤起→上报→产 wake 需工程师真机确认 | 🟠 B3 实时到达 wake 待工程师真机验;前台时换网→开 app 也能让锚点上报 |
| **Apple 开发者后台 capability** | App ID 需开 HealthKit + WeatherKit,否则真机授权失败 | 🟠 G1/G2 真机前置,工程师做 |
| 屏幕看懂内容(screen.read caption)| D14 小 VLM 未接 | C1 只验帧/pHash |
| 任意蓝牙锚点 | iOS 不给第三方 app 系统级 BT 连接 | 已用 AVAudioSession 音频路由子集替代(G3)|
| request_broadcast | 未做 | §5.3 |
| hosted 窗口内 wake 合并 | merge_window=0(有意)| D1 只验单飞 |

---

## 测试日志(按时间追加)

_（Seven 报告结果时,审查员在此追加：时间 · 测试ID · 结果 · 证据/dashboard 观察）_

### 2026-06-23 · resident(VPS/OpenClaw)路真机实测
- **A1 前台聊天有回应** ✅:连发多条均秒回(修 resident session 无界超时 `baba9ee` 后),真实对话(非罐头)。
- **聊天 pull 感知(Part 0.5)** ✅ 端到端:真实聊天里 agent 主动调 `perception_*` 并报真值——「我的睡眠」→ 390min/6.5h(=io_cli 真值);「天气」→ 答出 location=outdoor 但 weather 拿不到;「在哪」→ outdoor/home。值与 io_cli 直查**逐字一致,无编造**。
- **机制**:agent 经 `tools/io_cli.py` + OpenClaw `feedling-io-tools` 插件打 `/v1/agent/perception`。插件 config 交付崩溃已修(零硬编码)。
- **G2 健康 pull**:sleep ✅(390min);steps 凌晨为 null=今天还没走(正确,非 bug);心率 null=HealthKit 未产。
- **G1 天气 pull** ❌:恒 null = `WeatherService` 抛错(entitlement 在,疑 Apple Portal WeatherKit capability;Xcode Console 搜 `weather fetch failed`)——**工程师**。
- **日历**:24h 窗口内有会即返回(实测「io」事件,07:00Z=本地 15:00);±14天列表 + 城市 locality + 本地时区已部署,**装新包后复验**。
- 字段语义全量对齐见 `PERCEPTION_FIELD_RECONCILIATION_2026-06-23.md`。
