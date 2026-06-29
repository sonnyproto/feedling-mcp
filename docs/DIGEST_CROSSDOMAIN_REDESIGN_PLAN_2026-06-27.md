# B · 主动情境 digest 跨域重构 · 实施计划(2026-06-27)

> 目标:把 wake 注入的 perception digest 从「后端预算 top-N **数值** delta(健康独大)」
> 改成「后端**均衡铺开跨域近况** → Agent 自己判 2-3 条拟人化值得一提」。
> 路径:resident/VPS(保留路径),不碰 hosted。

---

## 一、完整感知接口清单(不遗漏 · 供对比)

### 1.1 信号 × 字段(权威源 `AGENT_SIGNAL_FIELDS`,18 信号)

| 信号 | 档 | 能力 | 字段 |
|---|---|---|---|
| `now` | fast | CTX | local_time, timezone, locale, battery_level, charging, place_label, motion_state, now_playing, broadcast_state, broadcast_active |
| `location` | fast | WAKE+TOOL+CTX | place_label, wifi_label, country, locality, wifi_anchor_id |
| `weather` | fast | TOOL | condition, temperature, apparent_temperature, humidity, precipitation_chance, uv_index, is_daylight, alerts |
| `motion` | fast | WAKE+CTX | motion_state |
| `calendar` | fast | TOOL+CTX | calendar_next_event, calendar_events, calendar_events_truncated |
| `focus` | pull | CTX | focus_authorization_status, in_focus |
| `audio_route` | pull | TOOL | output_type, is_bluetooth, device_name |
| `app` | pull | TOOL+CTX | app_name, app_category |
| `steps` | slow | TOOL | step_count |
| `sleep` | slow | TOOL | asleep_minutes, core_minutes, deep_minutes, rem_minutes |
| `workout` | slow | TOOL | workout_type, duration_min, count_today |
| `vitals` | slow | TOOL | resting_heart_rate, step_count, current_heart_rate, hrv_sdnn_ms, respiratory_rate, oxygen_saturation_pct, vo2_max |
| `activity` | slow | TOOL | active_energy_kcal, exercise_minutes, stand_minutes, mindful_minutes |
| `body` | slow | TOOL | weight_kg, bmi, body_fat_pct, height_cm |
| `metabolic` | slow | TOOL | blood_glucose_mmol_l, blood_pressure_systolic, blood_pressure_diastolic |
| `cycle` | slow | TOOL | flow_level, is_active_period |
| `mood` | slow | TOOL | valence, valence_classification, kind, label_count, recorded_today |
| `reminders` | slow | TOOL | next_reminder, reminders, overdue_count, due_today_count, reminders_truncated |

> 能力:**WAKE**=可触发主动唤醒 · **TOOL**=Agent 可按需拉 · **CTX**=作为上下文字段。

### 1.1b 内容类(非信号,走独立 pipeline —— 不在 `AGENT_SIGNAL_FIELDS`,但 digest 当"域"用)

| 来源 | 能力 | 字段(代码真值) |
|---|---|---|
| `photos` | WAKE+TOOL | 记录:photo_id, frame_id, status, usable, sensitive;metadata(`PHOTO_METADATA_FIELDS`):has_faces, face_count, scene_hint, scene_confidence, time_of_day, is_burst, is_indoor, has_text_block, is_screenshot, place_label |
| `screen/broadcast` | CTX(状态)+内容走帧 | broadcast 信号:broadcast_state, broadcast_active;帧 meta(解密前):frame_id, ts, app, ocr_text, w, h;解密后(screen-read):+bundle, caption/ocr_text, decrypt_status, image_b64(带图时);summary:current_app, continuous_start_ts |

> 注意重叠:`broadcast_state`/`broadcast_active`/`now_playing` **同时也在 `now` 信号里**,所以 digest 的
> `presence` 能直接从 `now` 快照拿到当下音乐+屏幕状态;**屏幕的内容(字幕/OCR/截图)**才需走帧 pipeline 的 screen-read。

### 1.2 HTTP 端点全集

- 摄入/上报:`POST /v1/perception/report`、`POST /v1/perception/photo/evaluate`、`GET /v1/perception/app_open`(快捷指令)
- 读取(信号):`GET /v1/agent/perception`(snapshot)、`/trend`、`/history`、`/digest`
- 读取(快照/项):`GET /v1/perception/snapshot`、`/photos`、`/photo/<id>/content`、`/items/<kind>`
- 屏幕:`GET /v1/screen/frames`、`/frames/latest`、`/frames/<id>/decrypt`、`/frames/<id>/image`、`/screen/summary`、`/screen/analyze`

### 1.3 Agent 工具(io_cli / OpenClaw,9 verb)

`perception` · `perception-trend` · `perception-history` · `memory-index` · `memory-fetch`
· `screen-recent` · `screen-read` · `photo-recent` · `photo-read`

---

## 二、现状(病根)

`_proactive_perception_digest()` → `(presence, change)`:
- **presence**:place_label / motion_state / now_playing / 电量 / broadcast_state / local_time(当下快照,含音乐)。
- **change**:`/v1/agent/perception/digest` → `notable_changes()` 取 top-8,**只比较量化数值信号**
  (`health_vitals/metabolic/weather/activity/sleep/body/cycle`)。

→ "什么值得主动提"的**排序结构上只能是健康数值**;音乐/地点/app/照片这些拟人化情境永远进不了榜
→ Agent 退化成"身体监测 Agent"。

---

## 三、方案(职责重切)

**后端**:从「算最大的几个数值变化」→「**均衡铺开跨域近况**(数值 + 情境,每域一行,健康折叠成 1 行)」。
**Agent**:从「被喂 top-N 健康 delta」→「看均衡桌面,**自己判 2-3 条拟人化值得一提**」。

### 3.1 后端接口形状(`/v1/agent/perception/digest` 升级)

```json
{
  "presence": { "...当下快照(保持不变)..." },
  "domains": {
    "location": { "now": "公司", "recent": "今天在公司 8h", "novelty": "long_at_work" },
    "media":    { "now_playing": "Phoebe Bridgers — Motion Sickness",
                  "recent": "今天 3 张专辑都是她", "novelty": "new_artist_today" },
    "app":      { "now": "Spotify", "recent_1h": ["Spotify","Messages","Xcode"], "novelty": null },
    "health":   { "summary": "睡 6h10m(↓~50m) · 步数 3.1k(偏低) · 静息HR 71(正常) · 活动 210kcal(偏低)",
                  "notable": ["sleep_down","sedentary"] },
    "weather":  { "summary": "晴 24°C" },
    "mood":     { "summary": "今日未记录" },
    "reminders":{ "due_today": 2, "overdue": ["回复 Liko 设计稿"] },
    "photos":   { "recent_2h": 1, "scenes": ["food"] },
    "screen":   { "state": "off" }
  }
}
```

要点:
- **健康从"8 条数值 delta 霸榜"折叠成 1 行 `health.summary`**,与 media/location/app/photos **平级**。
- 每域可带**轻量 `novelty`**(new_artist / new_place / long_at_work…)——这是**事实性上下文**,
  不是跨域排名。**2-3 条拟人化 flag 不在后端产**,由 Agent 读完整桌判。
- 缺数据的域 → `summary` 直接说"未记录/无",诚实报空,不编。

### 3.2 消费端 / prompt 改法
- `_proactive_perception_digest()` 第二半从 `notable_changes` 列表 → 改取 `domains` 字典。
- wake prompt:把"top-N 变化"段 → 换成"跨域近况桌面",并加一句指令:
  「从以下跨域近况里,自己判断**最多 2-3 条**真正值得主动提的(可跨域组合,优先拟人化情境而非健康播报);
  若都不值得,可以不发。需要细节用 perception_trend/history 工具自取。」

### 3.3 验收点
1. 同一份输入,新 digest 的 `domains` 覆盖 ≥9 个域,健康只占 1 行。
2. 构造"听了一天某歌手 + 睡眠偏少"场景 → Agent 产出的主动语**以情境/关心为主**,不是"你 HR 71"。
3. 缺权限的域诚实报空,不编造。
4. resident e2e:VPS consumer 真跑一轮 wake,日志里看到 `domains` 注入 + Agent 自判 flag。
5. 不回归:digest 端点旧字段(若别处依赖)兼容或同步迁移。

### 3.4 分工
- **后端(Codex)**:`backend/perception/history.py` / `backend/agent/routes.py` 新增 `cross_domain_recent()` + 升级 digest 端点;health 折叠 + 轻 novelty。
- **我(CC)**:`tools/chat_resident_consumer.py` 取 `domains` + wake prompt 改写;resident e2e 验。

---

## 四、一次循环长什么样(示例)

**触发**:心跳 fire(broadcast=off,60s 档),19:42 周五。
**后端组装的跨域桌面**(= 上面 §3.1 那份 JSON)。

**Agent 读桌面后自判的 2-3 条拟人化 flag**:
1. (media × health)听了一整天 Phoebe Bridgers(emo folk)+ 睡得少 + 久坐 → 可能累/情绪低。
2. (reminders)逾期:回复 Liko 设计稿 → 可行动。
3. 其余(天气/位置/照片)平稳,不值得单独提。

**Agent 产出(主动消息)**:
> 晚上好呀~ 看你今天 Phoebe Bridgers 单曲循环了一整天,是不是有点累?
> 顺便,Liko 设计稿那条还躺在你提醒里逾期了,要不要我帮你钉到明早第一件事?

**对比 · 现状(健康独大)会产出**:
> 你今天睡眠比平时少约 50 分钟,步数 3.1k 偏低,活动能量也不太够哦。

→ 同一份数据,新方案让 Agent **像个会观察的人**,旧方案像**健康播报机**。
