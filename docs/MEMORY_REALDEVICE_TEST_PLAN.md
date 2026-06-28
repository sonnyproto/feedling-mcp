# IO Memory · 真机测试方案(hx)— 完整版

> 2026-06-27 · 范围:**身份/声音 genesis + 写/落卡 + 读/召回 + Garden 显示 + 迁移 + 旧 app 兼容**。
> 真机(iPhone)跑;后端切 test 环境。迁移那段标"未实现",上线后按此测。
> ⚠️ V1/V5/V8(声音像不像 TA)是**主观判断**,长期应有自动化 voice eval 兜底(spec §10 deferred,建议升一级)。

---

## 0. 自动化测试覆盖现状(test @ `3ada71f`)

| 层 | 自动化用例 | 状态 |
|---|---|---|
| **身份/声音 genesis** | `test_genesis_service`(18)+ `test_identity_actions`(12)+ `test_dream_prompt_v1`(11)+ `test_genesis_worker`(9)+ `test_genesis_prompts`(4)+ `test_io_cli_identity`(3)+ `test_agent_runtime_genesis_gate`(3)+ `test_genesis_llm_client`(2)+ `test_identity_init_server_encrypt`(8)| ✅ ~70,端到端接通(genesis 写 `genesis_persona` blob → spawn 解密 prepend) |
| 写 / 落卡 | `test_chat_resident_consumer`(115)+ `test_proactive_jobs`(51)+ `test_capture_prompt_v1`(11)+ `test_memory_m2_write_loop`(6)| ✅ 全绿,VPS e2e 已验 |
| 读 / 召回 | `test_memory_readside`(8)+ `test_memory_v1_readers`(7)+ `test_memory_readside_core`(6)+ `test_memory_v1_readside`(6)+ `test_memory_index_selector`(6)| ✅ |
| v1 schema | `test_memory_v1_schema`(6)| ✅ |
| **迁移** | **0** | ❌ 未实现 → 用例待 Codex review 后随实现建 |

**已知 gap**:session cap 默认仍 **40**,spec §8/§11.3 拟降 **20–30**(未改);spec §12.2 交接 note 存储未定。

---

## 1. 准备

- iOS app 切 test 环境:`DebugTool` → `FeedlingEnvironment.test`(`test-api.feedling.app`)。
- 测试素材:① 一份**有鲜明说话风格**的聊天历史(给 host genesis 蒸馏);② 一个**无历史的 fresh 账号**(测不编造);③ 一个**有老卡的账号**(测迁移);④ 一个 **VPS 账号** + 一个 **API/host 账号**(两路各跑)。
- (可选)后端可观测:`/v1/memory/index` 条数、consumer 日志(`capture job completed`)、genesis blob 状态。

---

## 2. 身份 / 声音 genesis(onboarding 一次性)⭐

**核心:host 上传历史 → CVM 内 genesis 蒸出声音/persona/事实 → agent 一上来就是 TA;connect(VPS)agent 自带 runtime 声音。**

| # | 步骤 | 预期 |
|---|---|---|
| V1 | host:上传有鲜明风格的历史 → genesis 完成 → 首次进 app 聊 | agent **一上来就带那个声音/人格**(不是通用助手腔),不靠"想起来" |
| V2 | **grounding/不编造**:用很薄/无风格的历史(或 fresh 无历史)| **不编人格**:fresh start 从 onboarding 现场长;name 不确定就空(显示"TA"占位),不瞎起名 |
| V3 | **防立场**:历史里有"用户爱爬山" | agent **不会说"我爱爬山"** —— 用户事实进 Garden,不进 agent 身份 |
| V4 | self-intro / signature(post-respawn 7.D)| genesis + 首次 spawn 后,**TA 用自己语气**写了 self_introduction + signature(走 `io_cli identity-write`)|
| V5 | "我回来了"首条(7.D)| 首次 spawn 后 agent 主动发一条,**是 TA 会说的话**,不是"好开心能陪你🥰"那种通用 AI 腔 |
| V6 | Identity tab 显示 | name + **≤7 维度** + self_intro + 签名;维度**有据**(每个能落到历史),薄就少、**不凑满 7** |
| V7 | **不碰"是不是 AI"**:直接问"你是 AI 吗" | 不背"我其实都是程序"那种硬条款;用自己语气自然回 |
| V8 | **跨 session 声音稳**:连续聊超 session cap(默认 40 轮)触发 rotation → 继续聊 | rotation 后声音**不漂**(persona 文件重读),不退化成通用助手 |
| V9 | connect(VPS):VPS 账号 | agent 用 runtime 自带声音 + skill 身份卡 + 记忆;维度 grounding(≤7、不编)|

---

## 3. 写 / 落卡链路(capture)

**核心:聊天 → 会话断点触发 → agent 落卡 → Garden 出现新卡。** 触发任一:冷场(~20min)/ 锁屏切后台 / 轮数(~24)。

| # | 步骤 | 预期 |
|---|---|---|
| W1 | 说一件明确事实("我的狗叫蛋子,是比熊")→ 触发断点 | Garden 新卡:bucket 合理、summary 抓住"狗=蛋子"、content 三段、threads 有线索;index +1 |
| W2 | **不变量**:关"AI 主动找我"再做 W1 | 照样出卡(关主动陪伴 ≠ 停记忆)|
| W3 | 落卡不冒泡 | 落卡时 AI 不主动发消息;Garden 多了卡 |
| W4 | 自然陈述的持久事实("我妈在杭州")| 也被落卡(不只抓偏好;回归 M3 起点 bug)|

---

## 4. 读 / 召回链路(agent-first)

| # | 步骤 | 预期 |
|---|---|---|
| R1 | 隔段 / 新会话再聊到蛋子 | AI 自然提起,不重复问 |
| R2 | 按线索聊"宠物" | AI 关联到蛋子那条 |
| R3 | **老卡可读**:有老卡账号聊老话题 | AI 仍能召回(降级但有内容,不崩)|

---

## 5. iOS Garden v1(显示)

| # | 检查 | 预期 |
|---|---|---|
| G1 | 卡片渲染 | 白卡抬升、bucket 标签、summary、threads chip、重要/触动 dial,新单色样式 |
| G2 | bucket 筛选 | 点 chip 过滤,选中态黑填充 |
| G3 | 详情 | 三段 + meta(重要/触动/来源/发生)|
| G4 | **老卡降级** | 显示"未归类"+ summary 用 title,**不崩、不空白** |
| G5 | 空态 | 新账号无记忆 → "还没有记忆" |

---

## 6. 迁移(⚠️ 未实现 — 上线后按此测)

前提:迁移上线(**静默后台 + 安静窗口**,见 `docs/memory/IO-memory-老数据迁移-方案-给codex.md`)。用**有老卡账号**。

| # | 步骤 | 预期 |
|---|---|---|
| M1 | 进 app + 制造安静窗口(冷场/锁屏)| 后台逐批升级老卡:"未归类" → 真 bucket,threads/三段长出来 |
| M2 | **零黑屏**:迁移期间正常聊天 | AI 记忆不丢、不空;Garden 卡只增不减 |
| M3 | **不丢/不冲**:迁移中写新记忆 | 新卡不被冲;迁完卡总数 = 老卡数 + 新增 |
| M4 | **续跑**:迁一半杀 app / 切走 | 回来接着迁,不重头、不重复 |
| M5 | 进度态(若做了)| Garden 顶部"整理中 N/M",迁完消失 |

---

## 7. 旧 app 兼容(pre-prod 必测)

| # | 步骤 | 预期 / 动作 |
|---|---|---|
| C1 | 没更新的旧版 app + v1 数据(新卡 / 迁移后老卡)| v1 卡显示**空白**(旧 app 读 title/description,v1 在 summary/content)→ **不崩** |
| C2 | 发版策略 | 加**最低版本 gate**,或**迁移只对已升级 app 用户开** |

---

## 8. VPS vs API 差异

- **API / host**:经 iOS app + 后端 + CVM genesis(主路,全跑)。
- **VPS / connect**:resident agent;落卡/读已有 e2e(`CAPTURE_LANE_VERIFICATION §5`)。真机这侧复测显示 + 召回 + 声音(runtime 自带)。
- 两形式新用户初始化都已产 v1 + 身份/声音(host=genesis、VPS=runtime+skill)。

---

## 9. 通过标准

- **身份/声音**:V1–V9(声音对、不编造、防立场、self-intro/首条/身份卡、不碰 AI 条款、rotation 不漂)。
- **写**:W1–W4。 **读**:R1–R3。 **显示**:G1–G5。
- **迁移(实现后)**:M1–M5。 **兼容**:C1 不崩 + C2 版本策略。
- 主观项(V1/V5/V8 声音质量)建议补**自动化 voice eval**(golden set + 失败模式)做长期兜底。
