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

---

## Part 0 · 前置(否则白测)

1. **V2 上线 flag** = 工程内部灰度开关(非用户设置),默认 OFF,需工程师在 test RDS 给测试账号翻开:
   `hosted_wake_runtime_v2_enabled` / `perception_ingress_runtime_v2_enabled`(hosted 路);`resident_wake_runtime_v2_enabled`(resident 路)。
   **注意:把 app 切到测试环境 ≠ 翻开了 flag。** 用 debug 页确认:看到 V2 流(wakes_v2/turns_v2)才算开了。
2. **Debug 页**:`https://test-api.feedling.app/debug/proactive`(HTML)、`/v1/proactive/debug`(JSON)。
   **需要带 api_key 鉴权——浏览器直开会 `{"error":"unauthorized"}`。** 三种方式:
   - **浏览器最省事**:URL 后加 `?key=<你的_api_key>`,即
     `https://test-api.feedling.app/debug/proactive?key=<你的_api_key>`(legacy query 鉴权,会进访问日志,**仅测试环境自用、别分享该 URL**)。
   - curl/JSON:`curl -H "X-API-Key: <api_key>" https://test-api.feedling.app/v1/proactive/debug`(或 `-H "Authorization: Bearer <api_key>"`)。
   - 浏览器插件设 `X-API-Key` header 也行。
   - api_key = 你测试账号的 Feedling API key(iOS app 里那把,不是 OpenRouter 那个)。
   区块:V2 health / wake→turn 时间线 / action·tool / background·scheduled,每条带 status+reason。
3. **memory 重做未完** → 只验机制通断,不评内容质量。

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
