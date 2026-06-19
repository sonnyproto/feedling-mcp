# Round 3 (Proactive/Perception Runtime V2) — 工程师交接清单

合并前/上线前要交代给后端与 iOS 工程师的注意事项。配套文档:
`docs/ROUND3_VALIDATION_STATUS.md`(验证状态)、`docs/PROACTIVE_PERCEPTION_SPEC_V2.md`
(设计准绳,优先于落地计划)、`docs/PROACTIVE_PERCEPTION_ROUND3_EXECUTION_PLAN.md`。

分支:`proactive-perception-runtime`(已审完 PR1–PR10 + follow-up + CI 接入)。
优先级标记:🔴 = 阻塞/必须先确认,🟠 = 重要,⚪ = 知晓即可。

---

## 一、后端工程师

### A. Merge(`proactive-perception-runtime` → `test`)
- ⚪ **可干净合并**:`git merge-tree` 对 `origin/test` **0 冲突**。合并前用
  `git rev-list --count origin/test..HEAD` 确认最新 ahead 数。
- 🟠 **盯首次 CI**:合进 test 会**第一次**真正跑 V2 测试(新增 `Run Round 3 V2 regression suite` step,13 个文件)。此前从未在 CI 跑过(CI 只在 `main`/`test` 触发)。本地真 Postgres 16 跑过 `169 passed`,但 GitHub runner 首跑仍要盯。
- ⚪ **CI 主门**:`Start backend` + `test_api.py` 会跑 V2 app boot + `init_schema`,本地复现全过,合并 PR 应为绿。
- 🟠 **不需要数据库迁移**:V2 的表/流由 `init_schema` 起机时建(log 流是 `user_logs` 里按 stream 名的行;lease 等表由 init_schema 建)。**确认 test CVM 部署起机确实跑 init_schema**(app boot 会调)。

### B. Flag / 上线纪律(最重要)
- 🔴 **三个 flag 全默认 OFF、fail-closed**:
  `_hosted_wake_runtime_v2_enabled`(`backend/hosted/wake_consumer.py`)、
  `perception_ingress_runtime_v2_enabled`(`backend/perception/service.py`)、
  `resident_wake_runtime_v2_enabled`(`backend/proactive/resident_runtime_v2.py`)。
  **合并本身不改变任何生产行为**;只有逐用户翻 flag 才切到 V2。
- 🟠 **灰度翻**:逐用户,盯 `/debug/proactive`(per-user)+ `runtime_metrics_v2`(§10.3)。
- 🔴 **先别删任何 legacy executor**:每个删除是**单独的、观测窗口之后**的 PR(invariant 13),并带 deletion evidence。legacy 路径就是回滚路径。

### C. 运维注意(翻 flag 后才显现)
- 🟠 **hosted 主动唤醒依赖进程内缓存的 `last_seen_api_key`**:进程重启后、用户下次交互前,hosted 自发 wake 会**静默不触发**。观测时别误判成 bug。
- ⚪ **hosted `merge_window_sec=0.0` 是有意的**(wake 合并在 hosted 路径不启用,靠 60s tick 节流;代码有注释)。resident/inbox 端到端 own queue 时才用非零窗口。
- 🟠 **V2 输出仍走 legacy `proactive_jobs` 表当传输层**(兼容)。这张表 + 其语义仍是承重墙,删 legacy 时留意。

### D. 还需补的测试(审计未能闭环)
- 🟠 **真模型 agent 行为只做了单次 dry-run**(OpenRouter `claude-haiku-4.5`,4 场景:手动召唤出可见消息无 `ignored_manual`、环境低信号 sleep、定时唤醒出提醒、深夜克制——全过)。**但多轮工具执行循环没验证**:召唤场景里模型确实发了 `memory.fetch` 工具调用,dry-run 没跑工具执行器、没把结果喂回。建议补一个带(真/mock)模型 + 工具循环的集成测试,再信任多轮工具使用。
- 🟠 真机感知未验证(见 iOS 清单 B)。

### E. OCR → 小模型(屏幕共在 / D14)接入 ⭐
- ⚪ **现状**:后端**不做 OCR**。屏幕信号 = `screen_phash`(感知哈希,帧变化检测)+ iOS 端 on-device Vision 的 `scene_hint`。"看懂屏幕内容"(caption/VLM)**还没建**。
- 🟠 **接入点**:
  - `screen.read` / `screen.recent` 工具已在目录登记,但现在返回 `tool_not_implemented_in_pr3`(`backend/proactive/tool_executor_v2.py`)。接小模型时实现它们,让 `screen.read` 返回 caption。
  - `backend/perception/differ_v2.py` 已处理 `screen_phash` 信号(`METRIC_PHASH_FRAME`)。**小模型应只 caption 通过 phash 去重门的新帧**(成本控制:不变的画面不重复跑模型)。
- 🔴 **关键架构约束(务必先拍板)**:帧像素**明文后端永远看不到**,只能经 enclave 的 `/v1/screen/frames/<frame_id>/decrypt` 解密(`backend/perception/service.py:645-698`,注释明说 "backend never holds plaintext")。所以小模型若跑在**服务端**,**必须在 enclave/解密边界之内**跑,否则隐私模型就破了;否则就得**在端上**跑、把 caption 发上来。**"小模型放哪"直接决定隐私是否成立。**
- ⚪ **spec 是准绳**:屏幕属于 broadcast/共在 regime,受 `broadcast_state` 门控;`PROACTIVE_PERCEPTION_SPEC_V2.md` 优先于落地计划。

---

## 二、iOS 工程师

### A. 离散设备事件(🔴 可能是真机测试的 blocker)
- 🔴 **V2 靠离散事件唤醒**:`unlock_after_absence`、`screen_phash`、Wi-Fi/BT 连接。审计在 iOS 代码里**没找到 app 发这些**。请确认:app 到底发不发这些离散设备事件(带 `wake_trigger` / `safe_screen_phash` / 连接锚点)?
- ⚪ **现状 app 只发**:`/v1/perception/report`(`context_snapshot`,连续信号,**按设计 pull-only 零唤醒**)+ `/v1/perception/photo/evaluate` + `/v1/perception/app_open`。
- 🔴 **结论**:若 app 不发离散事件,真机测试能测到 photo/app_open,但**测不到 连接/解锁/屏幕 的感知唤醒**——无论后端 flag 怎么翻。需确认这些是 (a) 漏发、(b) 后端从 snapshot 服务端推导、还是 (c) 待补的 iOS 功能。

### B. 感知契约 fixtures 对齐(gate b)
- 🔴 后端 `tests/test_ios_perception_contract_v2.py` 的 fixtures 是**读 Swift 推出来的,不是真机抓的**。请**真机抓一次真实 report,和 fixtures 逐字段比对**。不一致 = 静默的感知失败。

### C. 照片处理变更确认(已上线、立即生效)
- 🟠 §2.1:照片**硬阻断已移除**。敏感照片现在**加密存储**(`sensitive=true, status=stored`),后端永不见明文。请确认 iOS 的 photo evaluate/上传流匹配:敏感场景仍**加密上传**(不再被拦),解密路径可用。

### D. 屏幕帧 / 小模型边界(与后端 E 联动)
- 🟠 小模型 caption(D14)会消费屏幕帧。确认:`screen_phash` 在**端上算**并以 `safe_screen_phash` 上报;帧经 broadcast 扩展 + enclave frame-decrypt 路径;`broadcast_state` 生命周期(`SharedConfig.swift` 的 `screen_broadcast_state`)正确。
- 🔴 **架构决定 iOS / 后端要一起拍**:小模型在 enclave 服务端跑 → iOS 继续发加密帧走解密路径;在端上跑 → iOS 跑模型、发 caption 上来。

### E. 三个开关 UI + wake_interval
- 🟠 V2 有**三个用户开关**:Ambient / Scheduled / Delivery-as-Reminders(后端键:`ambient` / `scheduled` / `reminders_delivery`,见 `backend/proactive/controls_v2.py`)。确认 iOS 把三个**都暴露**给用户——尤其 **Scheduled 独立于 Ambient**(关 Ambient 不该关掉定时)。早先 P4 标了 "iOS UI 待做",`wake_interval` 生效 + 这三个开关 UI 可能还没落地。后端认这些开关,iOS 不出 UI 用户就控不了。

### F. 时区
- 🟠 V2 的克制判断用 `local_time` + `timezone`,来自 settings.timezone(report 里 `device_context.timezone`)。确认 iOS **确实写入用户时区**;不写就 fallback 到默认 `Asia/Shanghai` → 异地用户的"深夜克制"会错。

### G. 通用(既有约定)
- ⚪ 任何新增 iOS 用户可见文案必须**双语**(`isChinese` flag),单语 patch 过不了审。
- ⚪ 涉及 Live Activity / 新感知 API 的,**部署目标 16.2+**。

---

## 三、跨端要一起拍的决定
1. 🔴 **小模型(屏幕 caption / D14)放哪**:enclave 服务端 vs 端上 —— 决定隐私模型是否成立(后端 E15 + iOS D)。
2. 🔴 **iOS 是否/如何发离散设备事件** —— 决定 V2 感知唤醒能否在真机被触发(iOS A)。
