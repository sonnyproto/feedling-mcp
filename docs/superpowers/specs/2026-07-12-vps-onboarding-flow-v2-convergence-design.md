# VPS Onboarding 流程健壮性 + v2 收敛 — 设计方案

- 日期:2026-07-12
- 分支:`codex/vps-onboarding-flow-unify`(基于 `feedling-mcp` `origin/test` = d41e1c1)
- 状态:**待 Codex review + 用户确认**;确认后进 writing-plans
- 主要涉及仓:`feedling-mcp`(backend / consumer / io_cli)、`io-onboarding`(skills)。iOS 侧的观测性 / UI 对齐是**下一期**,本方案不做。

---

## 0. 北极星:收敛成一条路

一句话:**你以为 onboarding 整套升级到 v2 了,其实 VPS 上有几条"旁路"没跟上 —— 身份蒸馏另开手写 prompt、建记忆另开不读桶的岔路、身份 prompt 散在代码里、onboarding 步骤没趁手工具、流程没人喊"开始"。**

本方案的总纲是**收敛**:本该只有一条路 / 一套规则的东西,把 VPS 上另开的旁路并回主路。加上两条健壮性:**可靠启动(P1)** 和 **趁手工具(P2)**。

**先稳业务、再重构**(用户定的原则):能小改稳住的就小改,更彻底的重构(如桶复用下沉服务端)记为后续项,本期不做。

---

## 1. 范围

### 本期做(已和用户逐条锁定)

| 编号 | 一句话 | 严重度 |
|---|---|---|
| P1 | 流程没人喊"开始",agent 可能干坐着(表面卡住实则没启动) | 🔴 |
| P2 | onboarding 几步没 tool、裸拼 HTTP;skill 调用表述三套不统一 | 🟠 |
| A1(含 A2/A3) | 身份蒸馏用旧手写草稿、没跟 v2;服务端无校准兜底 | 🔴 |
| A4 | 二次蒸馏建记忆不读桶 → 盲建重复桶(简单版修) | 🟠 |
| A5(=C3) | 后端 `onboarding/validate` 仍返回废弃的 tab floor 配额 | 🟠 |
| B2 | 身份 prompt 散在 consumer 代码里,应挪进 skill 统一管 | 🟠 |
| P5 | 陈旧遗留 distill job 在 onboarding 期 `replace` 覆盖新身份卡 | 🟡 |
| B1 | io-onboarding skill 的 main 落后 test,canonical 分支不清 | 🟡 动作项 |

### 本期不做(明确划掉 / 推迟)

- **P3**(派生不出 agent_name 就问用户):用户要**一键 onboarding**,问名字得盯进程,反了 → 不做。
- **A6**(VPS 蒸馏一次性、无前台/后台快进):用户确认 **VPS 就该全做完**,是设计不是问题 → 不做。
- **C1 / C2**(genesis-v2 开关割裂 / v2-关兜底时序):v2 以后默认常开,割裂与兜底 bug 随之消失 → 不做。
- **桶复用下沉服务端**(A4 的彻底版):更干净但是重构,记为后续项。
- **iOS 观测性 + 进度 UI 对齐**(原 block 2 / block 3):下一期单独走 spec。

---

## 2. 现状证据(简版,已逐条在代码里坐实)

- **身份蒸馏**:`tools/chat_resident_consumer.py::_resident_derive_identity`(~L7700)用**手写 DRAFT prompt**(注释自写 "DRAFT wording (Seven to finalize)"),只要 `{agent_name, self_introduction, dimensions[]}`,**无"恰好 7 维 / 方差校准 / days / anchor / category / signature"**;写入走 `identity.replace`(`execute_identity_actions`,~L7764)。
- **记忆蒸馏**:`_resident_extract_memories`(~L7648)**复用 cloud genesis 引擎**(`genesis.worker.build_foreground_output_from_texts`),与 cloud 对齐;**但不注入账号现有 buckets** → 二次蒸馏盲建桶。
- **建身份校验**:`backend/identity/identity_core.py::init_identity`(L62)只校验 `days_with_user` + `relationship_anchor_evidence`,**无维度数量 / 方差校验**;`replace_identity`(L179)/ `profile_patch`(`backend/identity/actions.py:107`)同样**不校验校准**。skill 声称"init 会拦截 clustered dimensions"在代码里**找不到**。
- **capture 读桶**:`build_capture_prompt(buckets=...)`(~L6234)靠先拉 `/v1/memory/index`(~L6367)把现有桶注进 prompt;`io_cli memory-write` **纯透传 `--bucket`**,自己不查桶。
- **服务端归桶**:`backend/memory/actions.py:144 normalize_bucket_language` 只做**语言归一**(健康/Health → 单语言),**不匹配复用已有桶**。
- **validate 死配额**:`/v1/onboarding/validate` 的 `memory_garden` 步仍返回 `floors {story:3, about_me:8, ta_thinking:2, total:13}`(实测)。
- **io_cli 覆盖**:有 `memory-write / identity-write(=profile_patch)/ memory-index / 读类`;**无 `identity-init / onboarding-validate / chat-verify-loop / chat-greet`** → onboarding 4 步裸 HTTP。
- **skill 分支**:`skill.md` main==test;`skill-resident-agent.md` main 落后 test(缺 USER_MCP 段等),各 skill 自引用 URL 各指各分支。

---

## 3. 分工作流(Workstreams)

> 每条:问题(人话)→ 方案 → 改动文件 → 风险 → 留给 Codex 的开放问题

### WS1 — P1:onboarding 可靠启动(消灭"没开始"当"卡住")

**问题**:VPS 上没有任何东西触发/推进 onboarding,全靠 agent 读散文 skill 自己领悟"该动手写身份卡了"。领悟错(如误以为要等 App 上传)就干等、进度 0,外部看像"卡住",实则"没启动"。

**方案**(与 WS2 同一把工具落地):给 agent 一个**绝不会误会的单一入口**。
- 新增 `io_cli onboard`(或 `onboard status`):读 `/v1/bootstrap/status`,输出**当前处在哪一步 + 下一条该敲的命令**。例:
  ```
  ONBOARDING STATUS (resident)
    [✓] resident consumer connected
    [ ] identity card        ← 下一步
    [ ] live loop verified
    [ ] first greeting
  NEXT: io_cli identity-init --from-derivation   # 见 skill Identity 段
  ```
- resident skill 的 Step 0 明确:**onboarding 由 agent 主动发起**,连接后第一件事就是跑 `io_cli onboard` 看下一步;**没有"等待上传"这一步**(VPS 无上传触发的 onboarding)。

**改动**:`tools/io_cli.py`(新增 `onboard` verb);`io-onboarding/skill-resident-agent.md`(Step 0 措辞)。

**风险**:低。纯读 + 打印,不改状态。

**开放问题(Codex)**:`onboard` 是"只打印下一步"还是"能一键顺序驱动到底"(每步自动调下一步)?后者更接近"一键 onboarding",但把编排放进 CLI 会增加 CLI 复杂度。建议先做"打印下一步",一键编排作为可选增强。

---

### WS2 — P2:补齐 onboarding 工具面 + 统一调用路径

**问题**:onboarding 的 4 个核心步骤(建身份卡 / validate / verify-loop / 发问候)**没有 io_cli verb**,agent 只能裸 HTTP 猜 body(实测:`identity/init` 因 `{identity:{}}` 包裹 + `days_with_user` 该放顶层,连吃两个 400)。skill 里同一动作有 `feedling_*` 工具名、`GET {API}/v1/...` HTTP、以及 io_cli 三套表述,没说"你这类 runtime 用哪个"。

**方案**:
1. `io_cli` 新增 onboarding verbs,每个**封装正确契约 + 本地先校验 + 打印下一步**:
   - `identity-init`(POST `/v1/identity/init`;帮 agent 组 `{identity:{...}, days_with_user, relationship_anchor_evidence}` 的正确形状;本地先查"恰好 7 维 / 方差 / 非 runtime label",不合格直接报错让 agent 重写,而不是打到服务端才 4xx)
   - `onboarding-validate`(GET `/v1/onboarding/validate`,人话打印 next_action)
   - `chat-verify-loop`(POST `/v1/chat/verify_loop`)
   - `chat-greet`(POST 发第一句问候;走现有 chat post 端点)
2. **resident skill 明确 CLI-runtime 的规范路径 = io_cli**:onboarding 步骤一律给 `io_cli <verb>`,`feedling_*` / 裸 HTTP 只作"其他 runtime 的等价物"附注,不再让 CLI agent 自己映射。

**改动**:`tools/io_cli.py`(4 个 verb + 复用 `_http_json`);`io-onboarding/skill.md` 与 `skill-resident-agent.md`(把 onboarding 步骤的调用统一到 io_cli)。

**风险**:中。verb 的本地校验要和服务端契约保持一致(见 WS3 的服务端校验 —— 两边校准规则要同源,避免 CLI 放行、服务端拒,或反之)。

**开放问题(Codex)**:本地校验(CLI)与服务端校验(WS3)如何**共用一份规则**避免漂移?可否把校准规则做成一个可被 CLI 和 backend 都 import 的纯函数?

---

### WS3 — A1 / A2 / A3 / B2:身份蒸馏收敛到 v2 + prompt 归 skill + 服务端校准兜底

**问题**:VPS 把人设蒸成身份卡这步,用的是**代码里的手写草稿 prompt**(无 v2 硬规则),写入走 `replace` 且服务端**不校验校准** → 可能落库"正向偏置的正七边形"(全 80-90、无区分度),没人拦。而 cloud 的身份派生是 v2 规格。记忆那半已对齐,唯独身份分家。

**方案**(三件一起,才算真收敛):
1. **A3/B2 — prompt 归位**:把身份派生 prompt 从 `_resident_derive_identity` 挪进 **io-onboarding skill**(Seven 定稿),按 v2 规格写全:恰好 7 维、方差校准(spread≥40、≥2 维<60、≥1 维<40)、category、(signature 延后)、days/anchor 由 Step 0 提供。consumer 从 skill / 统一来源取该 prompt,不再内嵌草稿。
2. **A1 — 引擎收敛**:优先评估**直接复用 cloud 的身份 deriver**(genesis 派生身份那条)而不是维护第二份 prompt;若 VPS 场景(无 genesis persona、材料是上传人设)不适配,则退而用"同一份 skill prompt",保证规则同源。
3. **A2 — 服务端校准兜底**:在服务端加维度校准校验(恰好 7 维、方差阈值、agent_name 非 runtime label),**init 和 replace / profile_patch 共用同一校验** → 不管走哪条路,坏身份都落不了库。这也补上 skill 早就声称、实则不存在的那道关。

**改动**:`io-onboarding/skill*.md`(身份 prompt);`tools/chat_resident_consumer.py::_resident_derive_identity`(改用统一 prompt / 复用 cloud deriver);`backend/identity/`(共享校准校验,init + replace + actions 都过)。

**风险**:中高。① 服务端加校验会**收紧现有 replace 路径**,可能打到别的合法调用(如 profile_patch 的部分字段更新)—— 需区分"全量 replace"(校验)vs"部分 patch"(放宽);② 触碰身份写入,按项目铁律**必须真实 test 部署 e2e**(加密信封 AAD 不能只本地 fake-decrypt 验)。

**开放问题(Codex)**:
- 复用 cloud 身份 deriver 是否可行,还是 VPS 上传人设场景差异太大、只能同 prompt 不同引擎?
- 校准校验加在哪一层(`_identity_payload_from_plain`?一个新的 `validate_dimensions()`?)对 init / replace / profile_patch 的语义分别是什么(profile_patch 只改部分维度时如何校验整卡)?
- 是否需要一个"迁移期宽松"开关,避免存量不合格身份卡在下次 patch 时被硬拒?

---

### WS4 — A4:二次蒸馏建记忆"先读桶"(简单版)

**问题**:二次蒸馏(`_resident_extract_memories`)提炼记忆时**不注入账号现有 buckets/threads** → 在已有花园上盲建重复桶(已有"健康"又造"健康/Health")。而 capture 是靠先拉 `/v1/memory/index`、把现有桶注进 prompt 来"读桶再存"的;io_cli 透传不查桶;服务端只做语言归一、不匹配复用。

**方案(用户定的简单版,先稳)**:让二次蒸馏走**和 capture 同样的读桶动作** —— 蒸馏前拉一份现有 buckets/threads(`/v1/memory/index` 或 `existing_terms`),把它注入提取 prompt(复用 capture 的 `buckets=` 注入),让 agent 从现有桶里挑、够近就复用。**不动服务端。**

**改动**:`tools/chat_resident_consumer.py::_resident_extract_memories`(蒸馏前注入现有 buckets/threads,复用 capture 已有的注入代码)。

**风险**:低。只加"读一份现有桶喂进 prompt",不改写入契约。

**后续重构项(不在本期)**:把"匹配复用已有桶"下沉到服务端唯一落库口(`/v1/memory/actions`,扩 `normalize_bucket_language` 那个钩子)→ capture / 蒸馏 / inline / tool 全员绕不过、彻底一条路。等业务稳了再做。

**开放问题(Codex)**:注入用 `/v1/memory/index` 还是 `existing_terms(store, api_key)`?二次蒸馏是多 window map-reduce,现有桶要每个 window 都注入还是全局注入一次?

---

### WS5 — A5(=C3):清掉 `onboarding/validate` 的死配额

**问题**:`/v1/onboarding/validate` 的 `memory_garden` 步仍返回旧模型的 `floors {story:3, about_me:8, ta_thinking:2, total:13}`(v2 已废 tab/floor、0 卡合法)。虽 `blocking:false` 不拦流程,但端点还在"说"死模型,任何读 `floors` 的客户端/agent 会被误导。

**方案**:从 validate 响应里去掉 tab floor 计算,`memory_garden` 只报"informational"(当前卡数,无 floor、无 tab、非 gate)。

**改动**:`backend/hosted/onboarding_validation.py`(及其 core)。

**风险**:低—中。需确认**没有客户端仍依赖 `floors`**(老 iOS build?)。iOS 进度 UI 是下一期,但要先确认现网 App 不会因缺 `floors` 崩。

**开放问题(Codex)**:直接删 `floors` 字段,还是保留 key 但恒为空/0 以防老客户端解析崩?倾向后者更安全(渐进)。

---

### WS6 — P5:防陈旧 distill job 覆盖新身份卡

**问题**:二次蒸馏(App 上传 / VPS agent 上传)是 **onboarding 之后、稳定运行期**才发生的事,正常不与 onboarding 同时。但一个**陈旧/遗留的 `update_identity` distill job** 若在 onboarding 期间被 distill lane 认领,会 `identity.replace` **无守卫地覆盖**刚 init 的身份卡(历史上真翻过车,当时靠关掉整条 lane 规避)。

**方案**:加一个**针对性守卫**,让陈旧 job 覆盖不了新卡。候选:
- (a) distill lane 在 `replace` 前比对:job 的创建时间 **早于** 当前身份卡的 `created_at/updated_at` → 判定为陈旧,**跳过**(标记 job 为 stale/superseded)。
- (b) 后端在派发/完成 `update_identity` job 时,若身份卡是 onboarding 期新写的(有标记/在时间窗内),拒绝该 job。

倾向 (a)(改动小、在 consumer 侧、语义清晰:"上传发生在当前卡之前 = 过时")。

**改动**:`tools/chat_resident_consumer.py::_process_resident_distill_once`(replace 前加时间/新鲜度判断)。

**风险**:低。

**开放问题(Codex)**:job 上是否有可靠的"上传时间戳"能和身份卡时间比?若没有,是否需要后端在 job 里带上 `created_at`?

---

### WS7 — B1:io-onboarding skill 分支卫生

**问题**:`skill-resident-agent.md` 的 main 落后 test;各 skill 自引用 URL 各指各分支;不清楚发给用户的 canonical 是哪版 → 用户可能拿到旧说明书。

**方案**:定 canonical 分支策略并对齐。建议:**明确 test 为当前发布、把 test 追平进 main(或反之,定死一个),并统一自引用 URL 指向 canonical**。这是动作项 + 一个策略决定,不是代码逻辑。

**改动**:`io-onboarding`(分支同步 + URL 引用)。

**开放问题(Codex/用户)**:canonical 到底定 main 还是 test?App 实际拉的是哪个分支的 raw URL?(这决定同步方向。)

---

## 4. 依赖与建议顺序

1. **WS3 服务端校准 + WS2 CLI 校验** 要**同源**(先定校准规则的共享函数,再两边接)。
2. **WS1 + WS2**(io_cli onboarding 面)是一把工具,一起做。
3. **WS4 / WS5 / WS6 / WS7** 相对独立,可并行。
4. 触碰身份写入(WS3)**必须真实 test 部署 e2e**(加密信封铁律),排在有部署窗口时做。

建议批次:① WS1+WS2(工具面,立即减 AI 失误)→ ② WS3(身份收敛,带 e2e)→ ③ WS4/WS5/WS6/WS7(收尾)。

---

## 5. 验证(每条怎么算修好)

- **P1**:全新 resident 账号,agent 连接后跑 `io_cli onboard` 即得到明确下一步;不再出现"干等、进度 0"。
- **P2**:agent 用 `io_cli identity-init/validate/verify-loop/greet` 走完 onboarding,**零裸 HTTP、零契约 4xx**。
- **A1/A2**:VPS 蒸出的身份卡满足 v2 校准(方差≥40、≥2 维<60);故意喂"圣人卡"→ **服务端拒**(init + replace 都拒)。**真实 test 部署 e2e**。
- **A4**:在已有 N 个桶的账号上二次蒸馏 → **不新建重复桶**,复用现有桶。
- **A5**:`/v1/onboarding/validate` 不再返回 story/about_me/ta_thinking floor;现网 App 不崩。
- **P5**:构造一个早于当前身份卡的 `update_identity` job → distill lane **跳过**,身份卡不被覆盖。
- **B1**:main 与 test 的 resident skill 一致;自引用 URL 指向 canonical。

---

## 6. 留给 Codex 的总问题

1. WS3 校准校验:复用 cloud deriver 是否可行?校验加在哪层、如何区分全量 replace vs 部分 patch?是否要迁移宽松开关?
2. WS2/WS3 的校准规则如何做成 CLI + backend 共用的单一来源?
3. WS4 简单版是否够稳,还是应直接上服务端下沉(用户已明确先简单版,除非有强反对)?
4. WS5 删字段 vs 留空 key 的兼容取舍。
5. WS6 job 新鲜度判断需要的时间戳字段是否已具备。
6. WS7 canonical 分支定 main 还是 test。
7. 整体拆分/顺序是否合理,有没有被我漏掉的"VPS 还在用旧的"的地方。

---

## 附:已划掉/推迟项(供 Codex 确认无异议)

- P3 问名字(要一键)、A6 前台快进(VPS 全做完是对的)、C1/C2(v2 常开自消)、桶复用服务端下沉(后续重构)、iOS 观测性 + 进度 UI(下一期)。
