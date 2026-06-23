# 感知字段 iOS ↔ 后端 对齐审计 + 修正清单（2026-06-23）

> 目的：以 **iOS 上报端为准**，逐字段核对「iOS 实际采集/上报」与「后端实际接收/存储/暴露给 agent」，
> 标出**现在能拿到 / 拿不到**，拿不到时**block 在哪一端**，交后端 + iOS 工程师修正。
> 证据均来自代码审计（非猜测）。**不阻塞** agent 调用能力的实测。

来源：
- iOS：`feedling-mcp-ios` — `PerceptionContextSnapshot.swift`、`PerceptionLocalResolver.swift`、`PerceptionPermissionsManager.swift`、`FeedlingAPI.swift`
- 后端：`feedling-mcp-v1` — `perception/catalog.py`、`ios_contract_v2.py`、`resolve.py`、`service.py`、`agent/routes.py`

---

## TL;DR（先看这段）

1. **契约是 1:1 的，没有"后端凭空捏造字段"的问题。** 后端 `EXPECTED_REPORT_KEYS_V2` 与 iOS `PerceptionContextSnapshot.Key` 逐字对齐；`timezone`、`temperature_bucket` 等你怀疑的字段在 iOS 端**确实存在**。后端 location resolver **不丢任何字段**（4 个 output 全存）。
2. **现在 agent 实测能稳定拿到**：time、battery、broadcast、calendar、sleep、steps（+ place_label 但值恒为 `outdoor`）。
3. **拿不到的真因不是 schema 错位，而是 5 类 block：**
   - **A. iOS 定位 fix 不新鲜** → 连带 `country`、`weather`(condition/temp/is_daylight) 都拿不到（**同一个根因**）。
   - **B. 用户没配 geofence** → `place_label` 永远是 `outdoor`/`unknown`，agent 拿不到"在家/公司/在深圳"。
   - **C. HealthKit / WeatherKit 设备端没产数据**（心率 `暂无数据`；天气 `暂无数据`）。
   - **D. TTL 新鲜度**：App 切后台超过 TTL（time/motion=5min）→ 该轮 snapshot 返 null（下次前台上报即恢复，非 bug）。
   - **E. 隐私设计**：城市名「深圳市」、亮度、系统版本**根本不在上报契约里**（仅本地 UI 显示）。
4. **一个确定的 agent 体验 bug（D8）**：weather/定位类信号"不可用"时，后端返**静默 null** 而非 `{disabled, reason}`，agent 不知道为什么没数据。原因是 iOS 的 message 文案与后端的识别 marker 没对齐（见 [后端 action #1]）。

---

## 表 1 · 每个感知能力现状（以 agent 经 `/v1/agent/perception` 实际拿到为准）

| 能力 | agent 现在能拿到? | 实测/现状 | 拿不到时 block 在哪 | 性质 | Owner |
|---|---|---|---|---|---|
| **time** (local_time/timezone/locale) | ✅ 能 | 偶发 null = 后台超 5min TTL | TTL 新鲜度 | 设计 | — |
| **battery** (level/charging) | ✅ 能 | 40%/充电中 ✓ | — | — | — |
| **broadcast** (屏幕采集状态) | ✅ 能 | ✓ | — | — | — |
| **focus** (是否专注) | ⚠️ 看权限 | 二值，拿不到具体模式 | Focus 权限未授；iOS 只给二值 | iOS 限制 | — |
| **location.place_label** | ⚠️ 恒为 outdoor | `outdoor` | **B：用户没配 home/work geofence → resolver 回退 outdoor** | **需产品决策** | **iOS** |
| **location.country** | ❌ null | null | **A：上报时无新鲜定位 fix；Locale.region 兜底也空** | bug/可改 | **iOS** |
| **location.wifi_label / wifi_anchor_id** | ⚠️ 看 WiFi | null | 当前未连 WiFi，或缺 Local Network 权限 | 多为预期 | iOS（确认权限） |
| **motion** (motion_state) | ⚠️ 看权限/新鲜度 | null | Motion 权限未授 / 近 15min 无活动 / TTL 5min | 多为设计 | iOS（确认权限） |
| **calendar** (calendar_next_event) | ✅ 能 | "Weekly Feedling..." ✓ | — | — | — |
| **now_playing** | ⚠️ 看权限/播放中 | stopped | Media Library 权限未授 / 没在放 | 预期 | — |
| **audio_route** | ⚠️ 看输出设备 | — | 无音频输出路由 | 预期 | — |
| **weather** (condition/temp/is_daylight) | ❌ 全 null | iOS 自身`暂无数据` | **A：无新鲜定位 fix / WeatherKit fetch 失败 / 权限**（设备 iOS 26，非版本问题）+ 后端 D8 缺口 | bug | **iOS + 后端** |
| **sleep** (asleep_minutes_bucket) | ✅ 能 | 6h28m ✓ | — | — | — |
| **steps** (step_count_bucket) | ✅ 能 | 1292 ✓ | — | — | — |
| **vitals.resting_heart_rate** (心率) | ❌ null | iOS 自身`暂无数据` | **C：HealthKit 心率没产数据 / 未授权 / 设备端 capability** | bug | **iOS / 工程师**（已知） |
| **workout** | ⚠️ 看是否有运动 | null | 今天可能确实没运动 | 多为预期 | — |

> 注：encrypted 信号（location/motion/calendar/playback/audio/weather/health_*）后端要经 enclave 解密；解密后**逐字段平铺**进 state，已 live-verified 路径通（calendar/sleep/steps 都有真值）。

---

## 表 2 · 不在感知上报契约里的字段（UI 有、但 agent 拿不到；后端也没声称有 → 非错位）

| UI 显示 | 在上报契约里? | 现状 | 若要 agent 能用 |
|---|---|---|---|
| 位置「深圳市」(城市名) | ❌ 不在 | 本地反地理编码出来仅供 UI；geofence 匹配后**坐标即丢**，不上报 | **产品决策**：是否上报一个粗 locality（城市级）标签 |
| 亮度 75% | ❌ 不在 | 仅本地 UI 读取 | 需双端加字段才能给 agent |
| 系统 iOS 26.5.1 | ❌ 不在 | 仅本地 UI 读取 | 需双端加字段才能给 agent |

---

## 表 3 · 其它来源字段（非感知 report 路，合理存在，非捏造）

| 字段 | 来源端点 | 说明 |
|---|---|---|
| `user_state` (=default) | 手动 POST | 用户手动状态，默认 default，不是感知信号 |
| `app_name` / `recent_apps` | iOS 快捷指令 `/app_open` | 不走 PerceptionContextSnapshot |
| photo / caption | `/v1/perception/photo/evaluate` | 独立照片管线，EXIF GPS 不上行、像素走加密信封 |
| `last_unlock_ago_sec` / `screen_phash` | `/device_event` | 设备事件（解锁/屏幕），唤醒判定用 |

---

## ▶ iOS 工程师 — 修正项

1. **【高】定位 fix 新鲜度（解锁 country + weather 两个）**
   `country` 与 `weather` 同根：上报时若没有新鲜的 location fix（`recentLocationFix()` 为 nil / 缓存过期），country 反地理编码不跑、weather 不取。
   - 上报前确保拿到一次定位 fix（或放宽新鲜窗口 / 触发一次主动定位）。
   - `country` 在无 fix 时应可靠回退到 `Locale.current.region?.identifier`（现在可能也为空，确认）。
   - 证据：`PerceptionPermissionsManager.swift` country 逻辑（Locale.region → reverse-geocode ISO）、weather gate（iOS16+ / location 权限 / fresh fix）。

2. **【高/产品】place_label 恒为 `outdoor`**
   resolver 需要用户预先配 home/work geofence，否则回退 `outdoor`。请确认：
   - App 里**有没有**让用户设置 geofence 的入口？若没有 → agent 永远只拿到 `outdoor`，等于位置信号近乎无用。
   - 决策：要么加 geofence 配置 UI，要么**上报一个城市级粗 locality 标签**（用户说"location 应正常上报"，当前等于被隐私回退 black 了）。
   - 证据：`PerceptionLocalResolver.placeLabel(...)` → `return bestLabel ?? "outdoor"`。

3. **【中】weather 不可用时的 message 文案对齐后端 marker**（配合后端 #1）
   现在 iOS 发「当前系统版本不支持 WeatherKit」「暂无新鲜定位 fix 或天气数据」——后端识别不了 → agent 收静默 null。
   要么让"不可用"类 message 含后端能识别的词（如 `未授权` / `不可用`），要么由后端放宽（见后端 #1）。

4. **【中】确认 HealthKit 心率 + WeatherKit 的 capability/entitlement 真生效**（已在排查）
   心率 `暂无数据`、天气 `暂无数据` 都指向设备端没产数据。确认 Apple Developer Portal 的 HealthKit / WeatherKit capability 已开且 entitlement 生效。

5. **【低】确认 Motion / Local Network 权限态**：motion 与 wifi_* 的 null 多半是权限/未连 WiFi，确认权限弹窗有正常请求即可。

---

## ▶ 后端工程师 — 修正项

1. **【高】weather/定位"不可用"返静默 null，应返 `{disabled, reason}`（D8 透明性）**
   `agent/routes.py:_null_state_message_reason` 对 weather 要求 message 同时含 `weatherkit` **且** `不可用`（line 145）才判 `not_permitted`；iOS 实际发的「不支持 WeatherKit」「暂无新鲜定位 fix」都不匹配 → agent 拿到 `{condition:null,...}` 静默 null，不知为何。
   - 放宽 weather/location 的不可用识别（如含 `weatherkit`/`定位`/`不支持`/`暂无` → 返一个 reason，例如 `unavailable`），或与 iOS 约定统一文案（配合 iOS #3）。
   - 让 agent 至少能透明告诉用户"天气暂时拿不到（定位/能力原因）"。

2. **【低/确认】location resolver 的 raw 坐标分支在 V2 是死代码**
   `resolve.py` location_signal resolver 仍读 `signal.latitude/longitude` 重算 place_label；但 V2 iOS **不再上报原始坐标**，实际走 iOS 已解析好的 `place_label` hint。确认这条 raw 分支无副作用即可（非 bug，清理项）。

3. **【信息】无字段丢弃 / 无凭空字段**：审计确认后端不丢 iOS 任何字段，也没有 iOS 不存在的接收字段。`now.time` 只是 `local_time` 的别名（routes.py:156），冗余无害。

---

## 附 · 字段级 1:1 对照（iOS 上报 → 后端 output，全部对齐）

| iOS key | iOS 上报字段 | 后端 catalog outputs | TTL |
|---|---|---|---|
| time | local_time, timezone, locale | 同 | 300s |
| battery | level, charging | battery_level, charging | 600s |
| broadcast | state, active | broadcast_state, broadcast_active | 300s |
| focus | authorization_status, focused | focus_authorization_status, in_focus | 300s |
| location_signal | place_label, wifi_label, country, wifi_anchor_id | 同 | 900s |
| motion_state | state, confidence, started_at | motion_state | 300s |
| calendar_next_event | title,next_event_time,end_time,event_kind,attendee_count,is_all_day,duration_min,minutes_until_start | calendar_next_event | 3600s |
| playback | now_playing{playback_state,title,artist,album_title,media_type,duration} | now_playing | 600s |
| audio_route | output_type, is_bluetooth, device_name | 同 | 600s |
| weather | condition, temperature_bucket, is_daylight | 同 | 1800s |
| health_sleep | asleep_minutes_bucket | 同 | 86400s |
| health_workout | workout_type, duration_min_bucket, count_today | 同 | 86400s |
| health_vitals | resting_heart_rate_bucket, step_count_bucket | 同 | 3600s |

iOS 明确"拿不到、上报为 null 占位"（`UnsupportedData`，后端 `IGNORED_KEYS` 静默忽略）：`frontmost_app`、`silent_mode`、`precise_unlock`。
