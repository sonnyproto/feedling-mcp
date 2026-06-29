# 主动陪伴系统 · 功能定义 + 详尽测试计划(2026-06-25)

> 起因:真机测试发现 ① 三开关全关后用户设的定时提醒仍触发;② 主动回复/心跳从未触发过。
> 排查确认这是**两个真 bug**(见 §3),且整套"自发主动"链路对 resident 用户**从未真正工作过**。
> 本文 = 先把**每个功能的确切定义**写清楚(§2),再列**详尽测试计划**(§4),测试以"代码当前态 vs spec 目标态"双口径判定。
>
> 证据来源:`PROACTIVE_PERCEPTION_SPEC_V2.md`(目标态)+ `backend/proactive/*`、`backend/hosted/wake_consumer.py`、
> `tools/chat_resident_consumer.py`(当前态)。**spec 是目标,代码是现状,两者有差异处本文逐条标注。**

---

## 1. 两条运行路径(先分清,否则全乱)

| 路径 | 谁跑 agent | 心跳/wake 谁驱动 | 主动消息怎么回流 | 我们现在测的是哪条 |
|---|---|---|---|---|
| **hosted** | 后端 model_api runtime | 后端 `_hosted_tick_loop`(只遍历 hosted 用户) | 后端直接写 chat | ❌ |
| **resident** | VPS 上的 OpenClaw consumer | **VPS consumer 轮询** `POST /v1/proactive/tick` | tick 产 job → consumer 领活 → 跑 agent → `POST /v1/chat/response` | ✅ **就是这条** |

> 关键:很多 gate / fire 逻辑**只在 hosted 路实现**,resident 路缺失或走旁路——这正是两个 bug 的来源。

---

## 2. 功能定义(每个东西到底是什么)

### 2.1 心跳 heartbeat
- **是什么**:6 种唤醒源之一,平台时钟驱动的**低频保底** wake——平静期偶尔给 agent 一次"要不要做点啥"的机会。
- **频率**:目标 ~30min(spec 标【待定参】);`wake_interval` 字段存在但**"可配/生效"尚未做完**(任务 #6 pending)→ 测试时频率视为未验证。
- **绝大多数心跳 → `sleep`**(不出声),这是**正常、期望**行为。
- **gate**:受**陪伴(Ambient)**开关控制。

### 2.2 wake → turn → 主动消息 链路
1. **wake 源**(6 种):`user_message`(含 manual flag)/ `heartbeat` / `perception_event` / `scene_change` / `scheduled_wake` / `background_result`。
2. **changed/wake 判定**(只对感知源):原始信号过 Perception Differ,只有**跨离散边界**才产 event;**连续量(步数/位移/电量/天气/体征/睡眠/focus…)永不 wake,只 pull**。
3. **Wake 控制 gate**(`controls_v2.py:199-215` `evaluate_wake_control_v2`):manual / user_message 永过;自发源遇 `ambient=false` 拒;`scheduled_wake` 遇 `scheduled=false` 拒 + `transparency_required`。
4. **inbox 合并 → 单飞 turn**(per-user 同时最多一个 turn;hosted 当前 `merge_window=0` 只单飞不窗口合并)。
5. **turn 产出**:`messages[]` + `actions[]`(sleep / schedule_wake / cancel_wake)+ `needs_background`。
6. **Delivery gate**(`controls_v2.py:218-232` + `wake_consumer.py:504-549`):消息**总是先写进 chat log**;只有 `allow_visible_delivery=true` 才真推 push。
- **"判睡"是 agent 自己的判断,不是平台 gate**(spec 明令禁止平台/第二模型替 agent 判内容值不值)。

### 2.3 三个用户开关(精确语义 + 字段映射)

| 开关 | gate 哪一层 | 关掉=拦住什么 | **不连坐**(关了也必须照常的) | 后端字段(V2 / legacy) |
|---|---|---|---|---|
| **陪伴 Ambient** | Wake | 全部**自发**源:heartbeat + perception_event + scene_change | ✅ 定时任务、提醒、前台聊天、manual 召唤 | `ambient` / `enabled` |
| **定时任务 Scheduled** | Wake | `scheduled_wake`(agent 自埋的定时/闹钟)+ 设定时器的能力;关时必须**透明告知**不静默丢 | ✅ 陪伴、前台、manual | `scheduled` |
| **提醒 Reminders** | Delivery | 只拦最后那下 **buzz(push/Live Activity)**;仍照常醒、思考、**写 chat** | ✅ Wake/Voice 全照常,下次开 app 能看到 | `reminders_delivery` / `!dnd` |

- **总开关 = 陪伴(`enabled`/`ambient`)**。`ai_state`(present/watching…)**不是开关**,只是 chat 顶部展示态,V2 不拿它做门。
- **iOS 写入正确**:三开关都 `POST /v1/proactive/state`,字段 `ambient`/`scheduled`/`reminders_delivery`(`SettingsView.swift:370-393` + `FeedlingAPI.swift:2362-2395`),无 iOS bug。

### 2.4 scheduled wake vs 心跳 wake(两条独立路径)
- **scheduled_wake** = agent 在对话里形成意图、自埋的定时器(**不是**用户直接说"9点叫我"那种 UI 闹钟)→ 受 **定时任务** gate。
- **heartbeat / 感知 event** = 自发 → 受 **陪伴** gate。
- 关陪伴**绝不能**吞掉 scheduled(spec:"悄悄吞掉用户亲设的提醒是最伤信任的")。

### 2.5 深夜 / DND
- 平台**不设**硬性深夜静音;"深夜要不要出声"交 agent 按 `time` 自己判。
- `dnd=true`(=提醒关)只掐 **Delivery 那下 buzz**;Wake/Voice/写 chat 全照旧。

### 2.6 manual / 前台 永远穿透
- **manual 召唤**:绕过所有开关(连 DND/深夜都穿透,会 buzz),**且必须至少回一句**。
- **前台聊天 user_message**:不受任何开关影响(它不是 proactive)。

---

## 3. 已确认的 Bug(测试前必须知道,否则白测)

### Bug B —— 主动回复/心跳对 resident 从未触发(最高优,一行可修)
- **现象**:`v2_wakes=0 / proactive_messages=0`;主动消息从没出现过。
- **根因**:resident consumer 在广播态为空(`""`,平时都为空)时把心跳 trigger 派生成 **`heartbeat_unknown`**(`chat_resident_consumer.py:3716` + `_proactive_tick_trigger_for_broadcast_state:2459-2467`);gate(`gate.py:35-36`)对 `heartbeat_unknown` 无条件 `no_recent_frames` 拦死 → 不产 job。
- **对称性 bug**:hosted 同情况映射成 `heartbeat_broadcast_off`(放行 presence wake,`wake.py:114-124`),resident 却映射成 unknown(拦死)。
- **修法**:`chat_resident_consumer.py:3716` 默认 `""→"off"`(或空值→`heartbeat_broadcast_off`),与 hosted 对齐。**改完心跳才可能触发,§4 的 B/C 组才测得了。**

### Bug A —— 开关全关后定时提醒仍触发(legacy/V2 双系统并存 + resident 缺 V2 gate)
- **现象**:陪伴/定时/提醒全关,用户设的"X 小时后提醒"仍弹。
- **已排除 iOS 本地通知**:全 iOS 仅 `IdentityViewModel.swift:566` 一处 `UNNotificationRequest`,是**身份变化的即时推送**(`trigger: nil`);**没有任何 `UNTimeIntervalNotificationTrigger`/`UNCalendarNotificationTrigger` 之类"X 时间后"的本地定时通知**。→ **提醒是服务端来的,不是 iOS 本地绕过。**
- **新证据(/v1/proactive/debug)**:`jobs:26` 全是 `source:"agent_initiated_proactive"`(trigger=`photo_added`/`unlock_after_absence`/`manual_dynamic_island`),**全部 `status:pending`、`fired_at:null`、`delivered:null`**;同时 `v2_wakes:0`。
  → **后端有 legacy proactive job 队列 + V2 两套并存**:V2(开关 gate 的那套)被 Bug B 拦死从不触发;legacy 这套在攒 job。
- **根因(修正)**:V2 的 `fire_due_timers`(`scheduled_wake_v2.py:665`,带 `scheduled` gate)只在 hosted tick loop 跑、只遍历 hosted 用户;**resident 没有 V2 服务端 fire+gate 路径**。用户那个提醒疑似走 **legacy 路 fire/投递,绕过了 V2 的 `scheduled`/`reminders_delivery` 开关**。
- **待 Codex 定位**:① 那条提醒的**确切 fire 路径**(legacy job 队列?APNs?);② legacy 与 V2 双系统怎么收口(谁该负责 fire、gate 加在哪);③ `proactive_settings_v2` blob 全仓库**从没被 `.save()` 写过**,永远走 legacy fallback——是死代码还是漏写。

> 归属:Bug A/B 都是**后端/tooling = Codex**。iOS 三开关无 bug。

---

## 4. 详尽测试计划

> 观测手段:`/v1/proactive/debug`(JSON,带 `?key=` 或 X-API-Key)看 `settings / v2_wakes / v2_scheduled_wakes / decisions / proactive_messages`;
> VPS `journalctl --user -u feedling-chat-resident.service` 看 tick / job / reply。
> 状态:⬜ 未测 · ✅ 通过 · ❌ 失败 · 🚫 阻塞(依赖 bug 未修)

### A 组 · 前台 / manual(应永远工作,与开关无关)
| ID | 操作 | 期望 | 状态 |
|----|------|------|------|
| A1 | 前台发普通消息 | 秒回,真值 | ✅(已大量验) |
| A2 | 三开关全关 + 前台发消息 | 照常回(user_message 穿透) | ⬜ |
| A3 | 三开关全关 + manual 召唤 | 必回至少一句(manual 穿透,连 DND 都穿) | ⬜ |

### B 组 · 心跳 / 主动回复
| ID | 操作 | 期望 | 状态 |
|----|------|------|------|
| B1 | 陪伴开,tick(broadcast off) | 产 wake job → agent 跑 → 发出自发消息 | ✅ **2026-06-25 端到端通**:tick→claim→`proactive reply sent`(首条主动消息) |
| B2 | 制造一个"值得出声"的离散变化(如到家/久别解锁) | 产 perception_event → 一条自发消息 | ⬜ 待真机造事件 |
| B3 | 心跳醒来但无变化 | 判 `sleep`,用户无感(不打扰) | ⬜ |
| B4 | 关陪伴 + tick | **无 wake**(被 ambient gate) | ✅ 见 C1 |

> **B 组打通依赖**(2026-06-25 一并扫掉):① VPS env `PROACTIVE_POLL_ENABLED`/`PROACTIVE_TICK_ENABLED` 之前=false(总开关关着)→ 开;② Bug B(`heartbeat_unknown`)→ 已修部署;③ consumer venv 缺 `psycopg`+`psycopg_pool` → 已装(**手动,需写进部署**)。

### C 组 · 三开关 gate + 不连坐(信任攸关)
| ID | 操作 | 期望 | 状态 |
|----|------|------|------|
| C1 | 关陪伴 + heartbeat tick | 自发被 gate(`ambient_disabled`) | ✅ API 验:enqueued=False/ambient_disabled |
| C2 | **关陪伴 + scheduled_wake**(不连坐) | scheduled **不被** ambient gate | ✅ API 验:关陪伴下 scheduled_wake 仍 enqueued=True。**但到点真 fire 仍待**(见 E 组,resident fire 未建) |
| C3 | 关定时 + scheduled_wake | 被 gate(`scheduled_disabled`)+ 透明回灌 | ✅ gate 验:enqueued=False/scheduled_disabled。**透明 wake 回灌让 agent 解释** 待端到端验 |
| C4 | 关提醒(reminders_delivery=false)+ 触发主动 | **写进 chat 但不 buzz**(push suppressed) | ⬜ 待跑一条 reminders-off 的主动消息看 chat 元数据 push_decision=suppressed |
| C5 | 关提醒后开 app | 之前静默的消息浮出可见 | ⬜ |
| — | iOS↔后端字段往返 | POST ambient/scheduled → GET 原样回 + legacy 同步 | ✅ API 验通过 |

### D 组 · 深夜 / DND
| ID | 操作 | 期望 | 状态 |
|----|------|------|------|
| D1 | 深夜时段触发主动输出 | agent 按 time 自己掐 buzz;Wake/写 chat 照旧 | 🚫 依赖 B 组 + 需深夜 |
| D2 | DND 期间 manual 召唤 | 仍 buzz(manual 穿透 DND) | ⬜ |

### E 组 · scheduled wake(agent 自埋定时器)—— resident fire 已建(f772691)
| ID | 操作 | 期望 | 状态 |
|----|------|------|------|
| E1 | POST /scheduled/actions 埋 timer(at/tz/note) | 返回 timer_id status=scheduled | ✅ API 验:`sched_6ab07055...` scheduled |
| E2 | timer 到点(定时开) | consumer 60s loop 自动 fire → scheduled_wake job → agent 跑 → 发消息 | ✅ **2026-06-25 VPS 端到端**:fire→claim→realizing→`status:posted`(job trigger=scheduled_wake) |
| E3 | timer 到点但定时已关 | 不 fire 原 wake,回灌透明 background_result 让 agent 解释 | ⬜ 单测覆盖(PG),VPS 端到端待补 |

> 机制:resident consumer 默认每 60s 调 `POST /v1/proactive/scheduled/fire` → `ScheduledWakeServiceV2.fire_due_timers`
> (复用 scheduled gate + 透明回灌)→ 产 compat hidden job → 现有 jobs poll 链消费。f772691 + psycopg 部署。

### 开关字段对照(验 iOS↔后端写读一致)
| 验证 | 期望 |
|------|------|
| iOS 切某开关 → 立刻 GET /v1/proactive/state | 对应 `ambient`/`scheduled`/`reminders_delivery` 字段同步变;**且 POST 后 GET 原样回吐 V2 字段**(不能只更新了 legacy `enabled`/`dnd` 导致语义反掉)——后端待核对 |

---

## 5. 结论 / 下一步
- **先修 Bug B(一行)** → 解锁 B/C/D 组绝大多数测试,否则主动这块根本无从测起。
- **定位 Bug A**:先确认那个提醒是 iOS 本地通知还是服务端 fire(决定 gate 加哪)。
- 两个 bug → Codex;本测试计划 + 功能定义 → 已固化于本文,修完逐条回填状态。
- 与感知字段测试(`PERCEPTION_FIELDS_REALDEVICE_CHECKLIST_2026-06-25.md`)互补:那份测"读得对不对",本份测"主动行为对不对"。
