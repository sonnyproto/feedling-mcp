# 屏幕看护 ⟂ 心跳 解耦 · 实施计划(2026-06-27)

Seven 已拍板。把"屏幕看护"从重心跳里拆出来,成独立轻量 lane。

## 事实基线(已核)
- iOS 抽帧:默认 **30s/帧**(`feedling-mcp-ios` `SharedConfig.captureIntervalMsDefault=30_000`,可配)。
- 帧保留:`MAX_FRAMES=200` 环、**无时间过期** → 30s/帧 ≈ **100min** 历史可回看。
- 心跳现状:broadcast on=300s(5min)/ off=1800s(30min);每次 wake 都塞重载荷(跨域桌面+全工具目录+聊天上下文)。
- `PROACTIVE_WAKE_MAX_FRAMES=5` = 自动附进 wake 的采样上限(非 agent 能看上限)。
- 无"当天发言硬上限"(`count_24h` 仅自知)。
- OpenClaw 注册了**全部**工具 → resident agent 永远拥有/知道全部工具,prompt 列表只是引导。
- `/caption`(OpenRouter)只接在退役 hosted 路;resident 走直接像素。

## 目标设计
| 维度 | 心跳(presence) | 屏幕看护(screen-watch, 新 lane) |
|---|---|---|
| 频率 | 恒 **30min**(不再被 broadcast 拉快) | **2min** 巡查 |
| 唤醒条件 | 既有 gate | **屏幕变了 + 非活跃聊天**(变了才醒,聊天中让位) |
| 载荷 | 重:跨域桌面+全工具目录+聊天上下文 | 轻:在场说明 + 最近~5帧(OCR先图后)+ attention_facts + **全部工具名(无 cost-guide)** + 跨gap积压回看提示 |
| 看图 | — | OCR 先筛 →(值得)→ screen_read 深看;**无千问** |
| 性格 | 不动 | 不动(只陈述事实) |

## 分工

### Codex(后端 / infra)
1. **job_kind/trigger=`screen_watch` 透传**:tick→gate→job 把这个标记带到 consumer 领到的 job 上(独立追踪/dedup,不套重心跳的 frame 采样/gate 逻辑;不是 manual/forced,仍尊重 Ambient gate)。
2. **心跳解耦确认**:心跳 gate/cadence 不再因 broadcast 改变(配合 consumer 把 on-interval 设回 30min)。
3. **砍 caption VLM-key wiring**:回滚 `f2f34ee` 那批 prod/test compose+ci 的 `FEEDLING_SCREEN_VLM_API_KEY` 注入(或至少不合 prod);caption/OpenRouter 对保留架构非必需。
4. 不碰 hosted 以外的隐私姿态;改完邮件回 CC 审。

### CC(consumer / iOS / prompt / 文档 / 审计)
1. **心跳恒 30min**:`_proactive_tick_interval_for_broadcast_state` 不再对 on 返回 300;心跳与屏幕脱钩。
2. **screen-watch 循环**:新 2min 循环(仅 broadcast on 时活跃);轮询最近帧比对 frame_id/ts 做变化检测;`last_user_message_age` 近 → 让位(不戳);变了且非聊天 → post `screen_watch` tick(普通 self-wake,尊重 Ambient)。
3. **轻 prompt**:`_message_for_proactive_job` 按 `job_kind==screen_watch` 走轻分支(见上载荷);跨 gap 附"积压 N 帧,screen_recent 可回看"。
4. **iOS**:确认 30s 抽帧够(2min≈4帧);必要才调。
5. 单测(变化检测/让位/轻 prompt)+ VPS resident e2e + 体检表/CHANGELOG。

## 验收
1. 没共享:只有 30min 心跳,无 screen-watch。
2. 共享 + 屏幕静止:不戳 agent(变化检测)。
3. 共享 + 屏幕变 + 没在聊:2min 内 screen-watch 轻唤醒,prompt 含帧+全工具名,无跨域桌面。
4. 正在聊天:screen-watch 让位(不另发)。
5. 跨 10min gap:prompt 提示积压,agent 能 screen_recent/screen_read 回看(OCR 先筛)。
6. caption/OpenRouter wiring 已撤;screen 仍可被 agent 直接看(decrypt 像素)。
7. 心跳不再被 broadcast 拉到 5min。
