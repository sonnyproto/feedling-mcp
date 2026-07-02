# Genesis Onboarding 蒸馏修复 + 三入口写范围 + 报错硬化 — 设计方案

状态:Codex 已 review(R1-R5 + 6 断言已折进,见各节标注)→ **待 Codex 按此收敛版执行** → CC review
分支:后端 `feat/genesis-onboarding-fix`(基于 origin/test);iOS `feat/genesis-material-entries`(基于 origin/main,含三个 material sheet)

---

## 铁律(不许碰)

- **蒸馏派生逻辑 / 提示词一行不动**。本方案只改"**调用哪几步、写哪些产出、按什么 mode 分派**",属编排层。
- 动到加密信封(envelope id / K_enclave / AAD / 写 identity/memory 的信封构建)→ **必须真实 test 部署 e2e**,本地 fake-decrypt 不算数。

---

## 背景

### 现状 1:v2 前台身份吃"薄卡"(要修)
v2 为了前台快进,身份/开场白在"全部记忆还没抽完"之前就跑,只吃到 **3-5 条 core 记忆卡**(代码里上限还写着 `[:40]`,但前台根本凑不出)。857c09e/v1 是先抽完全部记忆、身份能吃到最多 40 张卡(全历史浓缩)。
- 影响:大历史时身份/开场白**看不全 → 抓不准**。
- 注:原文采样(identity 12000 字 / greeting 8000 字)v2 没改,和 857c09e 一样,**不是本次问题**;问题只在"记忆卡从满卡掉到 3-5 条"。
- 前台**本来就已经对所有块跑了 `fact_map`**(为了挑 core),候选全在手,所以写全量记忆只是把那次 `fact_write` 从"写5条"改成"写全量"(仍 1 次 LLM 调用)。

### 现状 2:iOS 三入口语义混,后端分不清(要修)
三个入口都打同一个 `uploadGenesisPlaintext` → 后端一律跑"全量蒸馏"(覆盖 identity + 重算相处天数 + 写记忆 + persona/voice):

| 入口(iOS) | 该做什么 | 现在后端实际做的 | 问题 |
|---|---|---|---|
| ChatEmptyStateView(onboarding) | 全套 | 全套 | ✅ 对(但吃薄卡,见现状1) |
| GardenMaterialSheet(加记忆) | **只加记忆** | 全量:**覆盖身份 + 重算相处天数** | ❌ 加条记忆把人设/相处天数搞坏 |
| IdentityMaterialSheet(改身份) | **只更新身份** | 全量:也会动相处天数、写多余东西 | ❌ 做了不该做的 |

### 现状 3:onboarding 最后一步报错有两个洞(要修)
iOS `pollGenesisImport`([ChatEmptyStateView.swift:4497](App/FeedlingTest/Pages/Chat/ChatEmptyStateView.swift#L4497)):
- ✅ job 明确 `failed` → 停 loading + 报错。
- ⚠️ 洞1:轮询 240 次上限,若 job 既不 failed 也不 completed(卡 processing / worker 挂)→ 240 次后**静默停,无报错**。
- ⚠️ 洞2:报的是写死的 "genesis distillation failed",**没带真实原因**(不像 history import 用了 `job.error`)。

---

## 目标 / 非目标

**目标**
1. **(B)修身份吃满卡**:onboarding 前台写**全量记忆** → identity/greeting 吃**全量卡**;去掉后台那次重复 `fact_write`;voice/persona **仍留后台**(前台不等它们,快进不变)。
2. **三入口 → 三写范围**:后端按 mode 分派,加记忆只写记忆、改身份只更新身份、onboarding 全套。
3. **报错硬化**:补上两个洞。

**非目标(明确不做)**
- 不做 A(合并 fact_map+voice_map):只省后台成本、不提体感速度,本次不碰。
- 不做 C(减块数采样):动召回,本次不碰。
- 不做 identity+greeting 合并调用。
- 不动派生逻辑/提示词。

---

## Part 1:后端 — genesis plaintext 变成 mode-aware

### 1.1 引入 mode(三选一)+ 进幂等逻辑(Codex R1)
`/v1/genesis/imports/plaintext` 请求体加 `mode` 字段:`onboarding` | `add_memory` | `update_identity`。

**向后兼容**:老 app 不传 `mode` 时,后端按 `client_job_id` 前缀兜底**推断** mode:
- `garden-*` → `add_memory`;`identity-*` → `update_identity`;其它 / 无 → `onboarding`(默认)。
- 显式 `mode` 优先;前缀**只用于推断出 mode**,不绕过下面的 mode 匹配。

**mode 必须进幂等 + metadata(否则串入口)**:
- `mode` 加进 `SAFE_JOB_METADATA_KEYS`(否则 `_safe_job_metadata` 会把它过滤掉),或单列 `genesis_mode` 字段持久化。
- `_find_reusable_plaintext_job` 复用判定**必须要求 mode 一致**:现在只看 `client_job_id` / `input_hash`,同一份材料先 `add_memory` 再 `update_identity` 时 input_hash 相同 → 会错误复用旧 job 导致第二次不执行。加上 mode 匹配后才不串。

### 1.2 三种 mode 的写范围

| mode | 写 memory | 写 identity | 相处锚点 anchor | persona/voice | greeting |
|---|---|---|---|---|---|
| **onboarding** | ✅ 全量(前台) | ✅ 全量卡派生(前台) | ✅ | ✅(后台) | ✅ |
| **add_memory** | ✅ 追加 | ❌ 不碰 | ❌ **不动** | ❌ | ❌ |
| **update_identity** | ❌ 不写 | ✅ 整张覆盖 | ❌ 不动 | ❌ 不重建 | ❌ |

### 1.3 onboarding mode 的具体改动(= B)(Codex R2 + R3)
在 `_run_plaintext_genesis_v2`(前台)里:
- **前台真正写全量记忆(R2):现在前台对所有 chunk 跑了 `fact_map` 但只对 core 做 `fact_write`**。要改成:前台对 **`all_fact_candidates` 做一次 full `fact_write`**,把候选真正落成全量卡。**不是把 `core_memories` 换个变量名**——中间隔着"对全量候选执行一次写入"这步。
- `identity` / `greeting` 传入的 `memory_cards` 用**这份 full memory set**(不再是 3-5 条 core)。
- **后台去掉 fact_write 要从根上省,不是只删调用(R3)**:后台 `_run_plaintext_background_enrichment` 调的 `worker.build_reducer_output_from_texts` **内部仍会跑 `_fact_write`**;只删 `routes.py` 那行 `apply_memory_outputs` → LLM 还是白跑一次(成本 + 402 风险仍在)。正解:**给 worker 加编排开关**(如 `include_memory=False` / `skip_fact_write=True`),后台只跑 voice_map/voice_reduce/persona_build。**只改调用范围,不动 prompt,符合铁律。**
- 快进契约不变:chat_ready 仍 = identity + greeting + 够记忆;voice/persona 仍在后台,用户不等。

### 1.4 add_memory mode(决策已锁)
- 复用现有"只写记忆"能力(`apply_memory_outputs`),抽事实 → **直接追加**写入花园。
- **不去重**(MVP):同段材料反复传会堆重复,由 **dream 夜间 merge 兜底**(见 §1.7)+ 用户可手删。
- **明确跳过**:identity 派生、`_store_identity_payload`、relationship anchor 重算、persona/voice、greeting。
- 相处天数绝不能被这条路改动(这是之前踩过的坑)。

### 1.5 update_identity mode(决策已锁)(Codex R4)
- 用上传的 character 材料跑一遍身份派生 → 生成一份身份卡 → **无脑整张覆盖**(blind replace,不合并)。
- **不能复用 `init_identity_if_absent`**:它有身份时也更新,且 `_relationship_anchor_from_output` **会重算相处天数**(service.py:65),与"绝不动相处天数"冲突。
- **新做一个 `replace_identity_preserving_anchor`**:只替换加密 body,**保留原 identity 的 `id / created_at / relationship_started_at / relationship_anchor_source / relationship_anchor_evidence`**。
- **原本没有 identity 时 → 返回 409**(让用户先完成 onboarding,别硬造一张)。
- **跳过**:memory 写入、relationship anchor 重算、persona/voice 重建。
- ⚠️ **这是破坏性覆盖,且 dream 不兜底**(见 §1.7):新角色卡没写的旧身份内容**永久丢失**。这是**有意为之**的产品决策(用户主动传新卡=重定义 TA),不是 bug。

### 1.6 约束落地
- 不新增/修改任何派生函数(`_derive_identity_with_provider` / `fact_map/fact_write/voice/persona` prompt 全不动)。
- 只改:路由读 mode → 决定调用哪几步 + 传全量卡 vs core。

### 1.7 Dream 兜底边界(定性,别误用)
最新 test 的 dream(`backend/memory/dream_prompt_v1.py` + `dream_scheduler.py`)**只整理 memory 卡**(`merge`/`thicken`/`supersede`,软替换不硬删,夜间/攒量触发)。
- ✅ **memory 靠 dream 兜底成立**:add_memory 不去重 → dream 夜里合并重复。
- ❌ **identity 不在 dream 范围**:dream **不碰身份卡**(它"不形成对 TA 的理解")。所以 update_identity 的覆盖是**不可恢复**的 —— 别指望 dream 修回被覆盖掉的身份内容。

---

## Part 2:iOS — 三入口传对 mode

iOS 三个 sheet 已存在(origin/main:GardenMaterialSheet / IdentityMaterialSheet / ChatEmptyStateView)。只需在 `uploadGenesisPlaintext`(及 history_import 兜底)请求里带上 `mode`:

- ChatEmptyStateView(onboarding)→ `mode: "onboarding"`
- GardenMaterialSheet → `mode: "add_memory"`
- IdentityMaterialSheet → `mode: "update_identity"`

(即便 iOS 先不发版,后端 1.1 的 client_job_id 前缀兜底也能对上;但既然要一起做,显式传 mode 更干净。)

---

## Part 3:报错硬化(onboarding 最后一步)

### 3.1 iOS
- **洞1**:`pollGenesisImport` 240 次超时那条,不要静默 `isImportingHistory=false`;改成设一个明确的超时错误(如 "onboarding 超时,请重试"),让 UI 走失败态而不是"转着转着没了"。
- **洞2**:失败分支用 `job.error`(真实原因)而不是写死的 "genesis distillation failed"(对齐 `pollHistoryImport` 的 `job.error ?? "…"`)。

### 3.2 后端
- 确认 **stale-job reaping / heartbeat**(zhihao `6c5d5f8`)覆盖 v2 的前台/后台 job:worker 挂/超时的 job 要被标 `failed`(否则 iOS 只能靠 240 次超时兜底)。若没覆盖,补上。

### 3.3 provider 402 别被"假身份"吞掉(Codex R5)
`foreground_identity.py` 调的 deriver 会把 provider 错误(如 402 欠费)**变成 fallback 身份 + warning**,调用方现在**不处理 warning** → 欠费被当成"身份生成成功"咽下去(这很可能就是"欠费一直 loading / 不知咋回事"的来源)。
- 修:provider 硬错误(402/额度/鉴权)要**一路冒到顶,把 job 标 `failed`**,**不能写一张 generic 身份当成功**。
- 前台的"deriver 失败 → fallback"只该用于"模型能连但没抽出身份"这类软情况,**不该盖住 provider 层的硬失败**。

---

## 验收标准

用"给人物信息 → 跑真实流程 → 看数据齐/准"验收(可复用 `tests/test_genesis_distill_acceptance.py` / `tools/genesis_e2e.py`,真 provider key、真 test 部署):

1. **B(身份吃满卡)**:同一份大历史 fixture,对比修前/修后,identity 的 name/维度/category/自我介绍**更贴 ground-truth**、漏抽更少;greeting 更贴人设。
2. **三入口写范围**:
   - add_memory:传一段新记忆 → 花园多了记忆,**identity 不变、相处天数不变**。
   - update_identity:传新角色卡 → identity 更新,**没新增记忆、相处天数不变**。
   - onboarding:全套齐(identity + 记忆 + anchor + greeting + persona/voice)。
3. **报错**:人为制造失败(如无 provider 额度)+ 制造卡死(超时)→ UI **明确报错、不无限 loading**,且失败时能看到真实原因。

### 6 条硬断言(Codex,必须写成测试)
1. **同内容不同 mode 不复用**:同一 input_hash,`add_memory` 后再 `update_identity` → 是**两个 job 都执行**,不被复用跳过。
2. **add_memory 后 identity 不变**(byte 级/字段级比对)。
3. **add_memory 后相处天数不变**。
4. **update_identity 后 memory 条数不变**(没偷写记忆)。
5. **update_identity 后相处天数 / relationship_started_at 不变**;无 identity 时返回 **409**。
6. **onboarding 的 full memory 条数明显 > core**(证明前台真写了全量,不是还只写 5 条);且 **provider 402 时 UI 显示真实错误、job=failed**(不是假身份 + 转圈)。

---

## 风险 / 注意

- **兼容**:mode 默认 onboarding + client_job_id 前缀兜底 → 老 app 不发版也不崩。
- **加密**:写 identity/memory 都走信封;本次是"写不写/写几条"的编排改动,不改信封结构。若 Codex 发现需动信封 → 触发真实 e2e 铁律。
- **相处天数**:add_memory / update_identity 两条路**绝对不能**调用重算 anchor 的逻辑(前面修过三层的坑,别回归)。
- **快进不回退**:B 改动后前台仍不等 voice/persona;确认 chat_ready 时机不变。

---

## 执行流程(用户指定)

1. CC 写方案(本文件)。
2. **Codex review 本方案** → 独立判断、指出问题/更优解 → 决定最终实现。
3. Codex 在上述分支执行。
4. **CC review Codex 的实现**。
