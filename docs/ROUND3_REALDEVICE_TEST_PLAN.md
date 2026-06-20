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
| **B3** | 连接性锚点:离家/到新地点 wake | 附A②/§3.3 | ⚠️ 看iOS | ⬜ | |
| **B4** | 久别解锁 unlock_after_absence | §3.1 | ⚠️ 看iOS | ⬜ | |
| **B5** | 连续信号**不** wake(步数/位移/电量/在播) | §3.1 | ✅ | ⬜ | |
| **C1** | broadcast 开 → scene_change + pHash 去重 | 附A③/§5 | ⚠️ 部分 | ⬜ | screen.read 未建,只验帧变化+pHash |
| **D1** | 多 wake 单飞(不双发);窗口合并仅非hosted | 附A④/§1.4 | ⚠️ | ⬜ | hosted merge_window=0,验单飞 |
| **D2** | 异步后台 needs_background(用"明天安排"替代步数) | 附A⑤⑧/§6 | ⚠️ | ⬜ | 步数版测不了 |
| **E1** | agent 自己 schedule_wake(带 tz/origin_refs) | 附A②/§7 | ✅ | ⬜ | |
| **E2** | 定时器到点 fired(可设1分钟后) | §7 | ✅ | ⬜ | |
| **F1** | 陪伴关→自发停,但前台/manual 照常 | 附A⑦/§8.2 | ✅* | ⬜ | *依赖开关入口,见下 |
| **F2** | 陪伴关+定时开→闹钟仍响(不连坐) | 附A⑦/§8.3 | ✅* | ⬜ | 最伤信任点,务必验 |
| **F3** | 定时任务关→不能设/不触发+透明告知 | §8.4/PR8 | ✅* | ⬜ | |
| **F4** | 提醒关→醒+写chat但不buzz+透明告知 | 附A⑥/§8.4 | ✅* | ⬜ | |
| **F5** | 深夜 Delivery 自掐(Wake/Voice 照旧) | §8.4/D4 | ✅* | ⬜ | 依赖 iOS 写时区 |

> `*` F 组依赖"三个用户开关"的入口。**当前 iOS 没有这三个开关 UI、后端 settings 只有 enabled/dnd、scheduled 无用户入口**(见下"已知缺口")。在补齐前,F 组只能用 legacy `enabled`(≈陪伴)/`dnd`(≈提醒)间接验,**定时任务(Scheduled)单独开关无法验**。

---

## Part 0 · 前置(否则白测)

1. **V2 上线 flag** = 工程内部灰度开关(非用户设置),默认 OFF,需工程师在 test RDS 给测试账号翻开:
   `hosted_wake_runtime_v2_enabled` / `perception_ingress_runtime_v2_enabled`(hosted 路);`resident_wake_runtime_v2_enabled`(resident 路)。
   **注意:把 app 切到测试环境 ≠ 翻开了 flag。** 用 debug 页确认:看到 V2 流(wakes_v2/turns_v2)才算开了。
2. **Debug 页**:`https://test-api.feedling.app/debug/proactive`(HTML)、`/v1/proactive/debug`(JSON)。
   带认证:`curl -H "Authorization: Bearer <api_key>" https://test-api.feedling.app/v1/proactive/debug`。
   区块:V2 health / wake→turn 时间线 / action·tool / background·scheduled,每条带 status+reason。
3. **memory 重做未完** → 只验机制通断,不评内容质量。

## Part 1 · 测试用例详情(对应状态表)

（组 A 基础唤醒、组 B 感知、组 C 屏幕、组 D 仲裁/异步、组 E 定时器、组 F 三开关——
每条的"做什么/期望/在哪验"见会话中给出的清单;状态表为权威进度记录。）

## 已知缺口 / 现在测不了

| 项 | 原因 | 影响 |
|----|------|------|
| 三个用户开关(陪伴/定时任务/提醒)| **iOS 无 UI + 后端 settings 仅 enabled/dnd + scheduled 无入口** | F 组只能间接/部分验 |
| 屏幕看懂内容(screen.read caption)| D14 小模型未接 | C1 只验帧/pHash |
| 步数 | HealthKit iOS 端 0 | D2 用日历替代 |
| WiFi/BT/unlock 离散事件 | iOS 疑似不发 | B3/B4 做了没 wake = 证实缺口 |
| request_broadcast | 未做 | §5.3 |
| hosted 窗口内 wake 合并 | merge_window=0(有意)| D1 只验单飞 |

---

## 测试日志(按时间追加)

_（Seven 报告结果时,审查员在此追加：时间 · 测试ID · 结果 · 证据/dashboard 观察）_
