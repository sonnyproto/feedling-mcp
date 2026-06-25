# IO Memory v1 · 实施计划(基于 Codex 通读 origin/test)

> 2026-06-25 · 作者:CC · 基线:`origin/test` @ `574b1ed`(就地演进,不建新表)
> 依据:结构定稿 `IO-memory-v1结构定稿-bucket-thread.md`、合同 `IO-memory-read-write-contract.md`、Codex 的 test 只读结构地图。
> 取代:`IO-memory-v1实施方案-给codex-纯净分支.md` 里"从 main 切 + 删死代码"那套(基线/方法论已变,见 §0)。

---

## 0. 核心判断(用户拍板:Garden 解耦 → 走干净 v1)

**用户决定**:这是项目大重构;**Garden 由用户把控、一切以 memory 为准、iOS Garden 先隐藏、memory 完后再重新设计展示**。→ **不再为 Garden 保留 legacy/双写,走干净 v1(删 legacy 字段)。**

**test 现状**:memory = "旧 Garden envelope + M1 readside + M2 action" 混合态;`type/title/description/her_quote/TAB_FOR_TYPE/双写` 耦合 Garden、import、identity floor、verify、enclave、proactive(Codex §4/§8)。存储 `memory_moments(user_id, moment_id, occurred_at, doc JSONB)`,每卡一行、就地加键、不建表(Codex §9)。

**方法论 = 干净 v1 + readers 全跟到 v1**(不是 additive 双写):
```
① schema 改 v1(bucket/threads/content/importance/pulse/status/source/occurred_at/last_referenced_at),删 legacy 字段、不双写
② 我们的 old-field readers 全更新到 v1:import(产 v1 卡)、identity floor(tab计数→卡计数)、verify(删/改)、enclave item builder(读 content/bucket/threads)
③ 旧卡:adapter 读出来映射成 v1(读侧)+(可选)批量迁移
④ Garden:iOS 先隐藏(用户)→ memory 完 → 重新设计
⑤ ⚠️ proactive(别人 144 提交里的代码)读旧卡 shape → 边界留薄 compat shim,别硬改它(避免和他们开发冲突);或与 owner 协调后更新
⑥ 别人业务代码(perception/agent/locality)+ hosted_context 主函数:仍不碰(只抽 memory adapter)
```

**基线/分支**:从 current `origin/test` 切 feature 分支,就地改已合进 test 的 memory 代码;worktree 那批(recall/sensitive-gate/conformance)当参考重建。

---

## 1. 字段演进(干净 v1,删 legacy 不双写,P1)

`memory/actions.py:_memory_inner_from_action`(54-80)+ envelope(131-183)+ `service.py`:
- **加 v1 字段**(写进 doc):`bucket`(string,单选)、`threads`(string[])、`content`(MD)、`pulse`(0-1)、`last_referenced_at`。
- **复用**:`importance`(0-1 已有)、`status`、`source/source_type`、`occurred_at`、`summary`(已有)。
- **删 legacy**(写侧不再产):`type`、`title`、`description`、`her_quote`、`linked_dimension`、`anchor_memory_ids`(Garden 已解耦、readers 跟到 v1,见 §4)。
- **adapter `to_v1_card(doc)`**(读侧统一过 **存量旧卡**):`title/description/her_quote → content`;`type → 默认 bucket`(moment/quote→"我们的关系"、fact/event→"未分类");`linked_dimension/anchor → threads`(能映就映,否则空);`importance` 复用;`pulse` 默认 0.3;`last_referenced_at` 缺省=occurred_at。(adapter 只为读懂旧卡;新写直接 v1。)

> 不动 `db.py` 表结构(JSONB 直接加键)。`memory_replace_all/upsert`(db.py:860-929)照用。

---

## 2. 读路径 v1(P2)

**`memory_readside_core.py` + `memory/routes.py` + enclave**:
- **bucket/thread filter**:`memory_index_core`(198-228)/`readside_candidates`(142-164)加 `bucket=` / `thread=` 过滤(thread = `where X in threads`);`follow_thread(X)` = `index(thread=X)`,**非新端点**。
- **两种挑法(别混)**:① **agent 查(相关性)** = `相关性 × importance × (1-decay)`,**pulse 不进**;② **气氛灯 ambient(情感底色)** = `importance × pulse × recency`,**无 query**。`decay` 读时 = `clamp((now-last_referenced_at)/half_life,0,1)`(half_life:30/90d,importance≥0.8 ×2)。改 `memory_score`(88-92)/候选排序(142-164)。
- **气氛灯能力(runtime push 用)**:`index` 支持**无 query + 按 `importance×pulse×recency` 排序 + 取 top-N**(runtime 每会话开始拿几条底色;非 agent 查)。
- **limit 可配,砍 hardcode 50**:已有 `FEEDLING_MEMORY_READSIDE_LIMIT`(0=full,Codex 105-139)——用它、默认放宽。
- **index 不含 content / fetch 才含**:enclave index item(enclave_app.py:1019-1029)加 `bucket/threads` 输出;fetch item(1032-1045)回 content。
- **fetch sensitive gate**:enclave `v1_memory_fetch`(1128-1149)现在没挡敏感——照 index(1119-1120)补过滤 + `blocked_sensitive_ids`(worktree 参考)。
- **不做 `/v1/memory/recall` / preflight**:默认 agent 会 call tool,该查自己 `search→fetch`;读侧 = `index/fetch` + 气氛灯 push。(recall 以后或作省 token 捷径,非 v1。)
- **`GET /v1/memory/buckets|threads` 新增**:聚合现有卡,给写入提示做 resolve-before-create。
- selector(`memory_index_selector.py`)复用,sensitive 默认规则保留(18-31)。

> **不碰**:proactive 的 `memory.index/memory.fetch`(点号命名,和 hosted 的 `memory_index/fetch` 两套并存,Codex §8);hosted_context 主函数(只抽 memory adapter,见 §4)。

---

## 3. 写路径 v1(P3)

**`memory/actions.py` + 规范化**:
- **actions**:`memory.add`(bucket/threads/importance/pulse/content)/`memory.supersede`(soft,复用 `_memory_supersede_action` 390-473,改产 v1 字段)/`memory.delete`(476-494)。`create=add` 别名;`patch→supersede`(改记忆=退旧立新);`retype→400`;`add_correction→add`。
- **`_memory_inner_from_action` 只产 v1 字段**(54-80 改写),**不再双写 legacy**(Garden 已解耦)。
- **resolve-before-create**:写入提示注入 `GET buckets/threads` 的现有词表,逼模型复用。
- **规范化等价**:route A `_normalize_v2_action_type` / route B `hosted_runtime.py:coerce_runtime_action`(348-467)对 `add/supersede/delete` 产同一 executor action;补 conformance 测试(worktree 参考)。

> **不碰**:`coerce_runtime_action` 的 perception 部分;capture worker(turn.py:891-940)的 perception 输入。

---

## 3.5 提示词工程(两层,Seven 拍板 / hx 出初版 / eval 调)

记忆的提示词**不可能空着上线,v1 必出初版**;但**最终由 Seven 确定**(hx 先给初始版,合并到 test 时作为一个**清晰可替换的提交/文档**交给 Seven 替换)。两层都要:

| 层 | 是什么 | 谁 |
|---|---|---|
| **写入指引** | agent 怎么判断该不该记、打 bucket/thread(resolve-before-create)/importance/pulse、content 三段 | hx 出初版 → **Seven 定** |
| **拼接/注入框法** | 取到的记忆**放进上下文时怎么包**:气氛灯=背景底色(别当话题回应)、查到的=自然织进别背诵别反复确认、每卡 `使用提示` 引导 | hx 出初版 → **Seven 定** |

- **注入框法是"自然 vs 出戏"的命门**,**只能 eval 驱动调**(真注入→看 AI 表现→改框法),纸上定不准。
- **交付方式**:写/读两套提示词文本**集中在一处**(便于 Seven 整段替换),随 v1 合并到 test 时单独标出。

---

## 4. 旧体系:现在删 + readers 跟到 v1(Garden 已解耦)

**A. 直接删**:
- `insight/reflection` 生成 + anchor 校验(actions 84-115、`/add` 284-314、`_validate_anchor_ids`/`_reflection_time_cap_ok`)。
- `retype` 端点(routes 342-411)+ action(actions 344-387)。
- `legacy 双写` + `title/description/her_quote/linked_dimension/TAB_FOR_TYPE`(写侧不再产)。

**B. readers 更新到 v1(不保留旧字段,跟着 memory 走)**:
- **enclave item builder**(enclave_app.py 1019-1045):index/fetch 改读/产 `summary/content/bucket/threads`,不再 `description/title/verbatim/her_quote`。
- **history import**(history_import 1492-1814):产 v1 卡(bucket/thread),不再映 `type/TAB`。
- **identity init floor**(identity/routes 56-125):`tab-floor 计数 → v1 卡计数`(或按 bucket 计)。
- **verify**(routes 430-543):删,或改成 v1 计数(不再 tab/floor)。

**C. ⚠️ proactive(别人 144 提交里的代码,不硬改)**:
- proactive 有自己的 `_memory_index_item`(tool_executor_v2 406-415/512-520)读旧卡 `id/type/title`。schema 一变它会断。→ **在 proactive 读 memory 的边界留薄 compat shim(v1 卡 → 它要的旧 shape)**,它代码不动;或与 owner 协调后一起更新。**别在它正开发时硬改。**

**D. 旧 memory 注入编排(抽 adapter,不重写主函数)**:
- `hosted/context.py:_model_api_context_messages`(78-208):**只抽 memory 部分成 adapter**(build_companion_context 零件),不重写主函数(和 identity/screen/perception 耦合)。zhihao 新 runtime 用。
- `chat_routes.py` memory tool loop(168-271,和 perception 混)/`consumer` 胶水:**只抽/移除 memory 部分**,不碰 perception。

---

## 5. 耦合护栏(Codex 标的,别碰/先解耦)
1. **identity init 依赖 memory floor + earliest date**(identity/routes 56-125)→ 改 type/floor/verify 前先解耦。
2. **proactive `memory.index/fetch`(点号)≠ hosted `memory_index/fetch`** → 两套并存,**不改 proactive 命名**(tool_catalog_v2 72-93)。
3. **hosted_context 同装 memory/identity/screen/perception** → 只抽 memory adapter,不重写主函数。
4. **别人 144 提交(perception/agent/locality)** → 原样不动。

---

## 5.5 迁移(老用户数据)—— 翻译官 + 后台慢升级口

老卡存在 `memory_moments.doc`(旧 shape:`title/description/her_quote/type…`),v1 期望 `bucket/threads/content/importance/pulse`。**上线不做阻塞批迁移。**

**A. 上线:读时 adapter(翻译官,不阻塞)**
- 读侧统一过 `to_v1_card(doc)`:老卡**当场映射**成 v1(`title/description/her_quote→content`;`type→默认 bucket "未分类"`;`threads 空`;`importance` 复用;`pulse` 默认;`last_referenced_at=occurred_at`)。
- **DB 里老卡不动,新写直接 v1**。上线即可读、零阻塞、无批迁移风险。
- 代价:老卡是"**降级 v1**"(默认桶、空线),仍能召回(summary/importance/recency),但**没 bucket 导航/follow_thread,直到回填**。

**B. 迁移口:后台回填升级(慢跑)**
- **`POST /v1/memory/migrate`(per-user,幂等)** 或离线 job:
  读老卡 → LLM 据旧 `description/her_quote` + 当时上下文,**重新打 `bucket/threads/content/importance/pulse`** → 写回 v1 → 标 `migrated`(已 v1 的跳过)。
- 触发:按需 / 用户首次 v1 会话懒迁 / 离线批量,皆可。
- 上线后慢慢/离线跑;**早期用户记忆量小,压力低**。

**原则**:**上线靠 adapter 兜底(老卡立刻能读、糙点),真升级靠回填口慢补**;谁都不耽误。

---

## 6. 阶段顺序
```
P1 schema 改 v1(删 legacy 字段、不双写)+ adapter(读懂存量旧卡)
P2 读 v1:index/fetch bucket/thread filter + 两种挑法(agent相关性 / 气氛灯 importance×pulse×recency)+ limit可配
        + enclave builder 改读 content/bucket/threads + fetch sensitive gate + GET buckets/threads(无 recall/preflight)
P3 写 v1:actions add/supersede/delete + bucket/thread/importance/pulse(不双写)+ resolve-before-create + 规范化等价
P3.5 提示词初版(写入指引 + 注入框法)—— hx 出初版、集中一处、Seven 后替;eval 调注入框法
P4 readers 跟到 v1:import 产 v1 卡 / identity floor → 卡计数 / verify 删改 / proactive 边界留 shim
P5 测试:conformance + sensitive-gate + adapter旧卡 + readers 没断 + 真机 smoke
P6 删旧(**必在 readers + 测试之后**):insight/reflection 生成+anchor、retype、TAB_FOR_TYPE;抽 hosted_context memory adapter
P7 iOS(你):隐藏 Garden tab(随 v1 上线)→ memory 稳 → 重新设计 Garden 展示
```

---

## 7. 测试清单(必加)
```
add 写 v1 字段(bucket/threads/importance/pulse/content)
adapter:旧 M2 卡读出来是 v1 shape(title/description/her_quote→content;type→默认bucket)
index 只返回 id/bucket/threads/summary/importance/source/occurred_at(+派生decay),不含 content;fetch 才含
index(bucket=X)/index(thread=Y) filter;follow_thread 跨 bucket 捞同线卡
limit 可配(0=全,不hardcode 50);status≠active 不返回
supersede soft:旧卡 status=superseded、链新卡、不硬删、继承 bucket/threads
fetch sensitive gate:敏感 id 直取被拦 + blocked_sensitive_ids
ambient 选择函数=最近+高importance(不依赖query);recent floor 用 created/referenced;去重;pulse 不进排序
last_referenced_at 只在 fetch/注入后更新(扫目录不更新)
规范化等价:route A/B 对 add/supersede/delete 产同一 executor(conformance)
enclave/import/identity-floor 改读 v1 后没断(不双写;旧卡靠 adapter 读)
GET buckets/threads 返回现有词表(resolve-before-create)
create→add / patch→supersede / retype→400
list/get/delete 保持可用
```

---

## 8. 给 Seven / zhihao
- **Seven 待确认/拍板**:① bucket 平铺(vs 三层,非阻塞)② pulse 不进 agent 排序(非阻塞)③ **提示词(写入指引 + 注入框法)= Seven 定**:hx 出初版、集中一处、随 v1 合 test 时单独标出供 Seven 整段替换。(Garden 时机已拍:iOS 先隐藏、后重做。)
- **🔴 鉴权 token 边界(hx × zhihao 必对)**:agent 调工具带 **runtime token**(per-user),但记忆端点现认 **`X-API-Key`**。谁把 runtime token → 用户?gateway 翻译,还是端点直认 runtime token?**开工前对齐。**
- **iOS(用户)**:随 v1 上线**隐藏 Garden tab**;memory 稳后重新设计 Garden 展示。
- **proactive owner(协调一次)**:memory 卡 schema 改 v1 → proactive 边界要么用 hx 提供的 compat shim、要么一起更新到 v1。
- **zhihao 交付**:工具契约(memory_search/fetch/write + buckets/threads + identity_get;**无 recall**)+ **hosted_context 抽出的 memory adapter**(build_companion_context 零件)+ 本计划。
- **鉴权 = 选 A(Codex/CC 定)**:tool gateway **服务端**把 runtime token 翻译成后端可识别的用户身份/短期凭证;memory 端点**先不直接认 runtime token**(后端零改动、v1 快)。**不把用户长期 API key 暴露给 agent**。B(端点直认 token)= v2 统一 auth 再做。

**一句话**:**基线 current test、就地演进;Garden 解耦 → 走干净 v1(删 legacy、不双写),我们的 readers(import/identity floor/verify/enclave)全跟到 v1;proactive 留 shim 别硬改;iOS 先隐藏 Garden 后重做;perception/别人代码不碰。**
