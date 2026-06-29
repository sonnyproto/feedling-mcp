# 方案:用户自定义心跳频率（wake_interval）— 2026-06-29

作者:CC × Seven。**状态:已拍板待实现**(本版不做,留下一个版本)。
本文是实现级 spec,工程师可直接照做。

## Context（为什么）
「陪伴」模式下,系统定期叫醒 agent 做"在场检查"(presence/heartbeat),让它自己决定要不要
主动找用户。当前心跳间隔是**写死的全局 30min**(`PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC`,
env 级、非 per-user、用户改不了)。

用户诉求(Seven):
- 有人想**省 token / 减少被主动打扰** → 让 wake 更稀。
- 有人想要**更勤的陪伴** → 让 wake 更密。
- 给用户**可掌控的自定义空间**。

目标:让用户在 App 里自选心跳频率,**范围 10min–12h,默认 30min**。

## 边界（必须在 UI 文案里讲清,避免误解）
这个频率**只控制「陪伴」模式下"闲时定期在场检查"的节奏**。它**不影响事件驱动唤醒**
(到达地点 `arrived_at_anchor`、解锁 `unlock_after_absence`、拍照 `photo_added`、
共享屏幕时的 `screen_watch` 2min lane)——真有事发生 agent 照样会醒。
即:滑杆调的是"没事时多久主动找你一次",不是"全部唤醒频率"。
且**仅当「陪伴」开关(ambient)为开时有意义**(ambient 关 = 根本没有 heartbeat)。

## 设计决定（已拍板）
- 字段名:**`wake_interval_sec`**(整数秒),挂在 proactive state,**per-user**。
- 默认:**1800**(30min)。
- Clamp:**min 600(10min)/ max 43200(12h)**。后端写入时强制 clamp,非数字拒绝/落默认。
- iOS 用**离散档**(不是连续滑杆,避免 37min 这种怪值),**8 档**:
  **10min / 15min / 30min / 1h / 2h / 4h / 6h / 12h**,默认选中 30min。

## 实现:三层

### ① 后端（Codex）—— `feedling-mcp-v1`
- `backend/proactive/controls_v2.py`:
  - `ProactiveSettingsV2` dataclass 加 `wake_interval_sec: int = 1800`。
  - 加进 `switches()`/序列化(注意:它是数值,不是开关 bool——按"设置项"对待,和
    `ambient/scheduled/reminders_delivery` 同一份 state,但单独的数值字段)。
  - 解析入参:读到后 `clamp(600, 43200)`;缺省或非法 → 1800。
- `backend/core/store.py`:proactive state patch 接受 `wake_interval_sec`(子集接受,
  与现有三开关同样路径,L525 那批 key 旁边加)。
- `GET /v1/proactive/state` 返回 `wake_interval_sec`;`POST /v1/proactive/state` 子集接受它。
- 测试:clamp 边界(599→600、43201→43200、非数字→1800)、state round-trip、
  默认值(老用户无此字段 → 1800)。

### ② consumer（CC）—— `tools/chat_resident_consumer.py`
- 现状:`_proactive_tick_interval_for_broadcast_state()`(L2837)返回
  `max(60, PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC)`(全局 env)。
- 改:让它读 **per-user `wake_interval_sec`**(从 consumer 已经在拉的 proactive state 里取;
  若 state 里没有则回退到 env 默认 1800)。
- consumer 侧也 clamp 一道(600–43200)防御后端异常值。
- 下一个 tick 调度(L5489 `next_interval = ...`)自然用到新值,即时生效;无需重启。
- 测试:state 给 600 → 间隔 600;给 0/None/超界 → 回退/clamp;ambient off 时不受影响。

### ③ iOS（CC）—— `feedling-mcp-ios`
- `App/FeedlingTest/Pages/Settings/SettingsView.swift` 的 `proactiveCard`:
  在「陪伴」(ambient)那行下方加一个**频率选择器**(8 档 segmented/picker 或 menu)。
  - 仅当 `api.proactiveAmbient == true` 时可用,否则置灰(无意义)。
  - 当前值从 proactive state 的 `wake_interval_sec` 映射到最近的档位。
  - 改动 → `FeedlingAPI.updateProactiveSwitch` 同款路径新增
    `wakeIntervalSec: Int?` 参数 → POST `/v1/proactive/state {wake_interval_sec}`。
- `FeedlingAPI.swift`:`ProactiveStateResponse` 解码加 `wake_interval_sec`;
  `updateProactiveSwitch(...)` 加 `wakeIntervalSec` 可选参数(只在提供时进 payload)。
- 文案(`Localizable.xcstrings`,双语,走 `isChinese` 规范):
  - 标题如 `settings.proactive.wake_interval.name` = "陪伴频率" / "Companionship frequency"
  - 二级说明:讲清"多久主动找你一次;只在陪伴开启时生效;事件(到地点/拍照/共享屏幕)
    不受此影响" / EN 对应。
  - 各档位标签:10 分钟 / 15 分钟 / 30 分钟 / 1 小时 / 2 / 4 / 6 / 12 小时。

## 风险 & 为什么不赶这版
- 三层联动,且碰**核心**:`controls_v2`(gate 所有主动行为)+ consumer **主循环 tick 调度**——
  interval 读成 0/None 会让心跳狂发或不发,高爆炸半径。
- 需要后端部署 = **CVM 重部署**;test CVM 当前不稳定(每 ~30-40min 崩),赶发版风险高。
- 做"完整且稳"需 Codex(后端)+ CC(consumer+iOS)+ 测试 + 真机 e2e,超过单次发版窗口。

## 分工
- 后端字段 + clamp + state + 测试 → **Codex**。
- consumer 读取改造 + 测试 → **CC**。
- iOS 选择器 + API + 文案 → **CC**。

## 验收（真机 e2e）
1. App 设 10min → consumer 下一个 heartbeat tick 约 10min 后触发(`next=600s`)。
2. 设 12h → `next=43200s`。
3. 关掉「陪伴」→ 不再有 heartbeat tick(选择器置灰);期间拍照/到地点仍能唤醒(事件 lane 不受影响)。
4. 老用户(无 `wake_interval_sec`)→ 默认 30min,行为不变。
