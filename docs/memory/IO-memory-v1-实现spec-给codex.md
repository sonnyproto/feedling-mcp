# IO Memory v1 · 实现 Spec(给 Codex 改代码)

> 2026-06-25 · 作者:CC · **Codex 照此改代码,CC review**。
> 基线:从 `origin/test` 切分支。结构以 `IO-memory-v1结构定稿-bucket-thread.md`、计划 `IO-memory-v1实施计划-test基线.md`、合同 `IO-memory-read-write-contract.md` 为准。
> 原则:**干净 v1(删 legacy 字段、不双写)**;**读旧卡靠 adapter**;**删旧放最后(测试之后)**;**perception/proactive 核心、hosted_context 主函数、别人 144 提交:不碰**。行号 = Codex 之前给的 test 地图。

---

## 执行备注(Codex review 后补,开工前必读)
1. **adapter 分两层,不假设 backend 看得到密文 inner**:backend envelope adapter(明文层)+ enclave inner adapter(解密后)。见 P1。
2. **bucket/thread filter 在解密后、不漏卡地做**:不先按分数截窗口再过滤;带 bucket/thread 时候选放全或 enclave 内 filter+limit。见 schema ⚠️ / P2。
3. **本批文档要带进 feature 分支**:这 4 份真相 + 本 spec 现只在脏工作区,`origin/test` 里没有。Codex 建分支后把 `docs/memory/` 这批一并 commit,否则别人 checkout 分支看不到依据。

---

## 卡 schema(v1 目标,贯穿全程)
```jsonc
inner(密文 body_ct):{ summary, content(MD三段), bucket, threads[] }
envelope(明文):    { id, occurred_at, created_at, updated_at, source(chat|screen),
                      importance(0-1), pulse(0-1), status(active|superseded|archived),
                      last_referenced_at, body_ct/nonce/keys/visibility/owner_user_id/K_enclave }
```
- **bucket/threads 放 inner(密文)**(桶名/线索可能敏感);enclave 解密后在 index/fetch item 里产出。⚠️ **因为是密文,backend 不能预筛**:bucket/thread filter **必须在解密后做,且过滤前不能先按分数截断**(否则真属于该 thread 的卡排在窗口外被漏)。带 bucket/thread 查询时 → **候选窗口放全(limit=full)或在 enclave 内 filter+limit,以"不漏"为先**。
- **`decay` 不存**:读时 = `clamp((now-last_referenced_at)/half_life,0,1)`,half_life 30/90d、importance≥0.8 ×2。
- **删字段**:title/description/her_quote/verbatim/context/follow_up/linked_dimension/quoted_in_chat/type/card_v/salience/source_type/anchor_memory_ids/superseded? (supersede 改用 status+superseded_by,保留)。

---

## P1 · schema + adapter(只加/改写卡,不删旧逻辑)

**`backend/memory/actions.py`**
- `_memory_inner_from_action`(54-80):改产 v1 inner `{summary, content, bucket, threads}`;`content` 取 action 的 content(MD);**不再产** title/description/her_quote/verbatim/context/follow_up/linked_dimension/quoted_in_chat。
- envelope 组装(131-183):**加** `pulse`、`last_referenced_at`(=occurred_at 初值);`importance` 保留(0-1);`status` 保留;**bucket/threads 进 inner**;**不再写** type/card_v/salience/source_type/anchor_memory_ids。
- `_memory_record_from_envelope`:对应读出 v1 字段。

**adapter 必须分两层**(⚠️ Codex 指出:bucket/threads/旧 content 字段在**密文 inner**,backend service 看不到,不能在 backend 翻译):
- **backend envelope adapter**(`memory/service.py`):只处理**明文 envelope** 字段——`salience/importance→importance`、`pulse` 缺省 0.3、`last_referenced_at` 缺省=occurred_at、`status/source/occurred_at` 照搬、旧 `type` 仅用于明文层降级。**不碰 inner。**
- **enclave inner adapter**(`enclave_app.py`,**解密后**):处理**旧 inner → v1 inner**——`title/description/her_quote→content`(拼 MD)、`linked_dimension/anchor→threads`、`type→bucket`(moment/quote→"我们的关系"、fact/event→"未分类")。**这层在 enclave 解密后做。**
- 两层都**幂等**(已是 v1 原样返回)。`_load_moments`(30-35)出口过 envelope adapter;enclave index/fetch item builder 出口过 inner adapter。

**验收**:写一条 → doc 里是 v1 字段(无 legacy);读旧 M2 卡 → **经两层 adapter**出 v1 shape(明文层在 backend、inner 层在 enclave 解密后);`memory_moments` 表不动(db.py 887-929 照用)。

---

## P2 · 读侧 v1

**`backend/memory_readside_core.py`**
- `memory_score`(88-92)/候选排序(142-164):**两种挑法**——
  - **agent search 相关性**:`相关性 × importance × (1-decay)`(pulse 不进)。
  - **气氛灯 ambient**(无 query):`importance × pulse × recency`。
  - `decay` 读时从 `last_referenced_at` 算(见上)。
- `memory_index_core`(198-228):入参加 `bucket?`/`thread?`/`ambient?(bool)`;`ambient=true` 用气氛灯排序、无 query;否则 agent 排序。bucket/thread filter(thread = `X in threads`)**在解密后做**,且**带 bucket/thread 时不先按分数截断**——候选放全或 enclave 内 filter+limit,**不漏卡优先**(见 schema ⚠️)。
- **`limit` 旋钮删掉**(`FEEDLING_MEMORY_READSIDE_LIMIT` 105-139 去掉默认 50 那套):**index 默认全返回轻目录**;收范围靠 bucket/thread,不盲截(目录无 content、单卡很轻)。**保留一个不可见的安全上限**(HARD_MAX,纯防御,v1 不触发)——不是产品旋钮。**注意:气氛灯 ambient 的 top-N 是它自己的(几条底色,N 小固定),≠ index limit,别一起删。**
- `memory_fetch_core`(235-285):**fetch 真进 prompt 后更新 `last_referenced_at=now`**(只在 fetch 路径,不在 index)。

**`backend/enclave_app.py`**
- index item(1019-1029):产 `{id, summary, bucket, threads, importance, pulse, status, occurred_at, last_referenced_at, is_sensitive}`(去掉 salience/bucket_refs 旧名,bucket/threads 来自解密 inner)。**不含 content**。
- fetch item(1032-1045):产 `{..., content}`(用 v1 `content`,不再 verbatim/her_quote)。
- **fetch sensitive gate**(1128-1149):照 index(1119-1120)补——`include_sensitive=false` 时过滤 `is_sensitive`,返回 `blocked_sensitive_ids`。

**`backend/memory/routes.py`**
- `index`(136-154)/`fetch`(157-176):透传 `bucket/thread/ambient/limit` 参数。
- **新增 `GET /v1/memory/buckets` + `/v1/memory/threads`**:聚合现有卡(经 enclave 或从 inner)返回去重词表,给写入提示 resolve-before-create。
- **不做 `/v1/memory/recall`、不做 preflight**。`follow_thread` = `index(thread=X)`,非新端点。
- selector(`memory_index_selector.py` 173-180):入参/output 适配 v1(summary 仍是匹配源;bucket_refs→bucket/threads);sensitive 默认规则保留。

**验收**:`index(bucket=X)`/`index(thread=Y)` 过滤生效;`index(ambient=true)` 无 query 按 importance×pulse×recency;index 不含 content、fetch 含;fetch 后 last_referenced_at 更新、扫 index 不更新;敏感 id 直 fetch 被拦。

---

## P3 · 写侧 v1

**`backend/memory/actions.py`**
- `_execute_memory_action`(497-516)dispatch 收敛到 **`memory.add` / `memory.supersede` / `memory.delete`**;`memory.create`→add 别名;`memory.add_correction`→add;`memory.patch`/`content_patch`→**supersede**;`memory.retype`→400。
- `memory.add`(195-242):走 v1 inner;**去掉 insight/reflection anchor 校验**(84-115)。
- `memory.supersede`(390-473):soft——旧卡 `status=superseded`+`superseded_by`、**新卡继承旧卡 bucket/threads**、原子、**永不硬删**。保留。
- `memory.delete`(476-494):保留(Garden 用)。

**`backend/hosted_runtime.py`** coerce(348-467):`memory.create/add/add_correction`→add(v1 字段);`supersede`→supersede;`patch`→supersede;`retype`→400。**别碰 identity/perception 部分。**
**`tools/chat_resident_consumer.py`** 规范化(`_normalize_v2_action_type`):同上,与 route B coerce **产出等价**(conformance)。

**验收**:add 落 v1 卡;supersede 旧卡转 superseded、链新卡、不硬删、继承 bucket/threads;route A/B 规范化等价;create/patch/retype 按上面降级。

---

## P3.5 · 提示词初版(hx 出,集中一处,Seven 后替)
- 新增 `backend/memory/prompts_v1.py`(或合同引用处):**写入指引**(判断该不该记 + bucket/threads resolve-before-create + importance/pulse + content 三段)+ **注入框法**(气氛灯=底色别当话题、查到的自然织入别背诵、用每卡"使用提示")。**集中一处便于 Seven 整段替换**。CC 出初版文本。
- 已接入 route B 真实提示词组装:
  - 前台 memory tools system prompt 引用 `MEMORY_CONTEXT_FRAMING_V1`,约束 fetch 后怎么自然使用记忆。
  - 后台 state action prompt 引用 `MEMORY_WRITE_GUIDANCE_V1`,并把现有 `buckets/threads` 词表塞进 `existing_memory_terms`,用于 resolve-before-create。
  - 后台 memory capture prompt 输出 v1 字段(`summary/content/bucket/threads/importance/pulse/source`),不再要求旧 `title/description/type/her_quote`。
- route A native foreground 不在 consumer 里把隐藏规则拼进用户消息,避免污染真实 agent 输入。route A 侧应由 zhihao runtime/tool/skill 挂载同一份读写规则;consumer 只继续保证 action 名称规范化和 HTTP executor 收敛。

---

## P4 · readers 跟到 v1(删 legacy 前必须先做,否则炸)

- **enclave item builder**:已在 P2 改(读 content/bucket/threads)。
- **`backend/hosted/history_import.py`**(1492-1527,1680,1743-1814):产 v1 卡(bucket/threads),**不再映 type/TAB**。
- **`backend/identity/routes.py`**(56-67,112-125):memory floor 从 `_count_by_tab` → **v1 卡计数**(总数或按 bucket);earliest memory date 照用(occurred_at 仍在)。
- **`backend/memory/routes.py` `verify`**(430-543):删,或改成 v1 计数(去 tab/floor)。
- **`backend/proactive/tool_executor_v2.py`**(406-415,512-520):它的 `_memory_index_item` 读旧 `id/type/title`——**在它的边界加薄 shim**:v1 卡 → 它要的旧 shape(`type` 给空/默认、`title`=summary)。**不改 proactive 命名(memory.index/fetch 点号保留)、不改它主逻辑。**

**验收**:import 产 v1 卡;identity init 不依赖 tab floor;verify 不再报 tab;proactive 仍能拿到它要的 shape(经 shim)。

---

## P5 · 测试(删旧之前)
```
add 写 v1(bucket/threads/content/importance/pulse)、无 legacy
adapter:旧 M2 卡读出是 v1 shape
index 目录无 content、fetch 含;index(bucket/thread) filter;follow_thread 跨桶;ambient 无 query 按 imp×pulse×recency
index 默认全返回(无 limit 旋钮);超安全上限才截;status≠active 不返回;ambient top-N 不受影响
supersede soft(转 superseded、链、不硬删、继承 bucket/threads)
fetch sensitive gate(敏感 id 直取被拦 + blocked_sensitive_ids)
last_referenced_at 只在 fetch/注入更新(扫 index 不更新);pulse 不进 agent 排序
route A/B 规范化等价(add/supersede/delete)
import 产 v1;identity floor 卡计数;proactive shim 不断
GET buckets/threads 返回现有词表
create→add / patch→supersede / retype→400;list/get/delete 可用
```

---

## P6 · 删旧(测试通过后)
删:`MEMORY_TYPES` 的 insight/reflection、`_validate_anchor_ids`/`_reflection_time_cap_ok`、anchor 校验(actions 84-115、routes /add 284-314)、`retype` 端点+action、`TAB_FOR_TYPE`/`_count_by_tab`。抽 `hosted/context.py` 的 memory 部分成独立 adapter(给 build_companion_context;不重写主函数)。

## P7 · iOS(hx,非 Codex):隐藏 Garden tab → memory 稳 → 重做展示。

---

## 鉴权(走 A,Codex 注意)
memory 端点**保持认 `X-API-Key`**(`auth.require_user()` 不动);runtime token→用户 的翻译由 zhihao 的 tool gateway 服务端做。**Codex 本次不改 memory 端点 auth。**

## 不碰清单
perception/proactive 核心逻辑 + 命名、hosted_context 主函数、别人 144 提交、`memory_moments` 表结构、enclave 加密模型。

## 给 CC review 的产出
每个 P 的 diff + P5 测试结果;P6 删除前确认 P4 readers + P5 测试都过。

---

## 合并就绪结论(2026-06-25 Codex 复核)

结论:按当前 `feat/memory-v1-clean-schema` 真代码看,clean v1 合入 test **没有发现后端硬崩点**;但合前需要先吸收最新 `origin/test` 的两个 perception/test-image 提交后再跑同一组测试。

已确认:
- `/v1/memory/list` 对 clean v1 卡不要求 legacy `type/title/description`,只返回 moment 列表;新增测试覆盖 clean v1 卡可 200 返回。
- `/v1/memory/verify` 暂时保留 legacy tab-shaped response,但内部 `_count_by_tab` 已是 v1 active-card count shim;新增测试覆盖 clean v1 卡不会因缺 type/tab 崩。
- bootstrap gate 继续读 `_count_by_tab`,所以 clean v1 卡能降级通过旧 stage 判断;新增测试覆盖无 legacy tab 也能进入 `main_loop`。
- 老 M2 数据不迁移:backend envelope adapter `to_v1_card` 只补 `status/importance/pulse/last_referenced_at`;enclave inner adapter `_memory_inner_to_v1` 在解密后把旧 `title/description/her_quote/verbatim/context/follow_up/linked_dimension/anchor_memory_ids/type` 映射成 v1 `summary/content/bucket/threads`。默认桶:fact/event→`未分类`,moment/quote→`我们的关系`;threads 从 `threads`/`linked_dimension`/`anchor_memory_ids` 取。
- `prompts_v1` 不是 inert:route B foreground memory tool prompt import `MEMORY_CONTEXT_FRAMING_V1`;background state-action prompt import `MEMORY_WRITE_GUIDANCE_V1`;memory capture prompt import `MEMORY_WRITE_GUIDANCE_V1` 并要求 v1 output shape。
- 现有 bucket/thread 词表已注入 route B 写入判断 payload 的 `existing_memory_terms`;capture worker 也带同字段。Seven 后续可替换 `backend/memory/prompts_v1.py` 文案,不需要改执行器。

有意识保留/交接:
- route A native foreground 不在 consumer 里拼隐藏 prompt,避免污染真实 agent 用户输入;route A 规则由 zhihao runtime/tool/skill 挂载同一份读写合同。consumer 只做 action 名称规范化和 HTTP executor 收敛。
- `/verify` 的 tab 文案仍是兼容层,不是 v1 最终产品语义;P6 删除旧接口/旧文案前先保持不崩。
- DB-backed 测试已通过提权连接本机 Docker Postgres 跑过;普通沙箱会拦 `127.0.0.1:55432`,需用可访问 Postgres 的环境运行。

本轮验证命令:
```bash
/private/tmp/feedling-m2-venv/bin/python -m pytest \
  tests/test_memory_v1_schema.py \
  tests/test_memory_v1_readside.py \
  tests/test_memory_v1_readers.py \
  tests/test_memory_action_conformance.py \
  tests/test_memory_m2_write_loop.py \
  tests/test_memory_readside.py \
  tests/test_memory_readside_core.py \
  tests/test_memory_index_selector.py \
  tests/test_hosted_memory_tools.py \
  tests/test_hosted_memory_tool_loop.py \
  tests/test_model_api_prompts.py \
  tests/test_history_import_identity.py \
  tests/test_proactive_tool_executor_v2.py
```

结果:`81 passed`;普通沙箱下 DB-backed tests 会因 localhost 端口不可达被 skip。

DB-backed 补充验证:
```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres \
/private/tmp/feedling-m2-venv/bin/python -m pytest \
  tests/test_db.py \
  tests/test_identity_actions.py \
  tests/test_memory_v1_schema.py \
  tests/test_memory_v1_readside.py \
  tests/test_memory_v1_readers.py \
  tests/test_memory_action_conformance.py \
  tests/test_memory_m2_write_loop.py \
  tests/test_memory_readside.py \
  tests/test_memory_readside_core.py \
  tests/test_memory_index_selector.py
```

结果:`78 passed`;pytest provisioned throwaway Postgres DB successfully.
