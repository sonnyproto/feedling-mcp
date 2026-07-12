# VPS Onboarding 流程健壮性 + v2 收敛 — 设计方案(后续 backlog)

- 日期:2026-07-12
- 分支:`codex/vps-onboarding-flow-unify`(基于 `feedling-mcp` `origin/test`)
- **状态:🅿️ 后续 backlog。** 用户 2026-07-12 决定:**先做 onboarding 第一版问题 + 前台任务补齐,本整套 v2 收敛延后。** 本文档为存档,待前台任务完成后再启动。
- 已过一轮 Codex review(见 §8),核心修正已并入本稿。
- 涉及仓:`feedling-mcp`(backend / consumer / io_cli)、`io-onboarding`(skills)。iOS 观测性 / 进度 UI 对齐 = 再下一期。

---

## 0. 北极星与总原则

**北极星:收敛旁路。** VPS 上有几条没跟上 v2 的旁路:身份蒸馏另开手写 prompt 且丢人格字段、建记忆不读桶盲建、onboarding 步骤没工具靠裸 HTTP、流程没"启动"信号、旧 floor 叙事散落多处。

**总原则(用户定,0712):**
1. **在保证一定质量的前提下,优先 onboarding 成功率。** 质量不足可由二次蒸馏事后补。
2. **先稳业务,再重构。** 能小改稳住的不趁机大动(如"桶复用下沉服务端"记为后续)。

**身份契约决定:选 B —— 证据优先、稀疏允许(不强制 7 维、不因聚集/稀疏拒卡)。** 理由:优先成功率,二次蒸馏可补维度。→ **policy 是 lenient 的:只拦结构垃圾,绝不因"维度少/聚集"拒卡;质量靠 prompt 引导 + 事后蒸馏。**

---

## 1. 最终问题集

| 编号 | 一句话 | 严重度 |
|---|---|---|
| P1 | 流程没人喊"开始",agent 可能干坐着(表面卡住实则没启动) | 🔴 |
| P2 | onboarding 几步没 tool、裸拼 HTTP;调用表述三套不统一 | 🟠 |
| A1(含 A2/A3/B2) | 身份蒸馏用旧手写草稿、没跟 v2;丢人格字段;prompt 散在代码;无 lenient policy 兜底 | 🔴 |
| A4 | 二次蒸馏建记忆不读桶 → 盲建重复(简单版:蒸馏侧读快照) | 🟠 |
| A5(=C3,已扩大) | **所有** server-facing 旧 floor 叙事(validate + memory/verify + gates + docs) | 🟠 |
| P5 | 陈旧 distill job 在 onboarding 期覆盖新身份卡(用并发基线,非时间比) | 🟡 |
| B1 | io-onboarding skill main/test 双轨,缺 promotion gate | 🟡 |

**划掉/推迟**:P3(要一键、不问名字)、A6(VPS 全做完是对的)、C1/C2(v2 常开自消)、桶复用服务端下沉(后续重构)、iOS 观测性 + 进度 UI(再下一期)。

---

## 2. 身份契约与 policy(Batch 0,最先定)

**契约 = B(证据优先、稀疏允许)。** 落成一个纯 Python 模块,io_cli 和 backend **都 import 同一份**:

```
backend/identity/card_policy.py
  normalize_identity_card(...)
  validate_full_identity_card(...)   # init / 全量 replace
  validate_profile_patch(...)        # 局部改
  validate_dimension_nudge(...)      # 单维微调
```

**三档写入语义(Codex 修正,关键):**
- **全量 init / replace**:校验完整卡 —— agent_name 非 runtime label、维度结构合法(name/value 0-100/description)、维度名不重复、category 契约。**因为选 B:不强制恰好 7 维、不强制 spread/低维数量**(这些降级为 prompt 引导 + 可选质量标签,不做硬拒)。
- **profile_patch**:**只校验本次改动字段**(改名只校名、改 tone_style 只校长度),**不因旧卡稀疏而拒**。若未来允许 patch dimensions,则合并进当前明文卡后再校"合并后的完整卡"。
- **dimension_nudge**:只校目标维度存在 + 新 value 在范围;**不要求 nudge 后仍满足任何全局形状规则**。

**迁移兼容(不做长期全局开关):** 新 init 严格结构校验;全量 replace 严格;patch/nudge 允许旧卡继续存在只校本次改动;旧卡第一次全量 replace 要求升级到新结构。可加 `policy_version` 标签便于统计存量。

---

## 3. 分批(采纳 Codex 重拆)

### Batch 1 — onboarding 工具面 + 启动信号 + 体检(不碰身份写入语义)
落地 **P1 + P2 + 成功率护栏**:
- `io_cli` 新增 onboarding verbs,每个封装正确契约 + 本地预校验 + 可读报错 + 打印下一步:`identity-init`(支持 `--fresh-start` 自动填 days=0 + 标准锚点证据)、`onboarding-validate`、`chat-verify-loop`、`chat-greet`。
- `io_cli onboard`:读 bootstrap-status 打印当前步 + 下一条命令。
- `io_cli onboard start`:**幂等写一条 `resident_onboarding_started` 信号**(外部才能区分"agent 没动 / 已启动 / 卡在某步")。
- `io_cli doctor`:五项体检(API / enclave / identity 可读 / memory index 可读 / chat write 可用)—— 专治 sandbox 禁网、key 无效、enclave/consumer 不通等"神秘失败"。
- resident skill 把 onboarding 步骤统一指向 io_cli(feedling_* / 裸 HTTP 只作其他 runtime 等价物附注)。
- **不做** CLI 全自动跑到底(身份派生/起 service/verify 含 agent 操作,塞进脚本会变成另一套编排器)。

### Batch 2 — 身份蒸馏收敛(带真实 test 部署 e2e)
- VPS 保留自己的 **source adapter**(读上传人设),**共用 Batch 0 的 card_policy** 做 normalize + validate。**不强求整条复用 cloud deriver**(cloud 当前是"最多 7 维、证据优先",与本契约不同;强行复用只会把不一致藏起来)。
- **补齐人格字段**:`category / tone_style / agent_role / do_not_say / boundaries`(cloud 有、VPS 现在丢了;signature 按产品规则)。
- **身份 prompt 用共享的可执行模板/常量**(不是让 consumer 运行时抓 skill Markdown 切段)。skill 由模板生成或人工同步。
- 建卡/换卡走严格结构校验,改局部走宽松。**真实 test 部署 e2e**(加密信封铁律)。

### Batch 3 — 记忆读桶 + 防覆盖
- **A4(简单版)**:蒸馏开始前拉**一次**记忆快照(现有 buckets/threads + **现有卡摘要**),整个 job 复用(不每 window 重拉);把摘要喂给 fact-write 的 **`known_memories`** 做语义去重(桶名只能防"健康/Health",防不了"焦虑先给结论"vs"不喜欢长铺垫"这种语义重复)。验收 = 同名/中英桶必复用 + 重复率显著下降(**不写"绝不重复",LLM 保证不了**)。
- **P5(并发基线,非时间比)**:App 建 `update_identity` job 时带 `base_identity_updated_at` / `base_identity_revision`;`/v1/genesis/resident/pending` 返回它;replace 前做 optimistic concurrency check —— 只有"job 之后发生过 init/全量 replace"才判冲突并 supersede;**单纯改 signature/profile 不算冲突**(改成读当前卡 merge 后再 replace)。(注:现有 pending 不返回任何时间戳,`updated_at` 被 claim/heartbeat 刷新不能用作新鲜度 —— 都要补。)

### Batch 4 — 清所有旧 floor + 文档 + 发布
- 清 **所有** server-facing 旧配额叙事,不只 `onboarding/validate`:
  - `onboarding/validate`:删 `floors/counts/missing_tabs`(iOS `OnboardingValidationStep` 不解码它们,Swift 忽略多余字段 → 直接删安全;不留 `{}`/0)。
  - **`/v1/memory/verify`**:仍在算 floor / 返回 below_floor / 输出 "identity_init will 409 until…" / 用旧 floor 判 passing —— **最明显的旧 v1 旁路,必清**(`memory/memory_core.py::verify`)。
  - `bootstrap/gates.py`、history import 的 `floor_reference`、provider/tool descriptions。
  - io-onboarding `quickstart / troubleshooting / skill-api` 的 four-pass/floor 叙事。
  - 顺带:iOS `stuckPrompt`(`chat.empty.stuck_prompt`)里的 "Pass 1-4" 文案也更新(见 §7)。
- **B1**:双轨保留(main=生产 canonical、test=测试),补 **promotion gate** —— test 验完把 skill promote/merge 到 main;自引用 URL 与当前分支一致。

---

## 4. Onboarding 成功率 & 护栏(本方案的重心)

### 可能堵的点
- **步骤/必填**:①建身份卡硬性必填 anchor 证据(≥8 字符)+ days —— fresh start 给不出会 400(→ `--fresh-start` 出路);②契约猜错 400;③verify-loop 30s 超时 + CLI 冷启动 → 卡"验证中"。
- **环境(神秘失败)**:④key 无效/没注册 → 全 401;⑤sandbox 禁网 → 读不到记忆但 verify 假过;⑥enclave 不可达;⑦consumer 没连/没轮询 → live loop 永不过。
- **agent 自卡/静默**:⑧不知道要主动开始(P1);⑨蒸馏坏 JSON → `return None` 静默失败;⑩派生不出名字又不敢写。
- **旧模型当关卡**:⑪`memory/verify` 旧 floor 若还判 passing → 0 卡被卡。

### 护栏(对策)
1. 单一入口给下一步(`onboard`)→ 不瞎猜、不干坐。
2. 契约封装 + 本地预校验 + 可读报错(verbs)→ 不猜 body。
3. `doctor` 前置体检 → 环境不通开场暴露(治 ④⑤⑥⑦)。
4. `identity-init --fresh-start` → 给不出锚点也不堵(治 ①)。
5. 派生不出名字 → 安全默认名 + 标"待用户改",继续(治 ⑩)。
6. lenient policy(B)→ 稀疏/聚集都算成功(治"质量规则自造堵点")。
7. 坏 JSON → 重试一次 + 报错到 setup log,不静默吞(治 ⑨)。
8. 每步幂等可恢复 → 中断重跑 `onboard` 接着走;`init` 409 → 自动转 `replace`。
9. verify-loop 冷启动兜底 → 先 warm CLI / 超时重试一次(治 ③)。
10. setup 与 IO Chat 强隔离(`chat/response` 409 已 enforce,保持)。
11. skill 钉死"同一 agent 新入口、非新角色"→ 不跑偏成新人格。

---

## 5. 依赖与顺序

Batch 0(契约/policy)最先 → Batch 1(工具面/护栏,立即减 AI 失误)→ Batch 2(身份收敛,带 e2e)→ Batch 3(记忆/防覆盖)→ Batch 4(清 floor/文档/发布)。触碰身份写入(Batch 2)必须真实 test 部署 e2e。

---

## 6. 验证(每条怎么算修好)

- P1:全新账号,agent 连后跑 `onboard` 即得下一步 + `onboard start` 落信号;不再"干等进度 0"。
- P2:走完 onboarding **零裸 HTTP、零契约 4xx**;`doctor` 能提前报出环境问题。
- A1:VPS 蒸出的身份卡含人格字段;结构非法被 policy 拒、稀疏/聚集**不**被拒;真实 e2e。
- A4:已有 N 桶的账号二次蒸馏 → 同名/中英桶复用、重复率显著下降。
- A5:validate 与 memory/verify 都不再返回/依赖旧 floor;现网 App 不崩。
- P5:构造 base revision 冲突 → supersede;只改 signature → 正常 merge 执行。
- B1:main 与 test skill 经 promotion gate 一致。

---

## 7. iOS "Stuck?" 自检按钮(已存在,本期不动)

`isStuck`(10 分钟无进展)→ `serverStuckSection` → "复制排查指令"(`stuckPrompt`,`ChatEmptyStateView.swift:5204` / `chat.empty.stuck_prompt`)。
- **作用**:卡死后的**手动逃生口**,逼 agent 自报"skill 读没读 / Step 0 / consumer 轮询否 / 卡哪步 / 错误",覆盖 P1 诊断侧。
- **局限**:被动(等 10 分钟)、手动、只诊断不预防;且提示词自身**还带旧 "Pass 1-4" 文案**(v2 已废)。
- **与本方案关系**:`doctor`/`onboard` 是主动预防版,互补;Batch 4 清 floor 时**顺带更新这段 Pass 文案**。**用户明确本期不动它。**

---

## 8. Codex review 已采纳的修正(存档)

1. **不把"复用 cloud deriver"当 WS3 答案** —— cloud 是"最多 7 维、证据优先",skill 是"严格 7 维";两个产品契约。改为:derivation(source adapter 可不同)+ **shared IdentityCardPolicy**(同 policy、同字段语义)。用户最终选 **B**,policy 转 lenient。
2. **VPS 身份蒸馏丢人格字段**(category/tone_style/agent_role/do_not_say/boundaries)—— 补。
3. **校验分三档**(full / patch / nudge),放 `card_policy.py`,io_cli + backend 共用;不放 skill md / consumer / argparse / route。
4. **迁移兼容按操作语义**,不做长期全局 `STRICT` 开关。
5. **WS4 用 `known_memories` 语义去重**,快照贯穿整 job;验收非绝对。
6. **WS5 扩大**:`memory/verify` 才是最明显旧旁路,连同 gates/docs/provider 描述一起清。
7. **WS6 用并发基线(revision)**,非 `job.created_at < identity.updated_at`(会误杀"上传后改签名"的正常流程);pending 需补返回时间戳/revision。
8. **WS7 双轨 + promotion gate**,非 main/test 二选一。
9. **别让 consumer 运行时解析 skill Markdown 取 prompt** —— 用共享可执行模板。
10. 新增 `doctor`/readiness smoke(治 sandbox 禁网假过)+ `onboard start` 状态信号。
