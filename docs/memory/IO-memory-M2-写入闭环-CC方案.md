# IO Memory M2 最小写入闭环 · CC 方案(给 Codex)

> 2026-06-21 · 作者:Claude(CC) · 回应:`给 CC:IO Memory M2 写入闭环方案请求`
> 已读真实代码:`backend/memory/{actions,service,routes}.py`、`backend/db.py`、`backend/hosted/turn.py`(capture)。文中代码引用都核对过。
> 一句话:**M2 不是"从零做写入"——写入早就有了(显式 actions + 聊天后 capture 流水线)。M2 是把它形式化成 `MemoryCard v1 + commit 契约(insert / supersede)`,并补上"软退场 + 被 M1 recall 正确使用"的闭环。而且 recall 侧已经 supersede-ready,supersede 大半的活已经免费了。**

---

## 1. 当前写入现状(Q1,grounded)

### 1.1 存储
- 表 `memory_moments`,**行存**:`(user_id, moment_id, occurred_at, doc JSONB)`(`db.py:846+`)。
- `db.memory_load` 读、`db.memory_upsert`(行级 upsert)、`db.memory_replace_all`(差量整组重写,事务+只改动过的行)、`db.memory_delete`(**硬删一行**)。
- 每条 `doc` = 一个 envelope:**明文外壳 + `body_ct` 密文内层**。

### 1.2 两条写入入口
**A. 显式记忆动作(`memory/actions.py`)** —— 当前的"commit 执行器":
- `memory.add` / `memory.add_correction` → `_memory_add_action`
- `memory.content_patch`(改内容)/ `memory.retype`(改类型)/ `memory.delete`(**硬删**)
- 统一走 `_execute_memory_actions`(批量 ≤20),做:校验 → 调 enclave 加密(`core_envelope._build_shared_envelope_for_store`)→ 组装 record → `_save_moments`(= `memory_replace_all`)→ 写 change log。

**B. 聊天后 capture 流水线(`hosted/turn.py` + `_append_memory_capture_job`)** —— 当前的"propose":
- 一轮聊天后,后台 job 提取候选、检测旧卡、计划/创建新卡、归档旧卡。
- capture job 字段已含 `candidates_extracted / memories_created / old_cards_detected / old_cards_archived / new_cards_planned / new_cards_created` —— **写时 reconcile 雏形已存在**。
- 还有 onboarding(`setup_routes.py`)、history import(`history_import.py`)两个写入来源。

> 人话:**LLM 提取候选(propose),`_execute_memory_action` 确定性落库(commit),enclave 只做加密**——propose/commit 分离的雏形已经在了,M2 是把它正式化。

### 1.3 加密分工
写入经 `_build_shared_envelope_for_store` → **enclave 加密**,返回 `body_ct / nonce / K_user / K_enclave / enclave_pk_fpr`。**enclave 不跑 LLM,只做确定性加密**。(正好是你 Q3 的倾向。)

### 1.4 现有 doc 形状(`_memory_record_from_envelope`)
- **明文外壳**:`v, id, type, occurred_at, created_at, updated_at, source, visibility, owner_user_id, anchor_memory_ids` + 加密字段。
- **密文内层(body_ct)**:`title, description, her_quote, context, linked_dimension, quoted_in_chat`。

### 1.5 已有的"退场"与"recall 状态感知"(对 M2 极关键)
- **软退场已存在**:`is_archived / archived_at / archive_reason`(`service._memory_is_archived`),Garden 用它过滤(`_active_memory_moments`)。
- **recall 已 status-aware**(`routes.py:_memory_readside_available`):已读 `moment.get("status")`,**默认排除 `superseded` / `archived` / `deleted`**,支持 `include_superseded / include_archived`。
- ⚠️ **二义性**:recall 读 `status`,但写入侧目前只写 `is_archived`、**从不写 `status`**。M2 要统一(见 §5)。

### 1.6 M2 要补的 gap
1. 写入侧没有 `status` 字段(active/superseded/...)。
2. 没有 `supersedes / superseded_by` 链接。
3. `memory.delete` 是**硬删**——与"永不硬删"冲突,supersede 不能走它。
4. 基础 `memory.add` 不写 `salience / importance`(recall 目前给默认值)。

### 1.7 写入是怎么触发的(三道闸,**批量滞后**)
现状写入**不是实时的**(`hosted/turn.py:_model_api_maybe_run_memory_capture`):
- **闸0 · agent 抢先**:本轮 agent 自己写了 `memory.` 动作 → capture 跳过(不重复)。
- **闸1 · 确定性周期**:`turn % MODEL_API_CAPTURE_TURN_INTERVAL(默认 24)== 0` 才触发——**默认每 24 轮才尝试一次,不是每轮、也不是 LLM 判断要不要触发**。
- **闸2 · LLM 内容判断**:触发那轮,LLM 提取候选,有值得写的才写(`actions=0` 就不写)。
- 另有每 80 轮(`MODEL_API_CONSOLIDATE_TURN_INTERVAL`)的 recap/repair 清理。

⚠️ **关键含义**:用户说"其实是橘猫"如果不在第 24 轮,这条要**攒到下一个周期才被写**。所以 **M2 要的"纠正后即时 insert/supersede",靠不了 24 轮的 capture,必须靠 agent 当场写**(见 §3.5)。

---

## 2. M2 目标 / 不做范围

**目标(最小闭环):**
- 定义 **MemoryCard v1** 写入 shape(在现有 doc 上做**超集**,不另起炉灶)。
- **insert**:新事实按 v1 落卡,能被 M1 recall 找到。
- **supersede**:旧事实被纠正时,旧卡软退场(`status=superseded` + 链接),新卡生效;recall 默认不再返回旧卡。
- 旧 Garden 不坏、旧数据不迁移、有最小测试。

**不做(本轮):** merge、contradict 复杂仲裁、decay 精细参数、embedding、完整 eval 平台、历史迁移、Garden UI 重做、route A/B 全量收口。(全部采纳你 §3。)

---

## 3. MemoryCard v1 最小数据结构(Q2)

**设计原则:v1 = 现有 doc 的超集**——加字段、不改旧字段名、不另起表。老读者(Garden 靠 `type`)忽略新字段,新读者(recall 靠 `status/salience`)用新字段。

| 字段 | 放哪 | 说明 |
|---|---|---|
| `id` | 明文 | 现有 |
| `card_v`(= 1) | 明文 | 卡版本标记,区分老 MemoryMoment |
| `type` | 明文 | **保留**(Garden tab 映射靠它,`TAB_FOR_TYPE`)|
| `status` | **明文** | `active / superseded / archived / deleted`(recall 预筛靠它)|
| `salience` | **明文** | critical/high/medium/low(recall 排序)|
| `importance` | **明文** | 0–1(recall 排序)|
| `source_type` | 明文 | = 现有 `source` 对齐 |
| `created_at / updated_at / occurred_at` | 明文 | 现有 |
| `supersedes` / `superseded_by` | **明文** | 新增链接(supersede 用)|
| `is_sensitive` / `sensitivity_class` | 明文(粗) | 仅粗粒度;具体范围在密文 |
| `owner_user_id / visibility` + 加密字段 | 明文 | 现有 |
| `summary` | **密文** | 主摘要(recall index/fetch 已优先读 summary)|
| `verbatim`(= 现 `her_quote`)| 密文 | 原话 |
| `context / follow_up` | 密文 | 现有 + follow_up |
| `bucket 真实名 / sensitive_scope 具体值` | 密文 | |
| `title / description / her_quote`(legacy)| 密文 | 兼容老卡;**adapter 优先 `summary`,无则 fallback `description`→`title`**;`her_quote`→`verbatim` |

**明文/密文边界**沿用 M1 readside 既定口径:服务端能看"重不重要/什么状态/是否敏感",**看不到具体写了什么**。

**字段命名定死(Codex 已确认,Step 1 照此实现):**
- **明文**:`card_v` / `status` / `salience` / `importance` / `supersedes` / `superseded_by` / `is_sensitive`(或 `sensitivity_class`)。
- **密文**:`summary` / `verbatim` / `context` / `follow_up`。
- **legacy 兼容(读)**:后端 readside 取值优先级 `summary` → `description` → `title`;`her_quote` → `verbatim`。不新增同义字段、不改旧字段名。

**⚠️ 写入必须双写 legacy 内层字段(iOS Garden 兼容,Codex 补)**:
iOS Garden 解密后**直接读 `title / description / her_quote`**(它不走后端那套 `summary→description→title` adapter)。所以新卡密文里**不能只写 `summary/verbatim`,必须同步回写 legacy**:
- `title` ← `summary` 的短标题(截断/首句,≤180)
- `description` ← `summary`(或正文摘要)
- `her_quote` ← `verbatim`

即新卡密文 = **新字段(summary/verbatim/follow_up)+ legacy 字段(title/description/her_quote)同时写**。
> **一致性要求**:任何改内容的写入(insert / content_patch / supersede 的新卡)都要**同步更新两组**,否则后端 readside(读 summary)和 Garden(读 description/title)会显示不一致。

---

## 3.5 写入三层模型 + 决策 A(hx 已拍板)

现状写入是**周期攒批 + LLM 挑值得写的**(§1.7),滞后。M2 把"即时写"这条加强成 **agent 当场写**,周期任务全部降成兜底——这跟召回侧(M1.5)完全对称:

| | 召回侧(M1/M1.5)| 写入侧(M2)|
|---|---|---|
| **即时、精确** | agent 当场召回(index/fetch)| **agent 当场写 insert/supersede** ← M2 要加强 |
| **周期兜底** | 服务端 selector 兜底 | 每 24 轮 capture 兜底补抓 |
| **批量清理** | —— | 每 80 轮 repair/recap 清理 |

**决策 A(hx 已定):**
- **live 纠正/写入走 agent 当场写**(精确、即时);
- **周期 capture(24 轮)+ repair/recap(80 轮)降级成"批量兜底清理"**——只兜 agent 没当场写的、扫历史遗留乱卡,**不再是主路径**;
- repair 里那段**服务端相似度猜旧卡**的逻辑(`history_import._merge_import_candidates`)**只留在兜底路径**,live supersede **不走相似度猜,改由 agent 判断目标**(见 §5)。

> 一句话:**live 走 agent(即时精确),周期的都退成兜底。** 写入侧和召回侧同构。

---

## 4. insert 流程(Q3)

**propose / commit 分离(正式化现有雏形):**
- **propose(LLM,路由依赖)**:capture 流水线 / agent 产出候选(summary/verbatim/type/salience 等)。LLM **只提议**。
- **commit(确定性,无 LLM)**:`_execute_memory_action` 走一个正式的 **Insert commit**:
  1. 校验字段(沿用现有 `_memory_validate_write`)。
  2. 组装 v1 inner → `_build_shared_envelope_for_store` 加密(enclave)。
  3. 设明文 `status=active`、`salience`、`importance`、`source_type`、`card_v`。
  4. `moments.append(card)` → `_save_moments`(复用现有 plumbing)。
  5. 写 change log(`action=insert`)。
- **enclave**:只加密,不决策、不跑 LLM。

**落库与兼容**:新卡是 `memory_moments` 里一条普通 doc → `/v1/memory/list` 和 Garden 自动可见(靠 `type`);M1 recall 自动可召回(靠 `status=active`)。**基本是现有 `memory.add` + 补 v1 明文字段**。

---

## 5. supersede 流程(Q4)—— 大半已经免费

### 5.0 现状 vs 愿景流程图

**⚠️ 关键:supersede 第一步是"先查一遍 memory"——就是 M1 的召回(index/fetch),复用,不重做。** "取代哪张"是查出来 + agent 判断,不是凭空知道。

**现状(纠正一个事实)——只加不取代 + 滞后批量清理:**
```
用户:"其实武松是橘猫"
  │
  ├─ 不在第 24 轮 → 先不写(攒着)               ← §1.7 周期闸
  │
  └─ 到第 24 轮:LLM 提取 → memory.add 写【橘猫】  ← 只新增,不碰旧卡
        │
        ▼
   库里: 【狸花猫 active】+【橘猫 active】       ← ⚠️ 两张都在、都会被召回 → 易答错
        │
   ……(旧卡被标记 noisy + 到第 80 轮 repair)……
        ▼
   重读历史 → 相似度聚类 → 写新卡 → 把 noisy 旧卡 archive
        ▼
   狸花猫最终 is_archived(批量、滞后、靠相似度,不精确)
```

**愿景(M2)——agent 当场查 + 当场取代:**
```
用户:"其实武松是橘猫"
  │
  ▼
agent 召回相关旧卡(查 memory,数据源见 §5.5 #2)  ← ★先查 memory 才知道取代哪张
  │
  ▼
agent 判断:这条纠正了 mem_123【狸花猫】           ← 判断在 agent(propose)
  │
  ▼
agent 提议:insert【橘猫】 + supersede mem_123
  │
  ▼
[服务端确定性 commit](无 LLM、不猜相似度)
  • 写新卡【橘猫】 supersedes=[mem_123]
  • 旧卡 mem_123 → status=superseded, superseded_by=新id, is_archived(给花园)
  • 两张一次原子落库,旧卡不删
  │
  ▼
问"武松什么猫?" → 只返回【橘猫】(退场卡自动跳过,M1 已支持)
debug include_superseded → 仍可见狸花猫(没删)
```

| | 现状 | 愿景(M2)|
|---|---|---|
| 纠正瞬间 | 只加新卡,旧卡还在、还会被召回 | 加新卡 + 旧卡当场退场 |
| 谁决定取代哪张 | 当场没人定;靠周期 repair 相似度**猜** | agent 当场判断(**先查 memory** 再判断)|
| 退场方式 | 批量 archive,滞后 + 不精确 | 当场 supersede,精确 + 有链接 |
| 一致性 | 最终一致(短期两张并存)| 即时一致 |

### 5.1 Supersede commit

**核心:recall 侧已经排除 `superseded`(§1.5),所以 supersede 主要是写入侧两步,且全程软操作。**

**Supersede commit(确定性):**
1. 校验 `old_id` 存在且属本人。
2. **写新卡**(走 Insert),明文加 `supersedes=[old_id]`。
3. **旧卡软退场**:`status="superseded"`、`superseded_by=new_id`、`updated_at=now`——**绝不硬删**(不要走 `memory.delete`)。
4. 旧卡 + 新卡在**同一次 `_save_moments`、同一把 `store.memory_lock`** 下落库(原子)。
5. 写 change log(`action=supersede, supersedes=old_id`)。

**recall 行为**:`_memory_readside_available` 默认 `include_superseded=False` → 旧卡不再被召回 ✅(零改动)。debug 时 `include_superseded=true` 可看。

**Garden 兼容(关键决策)**:Garden 靠 `is_archived` 过滤,不认 `status`。两个选项:
- **(推荐,最小)** supersede 时**同时设 `is_archived=true` + `archive_reason="superseded_by:<new_id>"`** → Garden 不显示旧卡,**零 Garden 改动**。⚠️ 代价:`is_archived` 被复用承载"supersede"语义,未来 Garden 若要区分"用户手动归档 vs 被取代",看 `archive_reason` 即可。
- (备选)旧卡留在 Garden 加"已更新"标记——需要 Garden 改动,本轮不做。

> 你的倾向(永不硬删、recall 默认不返回 superseded、可显式 include)**完全落地**,且大部分逻辑 recall 侧已具备。

### 5.5 两个执行级细节(Codex review 补)

**重要事实(已核对代码):supersede 不是前台聊天 agent 做的,是已有的「background 执行控制器」做的**(`hosted/turn.py:_model_api_plan_state_actions` + `hosted_runtime.build_background_execution_messages`)——它本来就在产 `memory.create/patch/delete`。**M2 supersede 是插进这个现成控制器,不是新 loop。**

#### #1 · action 协议要完整贯通(4 处)
现状(已核对):prompt 只支持 `memory.create/patch/delete`;`coerce_runtime_action` 只映射到 `memory.add / content_patch / delete`;executor 不支持 `memory.supersede`。M2 要**新增并贯通**:
1. **prompt**(`hosted_runtime.py:build_background_execution_messages`):"Supported action types" 与 JSON shape 里**加 `memory.supersede`**;说明"用户纠正旧事实时用 supersede,`target.memory_id` 指被取代的旧卡"。
2. **`coerce_runtime_action`**:新增 `memory.supersede` 分支——`old_id` 取 `target.memory_id`(或 `candidate_ids[0]`,后者 `requires_confirmation=true`);新卡内容取 `payload.memory`;产出 `executor_action={type:"memory.supersede", supersedes:old_id, memory:{新卡}, reason}`。
3. **`memory/actions.py`**:新增 `_memory_supersede_action` 并接进 `_execute_memory_action`;**绝不复用 `memory.delete`(硬删)**;走 §5.1 的软退场两步。
4. **tests**:模拟 agent 输出 `memory.supersede` → coerce → executor 真正执行,断言新卡 `supersedes=[old]`、旧卡 `status=superseded/superseded_by/is_archived`、未硬删。

#### #2 · agent 怎么拿到 old_id(数据源,Codex 提的正确性问题)
**已核对定论**:background 控制器的候选来自 `hosted/turn.py:_model_api_state_memory_candidates` → **旧关键词路 `memory_relevance_details`**(解密**全部 active** → 关键词打分 → top 12 / score≥0.35),**不是 M1 readside index/fetch,两者不等价**。
- **M2 决定:supersede 的 old_id 沿用这条 background 候选路**。原因:它**解密全部 active、无 M1 readside 的 top-50 盲区**,对"找到该退场的旧卡"覆盖**反而更好**;且 supersede 本就在 background 控制器里,改动最小。
- ⚠️ **明确记下两条 memory 视图的分叉**:前台召回 = M1 index/fetch;background supersede 目标 = 关键词候选。**本轮不统一**(post-M2 收敛),但不能假装它是 M1 readside。
- **要求**:候选对象必须带 `id`(现有已带),供 agent 在 `target.memory_id` 回填;**验收测试必须断言**"猫纠正"case 里旧卡确实出现在候选集中,supersede 才能命中它。
- 局限:关键词候选对 paraphrase 纠正可能漏(漏了就退回周期兜底,§3.5 决策 A),与召回侧弱模型回退同构。

---

## 6. backend / enclave 分工

| 步骤 | 谁做 |
|---|---|
| 提取候选 / 判断要不要 supersede 哪条(propose)| LLM(capture / agent)|
| 字段校验、状态迁移、链接、落库、change log(commit)| **backend 确定性执行**(`memory/actions.py`)|
| body_ct 加解密 | **enclave**(无 LLM)|
| recall 预筛/排除 superseded | backend + enclave(M1 已就绪)|

**硬约束**:enclave 不跑 LLM;commit 不含 LLM 判断;supersede 永不硬删;旧卡/新卡一次原子落库。

---

## 7. "不拟合旧模块"怎么落地(Q5)

把 Seven 的意思**精确化**,避免走偏:

- ✅ **新写入按 MemoryCard v1 主线**:status/salience/importance/supersede 链接是一等字段,新代码以"卡 + commit"为心智。
- ✅ **复用通用存储 plumbing(`memory_moments` / envelope / `_save_moments`)不算"拟合旧模块"**——它是通用 JSONB row store,不是旧模块专属;而且 M1 recall 就读这张表,**另起新表会直接切断 recall**,反而是错的。
- ✅ **旧 MemoryMoment 通过 adapter 读成卡**:`_memory_readside_status` 缺省把无 `status` 的老卡当 `active`、readside 已把 `description/title→summary`、`her_quote→verbatim`——**adapter 已部分存在**,补全即可。
- ✅ **Garden 是展示兼容层,不定义架构**:lifecycle 由 `status` 定义;Garden 继续读 `is_archived`(§5 用兼容写法喂它)。
- ❌ **不为旧字段名牺牲新结构**(summary 是新主名,老 title/description 走 adapter)。
- ❌ **不迁移历史数据**,但新写入尽量是新 shape(超集 doc)。

> 一句话:**新卡是"老 doc 的超集 + 一个 adapter",不是新表也不是旧补丁。**

---

## 8. 测试方案(Q6)

### 8.1 产品可感知 e2e(test 环境真账号)
**① insert→recall 闭环**
1. 说"我有只猫叫武松,是狸花猫" → 等写入。
2. 清空近期聊天 → 问"我有猫吗?"。
3. 期望:recall 出"有,叫武松";trace 见 memory 命中。

**② 冲突→supersede→recall**
1. 已有"武松是狸花猫"。
2. 说"我记错了,武松是橘猫"。
3. 期望:写新卡(橘猫)+ 旧卡 `status=superseded, superseded_by=新id`,旧卡 `is_archived=true`。
4. 再问"武松是什么猫?" → 答**橘猫**,不出现狸花猫。
5. 校验:`include_superseded=true` 时仍能看到旧卡(没硬删)。

**③ 敏感/亲密边界**
1. 用户表达一个亲密偏好/边界。
2. 系统记"边界 + 使用条件",**不记露骨细节**;`is_sensitive=true`。
3. 后续相关问题能 recall,但 **index 不暴露原话**(沿用 M1 隐私分层)。

### 8.2 单元测试(`tests/test_memory_m2_*.py`)
- insert 写出 v1 明文字段(status=active/salience/importance/card_v)。
- **写入同步 legacy 内层字段**:新卡密文里 `summary/verbatim` 与 `title/description/her_quote` 都写到,iOS Garden 解密后能正常显示(见 §3 写入双写)。
- insert 后 `memory_load` 能取到、recall 候选包含它。
- supersede:新卡 `supersedes=[old]`、旧卡 `status=superseded + superseded_by + is_archived`,**两卡一次原子落库**。
- supersede 后默认 recall **不含**旧卡;`include_superseded=true` 含。
- **永不硬删**:supersede 路径不调用 `db.memory_delete` / 不从 moments 移除旧卡。
- Garden 兼容:`/v1/memory/list` 仍返回(老卡正常;superseded 卡按 is_archived 隐藏)。
- 老 MemoryMoment(无 status)经 adapter 读为 active、可召回(不回归)。
- **协议贯通(§5.5#1)**:模拟 agent 输出 `memory.supersede` → `coerce_runtime_action` → `_execute_memory_action` 端到端真正执行(不只测 executor)。
- **old_id 可达(§5.5#2)**:"猫纠正"case 里旧卡确实出现在 background 候选集(`_model_api_state_memory_candidates`)中——否则 supersede 无目标可命中。
- adapter 取值优先级:`summary`→`description`→`title`、`her_quote`→`verbatim` 各有用例。

---

## 9. 风险点

| 风险 | 处理 |
|---|---|
| `is_archived` vs `status` 二义性 | M2 以 `status` 为准;supersede 同时写 `is_archived` 喂 Garden;`archive_reason` 区分来源 |
| `memory.delete` 硬删与"永不硬删"冲突 | supersede **绝不走 delete**;建议把硬删限定为"用户显式删除",或本轮标注不在 commit 契约内 |
| 自动 capture 与 agent/supersede 并发写 | 复用 `store.memory_lock` + `memory_replace_all` 事务;supersede 两卡同一次保存 |
| 重复写(capture 自动提 + agent 又写同一事实)| 本轮**不做 merge**,但 insert 至少不崩;dedupe/resolve-before-create 记为 M2+(先 log 预警)|
| LLM 选错 supersede 目标(误退好卡)| 软操作可逆(status 翻回)+ change log 留痕;commit 只校验 id 存在/属主,不替 LLM 判断 |
| 新卡缺 salience/importance | commit 设默认(如 medium / 0.5),不阻塞 |
| **弱模型不当场写 → live insert/supersede 不触发**(§3.5)| 决策 A:退回**周期 capture(24 轮)/ repair(80 轮)兜底**补抓/清理——短期可能两张并存,不丢数据。与召回侧弱模型回退同构 |

---

## 10. Codex 执行顺序(采纳你的 + grounding)

```text
Step 0  确认现状写入链路(本文 §1,已画清)——核对无误即可开工
Step 1  定义 MemoryCard v1 写入 shape(§3:在现有 envelope/inner 上做超集 + adapter 补全)
Step 2  insert:把 memory.add 正式化为 Insert commit,补 v1 明文字段(§4);
        由 agent 当场写触发(live),周期 capture 降为兜底(决策 A,§3.5)
Step 3  supersede:① 先贯通 action 协议 4 处(§5.5#1:prompt / coerce / executor / tests 加 memory.supersede);
        ② 新增 Supersede commit(写新卡 + 旧卡软退场 + 链接,原子,永不硬删,§5.1);
        old_id 来源 = background 控制器关键词候选(§5.5#2,非 M1 readside);commit 确定性执行不猜相似度
Step 4  接 M1 recall 验证闭环(§8.1 ①②);确认默认排除 superseded、include 可见
Step 5  补单测(§8.2)+ 敏感边界 case(§8.1 ③)
全程    不动写入加密(enclave)、不迁移、不重做 Garden、不改 MCP/route A;
        周期 capture(24)/repair(80)维持现状别动,仅在定位上降为兜底(决策 A)
```

协作沿用你 §6:CC 出方案 → Codex review → Codex 执行 → CC review → Codex 修 → hx test 真机验。

---

## 11. 一句话收口

> **M2 最小写入闭环不是"从零做写入"——写入(actions + capture)和 recall 的 supersede 感知都已存在。M2 = 把它形式化成 MemoryCard v1(老 doc 的超集 + adapter,不另起表)+ insert/supersede 两个确定性 commit(LLM 只 propose,enclave 只加密,旧卡软退场永不硬删)。recall 侧默认排除 superseded 已经免费,主要工作量在写入侧的两步 + Garden 用 is_archived 兼容。目标只证明三件事:新事实能入库、旧事实能退场、新旧状态能被 recall 正确使用。**
