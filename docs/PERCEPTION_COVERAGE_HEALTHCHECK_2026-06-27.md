# 全感知覆盖 · 体检表 + 照片/屏幕路径拆解(2026-06-27)

> 本轮目标:(1)给一张"全感知覆盖体检表";(2)把**照片**与**屏幕共享**两条路径
> 的处理方法 / 逻辑分布 / 使用场景 / 具体调用全部摊开;(3)**重点核查这套 CLI
> 是否落位到「各种感知 / 心跳 / Agent 工具表」三处**。
>
> 部署:`test` → `https://test-api.feedling.app`,CVM `:d4f6976`(app 信号已上)。
> 图例:✅ 通过 · ⚠️ 按设计部分覆盖(非 bug)· ❌ 缺口 · 🚫 阻塞

---

## 一、体检表(coverage matrix)

### A. 感知信号(perception signals,18 个)

三方信号集 **完全一致**(`tools/io_cli.py` ≡ 后端 `AGENT_PERCEPTION_SIGNALS` ≡
OpenClaw 插件 `SIGNALS`,各 18 个,集合差 = ∅)。

| 组 | 信号 | 默认档 | Agent 可拉 | 状态 |
|---|---|---|---|---|
| Fast | now / location / weather / motion / calendar | fast | ✅ | ✅ |
| Slow(健康) | steps / sleep / workout / vitals / activity / body / metabolic / cycle / mood | slow | ✅ | ✅ |
| Slow(其他) | reminders | slow | ✅ | ✅ |
| Pull-only | focus / audio_route / **app** | pull | ✅ | ✅(app 本轮修复) |

> `app` 之前 400 `unknown_signals` 是 CVM 滚 `d4f6976` 时旧容器在服务;滚稳后第 1 次
> 轮询即注册成功,返回 `{app_name, app_category}`(暂为 null,等快捷指令上报一次 app_open
> 即有值)。

### B. 专用读工具(perception 信号之外,内容类)

| 能力 | 工具 | 状态 | 备注 |
|---|---|---|---|
| 屏幕共享 | `screen-recent` / `screen-read` | ✅ | 真机正在共享时 broadcast_state=on、8 帧、`screen-read` decrypt_status=ok、960px |
| 照片 | `photo-recent` / `photo-read` | ✅ | `photo-read --id` 可读单张细节(metadata + 可选解密 JPEG) |
| 趋势/历史 | `perception-trend` / `perception-history` | ✅ | 量化层(perception_daily)|
| 记忆 | `memory-index` / `memory-fetch` | ✅ | 读侧 |

### C. CLI 套件三处落位审计(本轮重点)

CLI 套件 = io_cli 的 9 个 verb:`perception` / `perception-trend` / `perception-history`
/ `memory-index` / `memory-fetch` / `screen-recent` / `screen-read` / `photo-recent` /
`photo-read`。

| 落位 | 屏幕 | 照片 | 其余 | 结论 |
|---|---|---|---|---|
| **① 各种感知** | broadcast_state=context_field | photos=wake_source+query_tool | 18 信号对齐 | ✅ 内容类(screen/photo)走独立 pipeline,在 perception 信号之外做"专用读工具",**设计如此** |
| **② 心跳** | ✅ 全接(触发节奏 + 自动注入字幕/截图) | ⚠️ 仅触发(`trigger:photo_added`),内容靠 Agent 自己拉 | digest 注入 | ✅ 屏幕全接;照片"拉取式",符合既定设计 |
| **③ Agent 工具表** | screen_recent/read ✅ | photo_recent/read ✅ | 全部 ✅ | ✅✅ 三份齐(OpenClaw 插件 + io_cli + agent_tools_prompt.md)|

**一句话**:三处都落位。唯一"非缺口的设计差异"是——心跳里**屏幕内容会自动注入** wake
prompt,**照片内容不自动注入**(只给 `trigger: photo_added`,Agent 自行 `photo_recent`
→ `photo_read` 去看)。这符合 Seven 既定的"照片应由 Agent 主动拉取查看"的设计。

> 可选优化(非 bug):照片 wake 时把 `photo_id` 一并塞进 wake metadata(类比屏幕的
> `frame_ids`),Agent 就能直接 `photo_read --id` 少一跳。当前 Agent 需先 `photo_recent`
> 拿最新 id,可正常 work。

---

## 二、照片路径完整拆解

### 处理方法(一句话)
单步加密摄入 + 元数据评估;**照片是 wake 触发源**,但**内容拉取式**(Agent 用工具看)。

### 逻辑分布(代码落点)
- **摄入/评估**:`POST /v1/perception/photo/evaluate` → `backend/perception/routes.py:103`
  → `service.photo_evaluate()`(`backend/perception/service.py:804`)。单步:评估 metadata,
  可用则存加密内容(client-encrypted envelope)。
- **wake 触发**:`service.py` 内 `_maybe_wake(user_id, "photos", PHOTO_CLUSTER_SEC, "photo_id", …)`
  + `differ_v2.py:165` `trigger="photo_added"`,`origin_refs=["photo:<id>"]`。
  簇内去抖:`PHOTO_CLUSTER_SEC`(避免连拍刷屏)。
- **能力声明**:`catalog.py:71` `Capability("photos", wake_source=True, query_tool=True)`。
- **读取端点**:`/v1/perception/photos`(列表)+ `/v1/perception/photo/<id>/content`
  (返回 frame_id + decrypt_path;解密走 enclave `/v1/screen/frames/<frame_id>/decrypt`)。

### 使用场景
1. **被动**:用户拍照 → evaluate → 触发 wake(`photo_added`)→ 心跳 prompt 显示
   `trigger: photo_added` → Agent 决定是否看 → `photo_recent` / `photo_read`。
2. **主动**:用户问"看看我刚拍的照片" → Agent 调 `photo_recent` 拿最近 → `photo_read --id`
   读单张(必要时 `--include-image` 解密 JPEG 用视觉看)。

### 具体调用(怎么调)
```
python <io_cli> photo-recent [--limit n]          # 最近照片 metadata(场景/时间,无像素)
python <io_cli> photo-read --id <photo_id>        # 单张细节(metadata)
python <io_cli> photo-read --id <photo_id> --include-image   # 解密 JPEG(大,视觉用)
```
- OpenClaw 工具名:`photo_recent` / `photo_read`(provider-safe 名)。
- 路径:io_cli → `GET /v1/perception/photos` / `…/photo/<id>/content` →(可选)enclave decrypt。

---

## 三、屏幕共享路径完整拆解

### 处理方法(一句话)
WS 上传加密帧 → frames_meta;**屏幕既驱动心跳节奏/触发,内容又自动注入** wake/前台
prompt;Agent 也可用工具补拉。

### 逻辑分布(代码落点)
- **帧上传**:iOS broadcast extension → WS 上传 encrypted v1 envelope → `frames_meta`。
- **状态**:`broadcast_state ∈ {unknown,on,off,paused}`;catalog `broadcast`=context_field
  (`catalog.py:57`)。
- **心跳节奏/触发**(`tools/chat_resident_consumer.py`):
  - `_proactive_tick_interval_for_broadcast_state()`:on → 30s 档,off → 60s 档。
  - `_proactive_tick_trigger_for_broadcast_state()`:on/off/paused → 不同 trigger 标签。
  - `_proactive_wake_kind()`:有 screen_text → `screen`,否则 `presence`。
- **内容自动注入**(两处):
  - 前台:`_screen_context_for_message()`(指示代词/"看我的屏幕"类 → 自动附最新帧字幕+截图;
    `_should_attach_screen_context()` 正则匹配)。
  - 心跳:wake job 带 `frame_ids` → `_screen_context_for_frame_ids()`(`:1026`)读取并把
    字幕+截图注入 wake prompt(`call_agent(images=…, image_paths=…)`)。
- **字幕生成**:enclave caption(`screen_caption` flag)。
- **解密**:`/v1/screen/frames/<frame_id>/decrypt?include_image=…`(消费端走内部
  `_fetch_screen_json`,**非 io_cli**;io_cli 是给 Agent 工具用的另一条同后端路径)。

### 使用场景
1. **共享中被动**:broadcast=on → 心跳切 30s 档 → wake 带 frame_ids → 字幕+截图自动进
   prompt → Agent 直接"看见"屏幕。
2. **前台指示问句**:用户问"这个怎么弄/看我的屏幕" → 自动附最新帧。
3. **主动补拉**:Agent 需要更多帧 → `screen-recent` 列帧 → `screen-read [--include-image]`。

### 具体调用(怎么调)
```
python <io_cli> screen-recent [--limit n]                 # 最近帧 metadata(无像素)
python <io_cli> screen-read [--frame-id id]               # 最新/指定帧的字幕/OCR
python <io_cli> screen-read --frame-id id --include-image # 连解密截图(视觉用)
```
- OpenClaw 工具名:`screen_recent` / `screen_read`。
- **关键区分**:**心跳注入屏幕** = consumer 内部 `_fetch_screen_json`(直连后端);
  **Agent 工具拉屏幕** = io_cli `screen-*`。两条代码路径,打同一组后端 endpoint。

---

## 四、真机验证快照(2026-06-27,Seven 正在共享时)

| 项 | 结果 |
|---|---|
| broadcast_state | `on`(未共享时 `paused`,属预期) |
| screen frames total | 8 |
| `screen-recent` | 返回帧(加密) |
| `screen-read` | `decrypt_status: ok`, app=`com.feedling.mcp`, 960px |
| `perception app` | `{"ok": true, signals.app.{app_name,app_category}}`(暂 null,待快捷指令上报) |
| 信号集对齐 | io_cli ≡ 后端 ≡ OpenClaw,各 18,差集 ∅ |

> 结论:之前"屏幕一直不太对"= 那一刻没在共享 → `paused`/0 帧,**管线本身没坏**。
