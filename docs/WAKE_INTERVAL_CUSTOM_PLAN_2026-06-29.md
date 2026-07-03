# 自定义陪伴频率（wake_interval）+ 心跳激活门 — 最终记录

作者:CC × Seven。原 spec 写于 2026-06-29(自定义频率),2026-07-04 追加**激活门**并一起 ship。
**状态:✅ SHIPPED 2026-07-04** — commit `0bc9e7d`(feedling-mcp `test`) + `758c823`
(feedling-mcp-ios `main`)。CHANGELOG 见当日 `[DONE]` 条。

> 本文是落地后的**最终记录**(what shipped),不再是待办 spec。历史迭代细节见 git log /
> mailbox;下方描述当前 test 分支的真实实现。

---

## 一、自定义陪伴频率

### 为什么
「陪伴」(ambient)开启时,系统定期叫醒 agent 做"在场检查"(presence/heartbeat),让它自己决定要
不要主动找用户。原间隔是**写死的全局 30min**(env `PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC`,
非 per-user)。Seven:有人想省 token / 少被打扰,有人想更勤——给用户可掌控的自定义空间。

### 边界（UI 文案已讲清）
`wake_interval_sec` **只控制「陪伴」模式下"闲时定期在场检查"的节奏**,不改变事件驱动唤醒的
**频率**(到地点 / 解锁 / 拍照 / 共享屏幕 screen_watch lane 各有自己的触发)。仅当 ambient 为开
时有意义。(注:激活门是另一层,见 §二——它在"首聊前"会连事件唤醒一起压住。)

### 设计决定（已 ship）
- 字段:**`wake_interval_sec`**(整数秒),per-user,挂在扁平 `proactive_settings` blob(和
  `wake_directive` 兄弟),**不经 `controls_v2.switches()`**(那只吃 bool)。
- **默认 7200(2h)**(Seven 2026-07-04 拍:比原 30min 平静省 token;改后端 default 1800→7200,
  影响所有没手动设过的用户,prod 量小风险低)。
- **Clamp `[900(15min), 43200(12h)]`**(Seven:去掉 10min 档,最小 15min;硬地板 600→900 同步提,
  防客户端绕 UI 传更小值)。
- iOS **7 档离散**:15min / 30min / 1h / 2h / 4h / 6h / 12h,默认高亮 2h。

### 实现（三层）
**① 后端(Codex)** `backend/core/store.py` + `backend/proactive/{gate,routes}.py`
- `store.py`:常量 `PROACTIVE_WAKE_INTERVAL_{DEFAULT=7200,MIN=900,MAX=43200}_SEC` +
  `normalize_proactive_wake_interval_sec()`;`load_proactive_settings` default 写入并 normalize;
  `save_proactive_settings` 白名单加 key,**F1 keep-old-on-save**(非数字 patch 不覆盖已有有效值,
  和 user_state/ai_state 邻居一致);load 路径坏 blob 仍 reset-to-default。
- `routes.py`:`_proactive_state_doc` 返回 `wake_interval_sec`;`proactive_state()` POST 子集白名单
  加 key。
- `gate.py::_build_proactive_v2_wake_decision`:decision 里带 `wake_interval_sec`(与 broadcast_state
  同层),consumer 直接从 tick 响应读——因为 consumer 主循环**不 fetch 完整 state**。

**② consumer(CC)** `tools/chat_resident_consumer.py`
- `_proactive_tick_interval_for_broadcast_state(broadcast_state, wake_interval_sec=None)`:有数值 →
  clamp `[900,43200]` 用之;None/非数字 → env fallback(默认已对齐 7200)。
- 主循环调度点(L6271 `decision = tick.get("decision")`)读 `decision.get("wake_interval_sec")` 传入;
  下一 tick 即时用新值,无需重启。

**③ iOS(CC)** `feedling-mcp-ios`(main)
- `SettingsView.swift::proactiveCard`:7 档 Menu 选择器,ambient 关时置灰禁用,当前值映射最近档,
  改动走 `setProactiveSwitch(wakeIntervalSec:)` → `updateProactiveSwitch` → POST `/v1/proactive/state`。
- `FeedlingAPI.swift`:`@Published proactiveWakeIntervalSec`(默认 7200) + `ProactiveStateResponse`
  解码 `wake_interval_sec` + `applyProactiveState` 映射 + `updateProactiveSwitch(wakeIntervalSec:)`。
- `Localizable.xcstrings`:双语(陪伴频率 / Companionship frequency + 说明 + 7 档)。

---

## 二、心跳激活门（2026-07-04 Seven 新增）

### 为什么
用户刚填完 API key、卡在 onboarding、或**根本没和 AI 说过话**时,心跳就开 = 纯烧 token、不符预期。
要求:**只有用户完成第一次成功聊天后,主动唤醒才自然开启**;之后用户自由调间隔(§一)。范围
(Seven 拍):首聊前**拦掉所有自发主动唤醒**(不止 heartbeat),手动/用户发消息放行(那本身就是首聊)。

### 设计（已 ship）
- **激活信号**:per-user flag `first_chat_ok_at`(iso ts),存扁平 `proactive_settings` blob。
  `store.mark_first_chat_ok()` **幂等**(已 set 不覆盖);**不进 save 白名单**(客户端不能 PATCH 伪造
  激活);`store.proactive_activation_ready()` 供 gate 读。
- **flip 点**:`backend/chat/routes.py::chat_response`(`/v1/chat/response`)——agent 回复成功
  append 后,若 `reply_to_message_id` 指向 `role=user & source=model_api` 的用户消息 → `mark_first_chat_ok()`。
  即"用户开口 + agent 成功回复"才算激活;onboarding greeting(genesis 另一条路)不算,普通 chat /
  坏 response 不算。
- **gate**(拦在 enqueue 前 = 零 LLM/token,gate 本身不调模型):
  1. `proactive/gate.py::_build_proactive_v2_wake_decision`:**未激活 && 非 manual →
     `block_reason="activation_pending"`**,should_reach_out=False,不 enqueue(优先于 wake_control /
     no-frame 判定;screen_watch 因 manual=False 也被拦)。覆盖 `/v1/proactive/tick` 主路。
  2. `perception/service.py`:感知事件有绕过 /tick 的 direct-enqueue 旁路——`_maybe_wake` /
     `_fire_wake` / `_submit_wake_event_v2_compat` / `_fire_wake_event_v2` 四处同加未激活 suppress。
  3. `agent_runtime/supervisor.py::_enqueue_introduction_job_if_needed`:post-respawn introduction
     未激活也 `return None`(不发主动介绍)。激活后 respawn 仍正常介绍。
- **不拦**:用户让 agent 排的提醒 / scheduled wake(post-activation 才存在);memory capture(静默、
  非 reach-out、无聊天不触发)。

---

## 验收（真机 e2e — 待 Seven 走一遍）
1. App 设 15min → 心跳约 15min 后 fire(`next=900s`);设 12h → `next=43200s`。
2. 关「陪伴」→ 无 heartbeat tick(选择器置灰);(激活后)拍照/到地点仍能唤醒。
3. 老用户 / 没手动设过 → 默认 **2h**。
4. GET `/v1/proactive/state` 回显 `wake_interval_sec`;tick 响应 decision 含该字段。
5. **激活门**:新用户没聊过 → 心跳 / 拍照 / 到地点 / introduction **都不触发**(后端 decision
   `activation_pending`、不 enqueue、零 token);发第一条消息且 agent 成功回复后 → 主动唤醒自然开启。

## 已验证（commit 前）
- 全量 pytest 对真 Postgres:**1462 passed**(10 个 pre-existing 失败非本次,已用清洁 origin/test
  worktree 隔离确认);consumer 189 passed;iOS `xcodebuild`(iphonesimulator) **BUILD SUCCEEDED**。
- DB-backed 测试后续由 CI(FEEDLING_TEST_PG)复跑。

## 运维提示
- 本次动后端 → push `origin/test` 会触发 **deploy-test-cvm**(整体 blip ~2min,通常自愈)。
- 每次部署要把 **compose_hash 上 Sepolia test 合约**——**部署钱包 gas 不足会挡真机 onboarding**,
  确认本次 deploy job 上链成功。

## 分工
后端字段 + clamp + state 路由 + tick 响应带字段 + 激活门(flag/gate/旁路/introduction) + 后端测试 →
**Codex**;consumer 读取改造 + 测试 → **CC**;iOS 选择器 + API + 文案 → **CC**。
