# Genesis Round 2 — 身份强替换 + voice 进前台合并 + 报错硬化 — 设计方案

状态:CC 写方案 → 待 Codex review → 执行 → CC review。
分支:后端沿用 `feat/genesis-onboarding-fix`(已合 test);iOS `feat/genesis-material-entries`。
前置:Round 1(mode 分派 / add_memory 范围 / R1-R5 / else 分支兜底 greeting)已合 test 在测。

---

## 变更总览
1. **#1 update_identity 改回强替换**(去掉 Round1 的 agent_name 底线),只在"空上传"时不生成。
2. **#2 voice + persona 全部拉进前台,并把 voice 抽取与 memory 抽取合并成一次调用**(减 LLM 调用、提成功率、进门即完整)。⚠️ 含"改抽取 prompt"的偏离项,见 §2。
3. **#3 报错硬化**:iOS genesis 轮询补 job.error 真实原因 + 240 超时报错;重试现状说明。

**互相影响**是本轮重点,见 §4。

---

## §1 update_identity → 强替换(决策已锁)

产品语义:用户主动传新角色卡 = 重定义 TA。与 onboarding 保持一致——onboarding 派生身份**也不要求必须有 agent_name**,所以 update 这里也不该有这个限制。

- **强替换**:派生出什么就整替什么,**agent_name 为空也照替**,不兜底、不报错(与 onboarding 对齐:onboarding 也不要求有名字)。
- **移除 Round1 的 agent_name 底线**:删掉 `replace_identity_preserving_anchor` 里 `if not agent_name: return "identity_update_incomplete"` 及 runner 对应分支。**不再要求有名字。**
- **两层守卫(Codex R1,已采纳)**:
  1. **输入空**:上传内容为空(没填、没传 character 材料)→ 不生成 job / 拒绝。
  2. **输出废卡**:派生出的 identity payload **彻底空壳**(agent_name / dimensions / self_introduction / category / signature **全都没有**)→ `identity_update_empty`(runner mark_failed),**不覆盖旧身份**。
     - 有 agent_name → 替 ✅;没名字但**任一** dimensions/self_introduction/category/signature 有值 → 也替 ✅;**全空才 failed**。
     - 关键:这**不是** Round1 的"没名字就失败"(那太硬)。这只拦"模型抽了张啥都没有的废卡"——**不要求名字,只要求至少有一样有效身份内容**。
- **保留**:无已有 identity → 409(先 onboarding);字段保留 id/created_at/relationship_started_at/anchor 不变。

> 注:这会让"新卡没名字 → 名字被替空"成为可能(有意与 onboarding 对齐),但**不会拿彻底空壳覆盖好身份**(废卡守卫挡住)。

---

## §2 voice + persona 进前台 + 与 memory 合并抽取(方案 a)

### 现状
- 前台:combined 无;`build_foreground_output`(write_core=False)只 `fact_map ×N` → full `fact_write` → identity → greeting。
- 后台:`voice_map ×N` → `voice_reduce` → `persona_build`。

### 目标(方案 a:全前台)
把 voice/persona 全部搬到前台,并把 **voice 抽取合进 memory 抽取的同一次 per-chunk 调用**:
```
前台(全部,顺序):
  🤖 combined map ×N   —— 一次调用同时出 { fact_candidates, voice_candidates }
  🤖 full fact_write ×1
  🤖 voice_reduce ×1
  🤖 persona_build ×1   —— 依赖 voice_reduce 的 behavior_notes/exemplars
  🤖 identity ×1
  🤖 greeting ×1
  → 全部落库 → 完成(进门即完整)
后台:onboarding 不再有独立 voice/persona 后台阶段
```
用户诉求:**不在意时间,要成功率 + 体感**。合并把 per-chunk 从 2N 降到 N(更少失败点=更高成功率);全前台 = 进门即有完整语气/人设(体感)。

### ⚠️ 偏离项 + 落地方式(Codex R2,已采纳:flag 门控 + 新函数 + 可回退)
**合并 fact+voice 到一次调用,必然要一个"同时抽事实+语气"的 combined 抽取 prompt** —— 打破"派生 prompt 一行不动"铁律。没有别的方式减少那 N 次。落地必须:
- **新增 `combined_map`(新函数 + 新 combined prompt),不改废旧的 `fact_map` / `voice_map`**(保留可回退)。combined prompt 意图 = 复用 FACT_MAP + VOICE_MAP,合成输出 `{fact_candidates:[...], voice_candidates:[...]}`。fact_write / voice_reduce / persona_build 三个 reduce prompt **不动**。
- **feature flag 门控**:`FEEDLING_GENESIS_COMBINED_MAP=1` 开;onboarding 开时走 combined,关时走旧的 fact+voice 分抽(都在前台)。
- **add_memory 无论 flag 开关都只 fact-only,绝不抽 voice**(见 §4)。
- **可回退**:真机 e2e 若记忆命中率 / voice 质量掉 → 关 flag 回退到"不合并、voice 逐块单抽但放前台"(不减调用但保质量)。质量对照是硬门槛。

### chat_ready / 进门时机
- 全前台后,job 完成 = 全套就绪。chat_ready 仍 = identity + greeting + 记忆;但由于都在同一前台顺序里,用户进门时 persona/voice 已就绪。
- 保留 else/弱身份兜底 greeting(Round1 已修),别回归。

---

## §3 报错硬化(iOS + 重试)

### iOS(genesis 轮询,`ChatEmptyStateView.pollGenesisImport`)
- **240 次超时别静默**:超时落失败态 + 明确提示(如"onboarding 超时,请重试"),不再 `isImportingHistory=false` 无声退出([:4543](App/FeedlingTest/Pages/Chat/ChatEmptyStateView.swift#L4543))。
- **失败带真实原因**:失败分支用 `job.error`,替换写死的 "genesis distillation failed"([:4529](App/FeedlingTest/Pages/Chat/ChatEmptyStateView.swift#L4529)),对齐 pollHistoryImport 的 `job.error ?? "…"`。

### 重试现状 + 建议
- **现有**:身份派生有 3 次重试(`foreground_identity.max_attempts=3`,空/瞬断)。
- **缺**:fact/voice 抽取、fact_write、greeting 无独立重试。
- **建议(可选,hx 定)**:既然 #2 追求成功率,给 combined map / reduce 步也加**有限重试**(瞬断/空→重试 1-2 次,cap 住浪费);硬错误(402)仍立即 failed(别把 402 也重试很多次)。

---

## §4 互相影响(本轮重点)

| 改动 | 影响 onboarding? | 影响 add_memory? | 影响 update_identity? | 必须处理 |
|---|---|---|---|---|
| #1 强替换 | 否 | 否 | 是(就是它) | 与 onboarding 对齐:两边都不要求 agent_name |
| #2 voice 进前台 + 合并抽取 | 是 | **是(隐患)** | 否 | ⚠️ **add_memory 复用前台抽取函数,必须开关隔离**:onboarding 走 combined(fact+voice),**add_memory 只走 fact,绝不抽 voice**(否则回归刚修掉的"加记忆白跑 voice") |
| #3 报错 | 是 | 是(同一轮询/runner) | 是 | 统一在 genesis 轮询/runner 层 |

**共用点提醒**:
- `_derive_identity_with_provider`(带"模型名护栏")被 onboarding + update_identity 共用 → "Gemini 被挡"两边都会发生;onboarding 弱身份走 else(已兜底 greeting),update 强替换(名字可能被替空,§1 已接受)。
- combined 抽取函数被 onboarding + add_memory 共用 → **必须用参数隔离 voice**(见上)。

---

## §5 验收(真机 e2e,复用 tools/genesis_e2e.py)
1. **#1 强替换**:已有 identity(名=X)→ 传含名新卡 → 身份变新名;传**无名新卡** → 身份被替成空名(**不 failed、不兜底**,证明强替换);**空上传** → 不生成/拒绝;无 identity → 409。
2. **#2 合并/全前台**:onboarding 完成后 persona/voice **进门即有**;combined map 每块只 1 次调用(voice_map 不再单独 ×N);**记忆命中率/语气质量 ≥ 合并前**(对比报告,防质量回退)。
3. **#2 隔离**:add_memory 跑完 **不产生任何 voice/persona 调用/产物**(沿用 Round1 断言)。
4. **#3 报错**:制造失败/超时 → iOS 明确报错带真实原因、不无限 loading。

---

## §7 执行顺序(Codex R,已采纳)
先做确定性的、把有风险的 combined 放最后灰度:
1. **#1 强替换 + 废卡守卫**(确定性,先做)。
2. **#3 iOS 报错硬化**(低风险)。
3. **#2 combined map**(flag 门控,最后做,带 e2e 对照)。

e2e 对照(#2 上/下线都要比):
- memory 数量 + ground-truth 命中率
- identity 是否稳定抽到"乔伊"(用非模型名 fixture)
- greeting 是否真实写入且可解密
- persona/voice 是否前台生成
- add_memory 是否 **0** voice/persona 调用

## §6 铁律与红线
- **reduce prompt(fact_write/voice_reduce/persona_build)不动**;唯一的 prompt 变动 = §2 的 combined 抽取(已标偏离,需 e2e 验)。
- add_memory / update_identity **绝不动相处天数**。
- 动加密信封 → 真机 e2e。
- 质量回退红线:§2 合并若使记忆/语气质量明显下降 → 回退不合并版。
