# Extended Perception — 前端接入文档

后端模块 `backend/perception/`，所有端点前缀 `/v1/perception`。

本文档面向 iOS 客户端接入，**只列前端会调用的接口**（上报 / 配置 / 权限）。
读取类接口（快照、照片列表/内容、集合读取）由 agent / MCP 使用，见文末附录。
Round 3 的感知→wake 方案以 `docs/PROACTIVE_PERCEPTION_SPEC_V2.md` 为准；
本文只保留 HTTP 接入细节，后端实现在 `backend/perception/`。

> **注**：信号集此后已扩展（截至 2026-07 为 19 个信号 + `unsupported`，含
> weather / reminders / health_* 等，见 `backend/perception/ios_contract_v2.py`
> 的 `EXPECTED_REPORT_KEYS_V2` 与 CHANGELOG）。本文 §3 的表只覆盖早期核心信号，
> 未逐一重写；口径以代码 / CHANGELOG 为准。

---

## 0. 通用约定

### Base URL
- 生产：`https://api.feedling.app`
- 本地：`http://localhost:5001`

### 鉴权
每个请求都要带 API key（与现有接口一致），三选一：
- `X-API-Key: <api_key>`（推荐）
- `Authorization: Bearer <api_key>`
- `?key=<api_key>`（query，会被日志脱敏）

鉴权失败返回 **401**。后端据此解析出 `user_id`，所有数据按用户隔离。

### Content-Type
请求体一律 `application/json`，响应一律 JSON。

### 错误约定
| 码 | 含义 |
|---|---|
| 200 | 成功（`/report` 即使部分字段被拒也返回 200，看逐字段状态） |
| 400 | 参数错误（如缺 `content_envelope`、未知 `kind`） |
| 401 | 鉴权失败 |
| 403 | 该能力未授权（权限开关关闭） |
| 404 | 资源不存在 / 已过期 |

错误响应形如 `{"error": "unauthorized", "capability": "photos"}`。

### 重要语义
- **权限默认全部关闭**。前端必须先调 `POST /permissions` 打开用户授权的能力，
  否则相关上报会被丢弃。
- **上报快照**：`/report` 提交一个 `context_snapshot` 数组，每项 `{key,data,message}`
  等价于一条信号；可只发变化的 key，也可发全量（未读到的用 `data:"null"`）。
- **`data` 是字符串化 JSON**：`context_snapshot` 每项的 `data` 是字符串（JSON 或
  `"null"`）；JSON 内部标量也建议用字符串。`/photo/evaluate` 的 `metadata` 同样用字符串。
  结构化数据——`/config`、`/items` 的 `doc`——保持自然类型（数字字段也接受字符串，后端容错）。

---

## 1. 能力清单（权限键）

`key` 是权限键（用于 `/permissions`）。下表对齐 iOS 实际上报的 key（见 §3）。

### 无需权限（默认开启，常发）
| 权限键 | 说明 | 上报 key | 唤醒源 |
|---|---|---|---|
| `time` | 本地时间 | `time` | 否 |
| `device` | 电量 | `battery` | 否 |
| `broadcast` | 屏幕采集状态 | `broadcast` | 否 |

### 需权限（用户授权后才采集）
| 权限键 | 说明 | 上报 key | 唤醒源(debounce) |
|---|---|---|---|
| `location` | 位置（含 WiFi/地区，粗粒度标签） | `location_signal` | 是（60s） |
| `motion` | 运动状态 | `motion_state` | 是（30s） |
| `calendar` | 下一场日程 | `calendar_next_event` | 否 |
| `now_playing` | 正在播放 | `playback` | 否 |
| `app` | 你在用哪个 app（iOS 快捷指令上报） | GET `/app_open`（见 §9） | 否 |
| `photos` | 你拍的照片 | 专用接口（见 §5） | 是 |

> `app` 默认开启（`default_on`，用户可在透明面板关闭）；数据只在用户配置了快捷指令后才流入。
| `health_sleep` / `health_workout` / `health_vitals` | 睡眠 / 运动 / 身体趋势 | `POST /items`（见 §8） | 否 |

> iOS 拿不到的信号（前台 App、静音、专注模式、精确解锁）统一走 `unsupported` key 占位
> 上报、后端**静默忽略**（返回 `ignored`）。`weather` 当前 iOS 恒 null，未接。

---

## 2. 上报 context_snapshot

### `POST /v1/perception/report`

一次批量上报当前感知快照。body 是一个 `context_snapshot` 数组，**每项 = 一条信号**
（等价于一个 key→value）。**照片不走这里**（见 §5）。

每项三字段：
- `key`：信号名（见 §3 及文首注的全量 key；也接受别名 `location`/`motion`/`now_playing`/`calendar`）
- `data`：**字符串**——JSON（如 `"{\"state\":\"walking\"}"`）或 `"null"`（无数据/未授权）
- `message`：人类可读说明（会存下来；`data` 为 `null` 时尤其有用，给 agent 解释原因）

**请求体**
```json
{
  "context_snapshot": [
    { "key": "motion_state",
      "data": "{\"confidence\":\"high\",\"started_at\":\"2026-06-08T15:15:00Z\",\"state\":\"walking\"}",
      "message": "当前粗粒度运动状态" },
    { "key": "location_signal", "data": "null", "message": "未授权定位，无法读取位置" }
  ],
  "client_ts": "1733650000"
}
```
- `client_ts`（可选，顶层）：整份快照的采集时刻（Unix 秒），用于时间戳守卫/新鲜度；
  不传则用服务端接收时间。
- 每次可只发变化的 key，也可发全量；未读到/未授权的能力用 `data:"null"` 表达。

**响应** `200`
```json
{ "results": { "motion_state": "accepted", "location_signal": "dropped_unauthorized" } }
```
逐项状态（`key` → 状态）：
- `accepted` — 已采纳（含 `data:"null"` 记为"当前无值"）
- `dropped_unauthorized` — 该 key 所属能力未授权，被丢弃
- `unknown_signal` — 不认识的 key
- `stale_ignored` — 本次 `client_ts` 比已存的值更旧，被忽略（不覆盖更新的值）

> `data`（JSON 解析后）等价于旧 signal 的 value；后端照常做**权限门 + 标签解析 +
> 时间戳守卫 + TTL**。原始值（坐标、`ssid`、`bundle_id`）解析成标签后**不落库**。
> `data:"null"` → 该字段记为"当前无值"（snapshot 返回 `null`），并保留 `message`。

### 当前有效状态是怎么确定的
后端给每个字段存 `{值, 时间戳}`。"现在有效的值"由两条规则决定：
1. **按时间取最新**：写入有**时间戳守卫**——只有 `client_ts` ≥ 已存时间戳才覆盖。
   所以乱序/迟到上报不会用旧值盖掉新值（迟到的旧值返回 `stale_ignored`）。
2. **新鲜度 TTL**：每个字段有有效期，超期即视为"无值"。TTL（秒）：
   `place_label`/`wifi_label`/`country` 900、`motion_state` 300、`time`/`broadcast` 300、
   `battery_level`/`charging`/`now_playing` 600、`calendar_next_event` 3600；
   `user_state` 永不过期。

读取当前有效状态用 `GET /v1/perception/snapshot`（未授权或过期的字段返回 `null`）——
这是 agent 侧接口，前端如需做"她现在能感觉到什么"的展示或调试也可调用，详见文末附录。

---

## 3. 各 key 及其 data 内容（早期核心信号，全量见文首注）

`data` 是**字符串化的 JSON**（或字面量 `"null"`），后端二次 `json.loads`。字段名
snake_case。下表是每个 key 的 data 结构与后端处理。

| key | `data` 的 JSON 内容（关键字段） | 后端处理 / snapshot 产出 |
|---|---|---|
| `time` | `{local_time, timezone, locale}` | 原样存 → `local_time`/`timezone`/`locale` |
| `battery` | `{level, charging}` | → `battery_level`/`charging` |
| `broadcast` | `{state, active}` | → `broadcast_state`/`broadcast_active` |
| `location_signal` | `{place_label, wifi_label, wifi_bssid, signal:{latitude,longitude,…}, country_region_change:{locale_region}, placemark:{…}}` | **只保留标签**：`signal.lat/lon` 经 geofence → `place_label`；`wifi_label` 直接采用；`country` 取 `locale_region`/ISO 码。**精确坐标、BSSID、placemark 地址全部丢弃** |
| `motion_state` | `{state, confidence, started_at}` | 原样存 → `motion_state`（对象） |
| `calendar_next_event` | `{title, next_event_time, event_kind, attendee_count, minutes_until_start, …}` | 原样存 → `calendar_next_event` |
| `playback` | `{playback_state, title, artist, album_title, …}` | 原样存 → `now_playing` |
| `unsupported` | `{frontmost_app, silent_mode, focus, precise_unlock}`（恒 null） | **静默忽略**（返回 `ignored`） |

- 别名：`location`→`location_signal`、`motion`→`motion_state`、`now_playing`→`playback`、
  `calendar`→`calendar_next_event`（前端用任一都行）。
- `place_label` 取值：`home/work/gym/transit/outdoor/unknown`（基于用户在 §7 `/config`
  标注的 geofence 解析）。
- `data:"null"`：该 key 当前无数据（未授权/读不到），其字段在 snapshot 里为 `null`，
  `message` 一并保留供 agent 参考。
- ⚠️ 隐私：`location_signal` 即便上报了精确坐标/BSSID/地址，后端**只取粗粒度标签、丢弃
  全部精确字段**，agent 永远只看到 `place_label`/`wifi_label`/`country`。

---

## 4. 权限开关（透明性 UI）

### `GET /v1/perception/permissions`
**响应**
```json
{
  "capabilities": [
    {"key":"location","label":"你大概在哪里（只看地点标签，不看具体地址）","tier":1,"enabled":true,"wake_source":true},
    {"key":"photos","label":"你拍的照片","tier":2,"enabled":false,"wake_source":true}
  ]
}
```
直接用来渲染需求文档里的"她现在能感觉到的"面板：`enabled=true` 展示在"开启"，
`false` 在"未开启"。

### `POST /v1/perception/permissions`
稀疏更新，只传要改的开关。
**请求**
```json
{ "location": true, "wifi": true, "photos": false }
```
**响应**：同 GET（返回全量最新状态）。

> 关闭某能力会**立即停止采集**该能力数据、停止其唤醒源、agent 侧也读不到。

---

## 5. 照片（单接口 + V2 无硬拦截）

照片像素是最敏感数据。一个接口完成评估 + 上传：metadata 和加密图片一起传。

- **V2 删除敏感场景硬拦截**：`scene_hint` 仍作为 metadata 保存，但不再决定图片能不能
  被伴侣看到。敏感内容的分寸由 agent 的表达层处理，不由平台 gate 感知。
- 后端**永不看明文像素**（只存密文信封，agent 要看时才经 enclave 解密）。

**iOS 端**负责：过滤截图（端上直接丢）→ 30s 连拍聚簇只取代表帧 → Vision 生成 metadata
→ 用 `ContentEncryption.swift` 的 `build_envelope` 加密像素（`visibility:"shared"`）。若照片有
位置，iOS 端本地解析 `place_label` 后放入可选 `meta_envelope`；raw EXIF GPS 不再上行。

### `POST /v1/perception/photo/evaluate`
**请求**（metadata + 加密图片一起传）
```json
{
  "metadata": {
    "has_faces": "true",
    "face_count": "2",
    "scene_hint": "landscape",
    "scene_confidence": "0.91",
    "time_of_day": "afternoon",
    "is_burst": "false",
    "is_indoor": "false",
    "has_text_block": "false",
    "is_screenshot": "false"
  },
  "content_envelope": { "v":1, "id":"abc123", "body_ct":"<base64 密文>", "nonce":"...", "K_user":"...", "K_enclave":"...", "visibility":"shared", "owner_user_id":"<uid>" },
  "meta_envelope": { "v":1, "id":"meta123", "body_ct":"<base64 密文>", "nonce":"...", "K_user":"...", "K_enclave":"...", "visibility":"shared", "owner_user_id":"<uid>" }
}
```
- `content_envelope`：`build_envelope` 输出，`body_ct` 是**加密后的照片像素**。
  `photo_id` 取 `content_envelope.id`（32 位 hex；解密通道按此识别）。
- `metadata` 各值**统一用字符串**（布尔用 `"true"`/`"false"`），全部端上 Vision 生成：

  | 字段 | 字符串内容 | 说明 |
  |---|---|---|
  | `scene_hint` | 枚举 | 见下方枚举（**必须**取枚举值） |
  | `scene_confidence` | 数字字符串 0~1 | 分类置信度 |
  | `has_faces` | 布尔字符串 | 是否有人脸 |
  | `face_count` | 数字字符串 | 人数 |
  | `time_of_day` | 枚举 | morning/afternoon/evening/night |
  | `is_burst` | 布尔字符串 | 是否连拍 |
  | `is_indoor` | 布尔字符串 | 是否室内 |
  | `has_text_block` | 布尔字符串 | 是否含大块文字 |
  | `is_screenshot` | 布尔字符串 | 截图（兜底，正常端上已过滤） |

- `meta_envelope`（可选）：加密敏感上下文，例如端上 geofence 解析出的 `place_label`。
  后端只存密文，不读取 raw GPS。

**`scene_hint` 枚举（前后端必须对齐）**

| 分组 | 取值 | 处理 |
|---|---|---|
| 非敏感 | `landscape` `food` `people` `pet` `activity` `object` `art` `text_note` `other` | 入库 |
| 语境敏感 | `private` `receipt` | `usable=true`，入库，交 agent 表达层自律 |
| 客观敏感 | `document` `id_card` `medical` `screenshot` | `usable=true`，入库，`sensitive=true`，不做平台硬拦截 |

> `private` = 看起来私密的场景（室内洗漱、床上等）。不在枚举内的字符串按 `other` 处理。

**响应** `200`
- 入库：`{ "photo_id":"abc123", "metadata":{...}, "usable":true, "sensitive":false, "status":"stored" }`
- 敏感场景仍入库：`{ "photo_id":"abc123", "metadata":{...}, "usable":true, "sensitive":true, "status":"stored" }`
- `400` 缺 `content_envelope`；`403` 未授权 `photos`。

> 照片**只走这一个专用接口**，**不经过通用 `/report`**。

---

## 6. user_state（含 iOS Focus 自动同步）

`user_state` 取值：`default` / `focused` / `away`。

### 手动设置 `POST /v1/perception/user_state`
```json
{ "user_state": "focused" }
```
**响应** `{"user_state": "focused"}`（返回生效值）。

> iOS 目前**拿不到专注模式**（在 `unsupported` 里恒 null），所以 Focus 自动同步暂不可用，
> `user_state` 仅靠上面的手动设置。后端保留了 Focus→user_state 的映射逻辑，等 iOS 能读到
> Focus 时（上报 `ios_focus` key）即可自动覆盖/恢复，无需改后端。

---

## 7. 配置（用户标注 / 映射）

### `GET /v1/perception/config` → 返回当前配置
### `POST /v1/perception/config`（稀疏合并）
```json
{
  "geofences": [
    {"label":"home","lat":37.42,"lon":-122.08,"radius_m":150},
    {"label":"work","lat":37.40,"lon":-122.10,"radius_m":200}
  ]
}
```
- `geofences`：用户标注的 home/work/gym 等地理围栏，用于把 `location_signal` 里的精确
  坐标解析成 `place_label`。**这是当前唯一在用的配置。**

> 后端还保留了 `focus_map`（Focus→user_state 映射）等字段的逻辑，供 iOS 将来能读到对应
> 信号时启用；当前 iOS 不上报这些信号，可忽略。`wifi_label` 由端上直接给（后端不映射
> SSID）。

---

## 8. Tier 2 集合上报（健康）

> 日历**不走这里**——下一场日程通过 `/report` 的 `calendar_next_event` key 上报（见 §3）。
> 本接口只用于健康聚合（睡眠 / 运动 / 身体趋势）。

### `POST /v1/perception/items`
```json
{
  "kind": "sleep",
  "items": [
    { "item_id":"s_1", "ts":1733660000, "doc": {"duration_min":420,"quality":"fair"} }
  ]
}
```
- `kind`：`sleep` / `workout` / `vitals`，分别由 `health_sleep` / `health_workout` /
  `health_vitals` 权限 gate。（`calendar` 不是有效 kind，会返回 `unknown_kind`。）
- `doc`：自由元数据。
- `item_id` / `ts` / `expires_at` 可选（缺省自动生成 / 用当前时间）。

**响应** `{"written": 1}`；未授权 `403`，未知 kind `400`。

> ⚠️ 时间型唤醒（会议前 15 分钟、刚醒、锻炼完）目前**由客户端按时机调用**触发：
> 在合适时刻上报对应 item 即可。后端暂未做定时调度器。

---

## 9. App 使用（iOS 快捷指令 GET 接口）

iOS 拿不到前台 app，所以用**快捷指令自动化**：「当打开 App X 时 → 获取 URL 内容」，
URL 指向下面的 GET 接口。**所有参数（含 api key）都放在 URL query 里**（快捷指令做 GET
最方便）。

### `GET /v1/perception/app_open`
```
GET /v1/perception/app_open?key=<apikey>&app=Instagram&category=social&ts=1733650000
```
| query 参数 | 必填 | 说明 |
|---|---|---|
| `key` | 是 | api key（也支持 `X-API-Key`/Bearer，但快捷指令用 `?key=` 最简单） |
| `app` | 是 | app 名称（也接受 `bundle_id`） |
| `category` | 否 | 分类（如 social/productivity，快捷指令里可手填） |
| `ts` | 否 | 采集时刻（Unix 秒）；不传用服务端接收时间 |

**响应** `200 {"status":"ok","app":"Instagram","category":"social","ts":1733650000}`
- `400 app_required`（缺 app）；`403`（`app` 能力被用户关闭）。
- 效果：更新 snapshot 的 `app_name`/`app_category`（当前 app），并往**使用时间线**追加一条
  （供"什么时间用了啥 app"统计；agent 读 `GET /app_usage` 或 MCP `feedling_perception_app_usage`）。

**快捷指令配置**：自动化 → 「打开 App」选目标 app → 添加「获取 URL 内容」(方法 GET) →
URL 填上面的串、把 `app=` 改成对应 app 名（建议关「运行前询问」让它静默执行）。每个要追踪
的 app 各建一条自动化。

---

## 10. 前端典型接入顺序

1. **首次授权**：用户在感知权限面板勾选 → `POST /permissions` 打开对应能力。
2. **标注**：用户 mark home/work → `POST /config` 写 geofences。
3. **持续上报**（后台）：状态变化时 `POST /report`，body 为 `context_snapshot` 数组，
   每项 `{key, data, message}`，`data` 是字符串化 JSON：
   ```json
   { "context_snapshot": [
       { "key": "time",            "data": "{\"local_time\":\"...\",\"timezone\":\"...\",\"locale\":\"en\"}", "message": "本地时间" },
       { "key": "battery",         "data": "{\"level\":\"0.8\",\"charging\":\"false\"}", "message": "电量" },
       { "key": "location_signal", "envelope": {"id":"...","body_ct":"..."}, "changed": true },
       { "key": "motion_state",    "envelope": {"id":"...","body_ct":"..."}, "changed": false }
   ] }
   ```
   敏感信号有值时只上传 `envelope + changed`；未授权/读不到时用 `data:null` 表达。
4. **照片**（拍照入库时）：端上过滤截图 + 30s 聚簇取代表帧 + Vision 生成 metadata +
   `build_envelope` 加密 → `POST /photo/evaluate`（metadata + `content_envelope`，可选
   `meta_envelope` 一起传）。V2 不再硬挡敏感照；敏感 metadata 只影响表达分寸。
5. **透明性面板**：`GET /permissions` 渲染开关；用户随时可关。
6. **Tier 2**：健康按 §8。
7. **App 追踪**（可选）：引导用户为想追踪的 app 配置快捷指令自动化（见 §9）。

---

## 11. 前端接口速查

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/perception/report` | 上报 context_snapshot（每项一条信号） |
| GET | `/v1/perception/permissions` | 读权限开关（渲染透明面板） |
| POST | `/v1/perception/permissions` | 改权限开关 |
| POST | `/v1/perception/photo/evaluate` | 照片单接口（metadata + 加密图片，一步入库） |
| GET | `/v1/perception/app_open` | App 打开上报（iOS 快捷指令，参数全在 URL） |
| GET | `/v1/perception/config` | 读配置（回显） |
| POST | `/v1/perception/config` | 改配置（标注 geofence） |
| POST | `/v1/perception/user_state` | 手动设 user_state |
| POST | `/v1/perception/items` | Tier2 集合上报（健康） |

---

## 附录：非前端接口（agent / MCP 读取，前端无需调用）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/perception/snapshot` | 当前有效状态（授权+新鲜的字段） |
| GET | `/v1/perception/photos` | agent 读照片元数据列表 |
| GET | `/v1/perception/photo/<id>/content` | agent 取照片（经 enclave 解密像素） |
| GET | `/v1/perception/items/<kind>` | agent 读集合（健康） |
| GET | `/v1/perception/app_usage` | agent 读 app 使用时间线 |

**`GET /snapshot` 响应示例**（未授权或过期字段为 `null`；`user_state` 总有值）：
```json
{
  "place_label": "home",
  "wifi_label": "home_wifi",
  "app_category": null,
  "app_bundle": null,
  "motion_state": "still",
  "battery_level": "0.82",
  "charging": "false",
  "silent_mode": "true",
  "last_unlock_ago_sec": "5",
  "bt_devices": null,
  "now_playing": null,
  "country": null,
  "user_state": "focused"
}
```
