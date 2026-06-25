# IO Memory · 读写行为合同(Read/Write Contract)

> 2026-06-24 · 作者:CC · 状态:**NORMATIVE(规范,生效中)**;Codex 已核对 §7,§5.1 fetch sensitive gate 已实现但 v1 默认休眠。
> 性质:**当前版本 route A / route B 必须一致的"基础行为不变量"**。不是质量调优(那是 M3)。**本合同对读写行为规则有最终解释权;定稿/skill/prompt 冲突以本合同为准。**
> 关系:这是从 `IO-memory-子系统-spec与plan-定稿v1.md` §3/§4 抽出的**规范法条**。**定稿后续应引用本合同,不再复述这些规则,避免两个真相源。**
> 适用三处:① 后端 `feedling-mcp/backend/memory/*`、`hosted_runtime.py`;② consumer `tools/chat_resident_consumer.py`;③ `io-onboarding/skill*.md`。

---

## §0 范围

**本合同管**:何时读 memory、何时取正文、敏感何时可查、何时**不**写、create/supersede/patch/delete 各自的触发、写入的时序与"能否说已记好"。这些两条 route **现在就必须表现一致**。

**本合同不管**(明确划走):
- **判断得准不准 / 漏记误记 / eval / 提取与 recall 的 prompt 质量** → 归 **M3**(`IO-memory-M3-质量与eval-方向.md`)。
- **type 分类法 / type↔tab 映射 / 记忆格式** → 归**格式议题**(`IO-memory-格式与tab-议题-给codex看.md`),正在重做。**本合同停在 action/行为层,不冻结 type 分类。**

---

## §1 两层结构(各有不同的"绑定机制",别混)

| 层 | 是什么 | 谁强制 | 漂移风险 |
|---|---|---|---|
| **L1 服务端不变量** | action schema(add/supersede/delete)、规范化等价、supersede soft、bucket/thread filter、敏感 gating、status 过滤 | **后端代码 + conformance 测试**(机器强制) | 低(两 route 都穿同一组 HTTP 端点) |
| **L2 agent 判断规则** | 何时读/写、create vs supersede、能否说"已记好" | **prompt/skill 文本**(人来守) | **高**——活在 route B prompt 和 route A skill 两处文本里 |

> L2 才是抽合同的主要价值:**route B 的 hosted prompt 和 route A 的 onboarding skill,都从本合同 §2/§3 派生、并显式引用它。**

---

## §2 读规则(L2,两 route 一致)

- **R1 何时读(index)**:当**长期记忆可能相关**时才查 —— 用户提到过去、问到关于自己的事、需要延续上下文、纠正/更新旧事实。**普通寒暄/问候/玩笑/一次性闲聊不查。**
- **R2 取多少(fetch)**:agent 读 `index` 的 summary,**自己挑 1–3 条真正相关的 id**,只 `fetch` 这几条正文。**不要全量 fetch,不要在弱相关时 fetch。**
- **R3 没命中不编**:`index`/`fetch` 没有相关结果时,**正常回答、不要编造记忆**。
- **R4 敏感 gating(v1 默认关闭)**:本 app v1 决定默认把敏感记忆当普通记忆处理,即 `MEMORY_SENSITIVE_GATING_ENABLED` 默认 off。off 时,`include_sensitive` 不影响 readside,index/fetch/selector 都不因 `is_sensitive` 过滤。
  - flag-on 时恢复从严门禁:`include_sensitive` 默认 false;agent 不得为"多补点上下文"主动打开 sensitive;只有用户**显式请求**、且请求本身就关于该敏感主题时,才允许取敏感卡。
  - flag-on 时,`index` 默认不返回敏感卡;`fetch` 按 id 取正文时也会在 enclave 解密后过滤敏感卡,并返回 `blocked_sensitive_ids` 便于观测。
- **R5 读 = agent-first,不做 recall/preflight**:默认 agent 会 call tool → 该查自己 `search→fetch`(query/bucket/thread),闲聊不查。**气氛灯 ambient = runtime push**(非 agent 查):`importance×pulse×recency` 无 query 取 top-N,会话开始带几条关系底色。identity 也常驻 push。(无 recall 兜底、无每轮 preflight、无 should_read。)

**两 route 的读分工(机制不同、行为一致,详见定稿 §3.2)**:route B 看得见 tool_calls → 条件式(没调才兜底);route A 看不见 → 无条件推小 baseline + agent 自查叠加。**一致 = "agent 挑不出来时都有兜底",不是"两条都 always 双推"。**

---

## §3 写规则(L2,两 route 一致)

> **v1 结构定稿(merged Seven baseline)**:写入 = **agent-in-loop 主 + 服务端 capture 兜底,共用本 §3 一份规则**。**动作:`memory.add` / `memory.supersede` / `memory.delete`**(`memory.create`=`add` 别名;supersede = soft 软退场)。结构以 `IO-memory-v1结构定稿-bucket-thread.md` 为准。

- **W1 何时不写**:寒暄、提问、玩笑、一次性/临时情绪、**只是引用已有记忆**、**agent 自己没被用户确认的推测**、角色扮演/假设——**都不写**。不每轮都写。
- **W2 事件即记忆,不分 type;归 bucket + 挂 thread**:
  - 记**一件完整的事**(人/事/情绪/过程);fact/relationship/insight **用 thread 贴标签,不做卡 type**。
  - `bucket`(**单选**,主话题):**优先复用现有桶**(写入提示会注入现有 bucket 列表),没有才新建,克制别造近义。
  - `threads`(**多选** 1–4,线索/人物/情绪/关键点):**优先复用现有线**;同一条线一个名(`蛋子` 不写成 `狗狗`)。
  - 稳定设定/称呼/边界 → 进 **identity**(或提议 identity 更新),不是普通 memory。
- **W3 importance + pulse 打分**:
  - **`importance` 0–1 = "看不看"**(对长期理解用户多重要,**不是语气**;客观、不随时间变):0.1–0.3 普通事实 / 0.4–0.6 偏好宠物生活 / 0.7–0.85 情绪关系边界 / 0.9–1.0 强情绪核心边界危机。
  - **`pulse` 0–1 = "想起来时情绪多强/多激活"**(只影响 agent 表达色彩,**不进检索排序**)。
- **W4 content 格式**(固定 MD 三段):`记忆:` + `上下文:` + `使用提示:`。`summary` 一句话短摘要,只给 index。
- **W5 纠错 = `memory.supersede`(soft)**:用户改口/纠正旧事实 → `supersede(target=旧卡id, memory=新卡)`;**旧卡 `status=superseded`、链到新卡、永不硬删;新卡继承旧卡 bucket/threads**。
- **W6 delete**:仅用户明确要"删/遗忘"才真删。
- **W7 去重**:同一事实已有卡 → 用 `supersede` 更新,不 `add` 重复。
- **W8 时序 +「已记好」规则**:写是**回复后异步提交**,agent 当下不知是否落库 → **不要把"已保存/已记好"当既成事实说**,说成意图或不提。
- **规范化等价**:route A(`_normalize_v2_action_type`)/ route B(`coerce_runtime_action`)对 `memory.add/supersede/delete` 规范化后**必须产出同一 executor action**(§4 conformance 守)。

---

## §4 服务端不变量(L1,文档化 + 链代码与测试)

这些后端已强制(除标⚠️者),**两 route 自动一致**(都走同一组 HTTP 端点)。**改这些必须同步改对应 conformance 测试。**

> ⚠️ **本节为 v1 结构定稿版**(bucket/thread + importance/pulse + supersede soft;删 anchor 门槛 / legacy 双写)。

**写入口(v1)**:
- **`POST /v1/memory/actions` = agent/runtime 统一写入口**:收 `memory.add` / `memory.supersede` / `memory.delete` → 规范化(route A `_normalize_v2_action_type` / route B `coerce_runtime_action`,**产出等价**)→ executor。
- **`POST /v1/memory/add` = legacy envelope 直写**:只给**旧 iOS / import**,不进新工具契约。
- **`list / get / delete` 保留给 Garden**(看详情 / 删记忆)。

**旧动作降级**(短期兼容,防旧 prompt 没清干净就断):
```
memory.create         → memory.add
memory.add_correction → memory.add
memory.supersede      → 支持(soft:旧卡 status=superseded、链新卡、不硬删)
memory.patch          → memory.supersede(改记忆=退旧立新,不做字段级 patch)
memory.retype         → 400 unsupported
```

**L1 不变量(v1)**:
| 不变量 | 说明 / 代码 / 测试 |
|---|---|
| action `add`/`supersede`/`delete`(create=add 别名)+ **A/B 规范化等价** | `_normalize_v2_action_type` / `coerce_runtime_action`;`tests/test_memory_action_conformance.py` |
| 字段:`bucket`(string,单选)、`threads`(string[],多选)、`importance`/`pulse ∈ [0,1]`、`status`、`source ∈ {chat,screen}`、`occurred_at`、`last_referenced_at` | 写入校验 |
| `content` = MD 三段;`summary` 短摘要 | — |
| `decay` 读时从 `last_referenced_at` 派生(不存、无后台任务);排序 ≈ 相关性×importance×(1-decay);**pulse 不进排序** | v1 定稿 §2 |
| **supersede soft**:旧卡 `status=superseded`+链新卡、**原子、永不硬删**;新卡继承 bucket/threads | `memory/actions.py:_memory_supersede_action`;`tests/test_memory_m2_write_loop.py` |
| readside:**支持 `bucket` / `thread` filter**;**`index` 只返回摘要(不含 content),`fetch` 才返回 content**;**`limit` 可配(默认放宽 / 0=全),不 hardcode 50**;`status≠active` 不返回 | `memory_index_core` / `memory_fetch_core` |
| **`follow_thread` = `index(thread=X)` 过滤**(跨 bucket),非独立端点 | `memory_index_core` |
| 读 = `index`(目录,无 content)→ agent 挑 → `fetch`(content);**无 recall/preflight**;气氛灯=`index` 无 query 按 `importance×pulse×recency` 取 top-N(runtime push)| `memory_index_core`/`memory_fetch_core`;`memory_index_selector` |
| 敏感 gating:`MEMORY_SENSITIVE_GATING_ENABLED` 默认 off | `memory_readside_config`;`tests/test_memory_readside*.py` |
| **resolve-before-create**:`GET /v1/memory/buckets\|threads`(或聚合现有卡)给写入提示注入现有词表 | 新增小能力 |

---

## §5 防漂移(合同怎么"不只是一份会过期的 md")

1. **L1**:靠上表的 conformance 测试做机器闸门。改行为 → 先改测试。
2. **L2**:**单一来源 = 本合同 §2/§3**。
   - `io-onboarding/skill*.md` 的读写章节**显式引用本合同**(skill 顶部一行指针)。
   - route B 的 hosted prompt/controller 同样以本合同为准。
   - **review checklist**(改动 memory 读写行为时必过):□ 改了 §2/§3 吗?□ route A skill 同步了吗?□ route B prompt 同步了吗?□ L1 受影响则 conformance 测试同步了吗?
3. **挂里程碑验收**:里程碑 M 的"真 agent-first smoke"(后端日志看到 agent 自调 index/fetch)= 验 §2 读规则真被执行;"持久事实两边都落卡" = 验 §3 写规则一致。

### §5.1 已完成项
- **fetch sensitive gate + tests 已补,但 v1 默认休眠**:`memory_fetch_core` 透传 `include_sensitive`;enclave `v1_memory_fetch` 在解密后按 `is_sensitive` 过滤;非 `include_sensitive=true` 不返回敏感卡正文,并返回 `blocked_sensitive_ids`。当前 v1 通过 `MEMORY_SENSITIVE_GATING_ENABLED=off` 默认把敏感当普通;翻开 flag 即恢复门禁。

---

## §6 范围外(别写进本合同)

- **type 分类法 / type↔tab / 记忆格式** → `IO-memory-格式与tab-议题-给codex看.md`(讨论中,会重做)。本合同涉及 type 处一律标"当前分类、受格式议题影响"。
- **判断质量 / eval / prompt 调优 / 漏记误记** → M3。
- 常驻/pinned 层、proactive 唤醒、A4 屏幕→记忆、MemPalace、merge/decay → 各自后置文档。

---

## §7 给 Codex 的核对点

1. §4 的代码/测试映射对得上吗?有没有我漏的不变量(尤其 enclave 边界)?**两条写入口的定性**(`/v1/memory/actions`=统一 executor 入口;`/v1/memory/add`=legacy envelope 直写、不经 executor)——准确吗?
2. §3 动作词:**agent-facing 别名 `memory.create` / executor canonical `memory.add`(create→add)** 这样表述清楚吗?其余 supersede/patch/delete 别名与 canonical 是否一致?
3. **§5.1 已完成:fetch sensitive gate + tests** —— Codex 已实现;后续改动需同步测试。
4. 本合同已设为 **normative**;请确认定稿 §3/§4 已"保留架构、删细则、改引用"(CC 已改,见定稿)。
