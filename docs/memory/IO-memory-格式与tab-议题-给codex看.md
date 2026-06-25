# IO Memory · 记忆格式 / type / tab / index 议题(给 Codex 先看,等下一起讨论)

> 2026-06-24 · 作者:CC(整理 Seven 的想法 + CC 的核对)· 状态:**议题讨论稿,不是实现 spec**
> 目的:这是一个**还在讨论中的方向性问题**,先让 Codex 看懂现状 + Seven 的方向 + 开放问题,**回点看法/风险**,我们再一起拍。**不要现在动手改代码**。
> 配套:与 `IO-memory-子系统-spec与plan-定稿v1.md`(读写主线)是**两件事**——那份是"怎么读写记忆",这份是"记忆本身长什么样、怎么分类/分 tab/建 index"。

---

## 0. 一句话

Seven 觉得现在"3 个 tab 的记忆像 3 种不同东西,导致 index 没法统一匹配、也没多大用";她想**把记忆收成一个统一格式,只是用不同方式读/总结**,并且 **type 和 type↔tab 映射都可以推翻重做**。这份是把这个问题摊清,让 Codex 一起判断。

---

## 1. 现状(已核对代码,给 Codex 对齐事实)

**`backend/memory/service.py`**:
- `MEMORY_TYPES = (moment, quote, fact, event, insight, reflection)` —— 6 种 type。
- `TAB_FOR_TYPE`:`moment/quote → story`、`fact/event → about_me`、`insight/reflection → ta_thinking` —— 3 个 tab。
- `insight` 要 ≥1 anchor、`reflection` 要 ≥2 anchor 且有频率上限(关系 <30 天最多 2 条),anchor 指向别的卡。

**关键事实(纠正一个常见误解)**:
- **存储 schema 其实是同一套**。`backend/memory/actions.py:_memory_inner_from_action` 对**所有 type**都拼同一组内部字段(`summary/title/description/verbatim/her_quote/context/follow_up/linked_dimension…`)。**不是"每个 tab 格式都不一样"**——底层是共用一个 envelope。
- 真正不一样的是**"种类/含义",不是"数据结构"**:
  - `story`(moment/quote)= 你俩之间发生的事 + 原话 → **关系记忆**
  - `about_me`(fact/event)= 关于用户的事实 → **事实记忆**
  - `ta_thinking`(insight/reflection)= **agent 自己对用户的推理/建模**(要 anchor 到别的卡)→ **本质不是"记忆",是 agent 的思考**

→ 所以 Seven 说"格式完全不一样"**更准确的说法是**:"**种类/含义完全不一样**(尤其'TA 在想'根本不是记忆),但**存储 schema 早就共用一套了**"。

---

## 2. 问题(Seven 提的痛点)

1. **index 没用**:因为 3 个 tab 对应**完全不同的行为/含义**,一个 query 没法在"事实"和"agent 推理"之间统一匹配——index 里混着两类根本不同的东西,挑不出来。
2. **type/tab 是早期拍的,可能不该作为依据**:Seven 原话——"前端的内容是要变化的,不能以之前的数据结构为依据;我 spec 里给的那个结构只是**一种数据结构方式的参考**。"
3. **"TA 在想"混进记忆**:insight/reflection 是 agent 的推理,塞进"记忆"这套里,既污染 index,也概念不清。

---

## 3. Seven 的方向

1. **一个统一的记忆格式**,只是**用不同方式读和总结**(不是不同的存储格式)。
2. **type 可以完全推翻**,**type↔tab 的映射也可以重新做**。
3. 别拿旧的 6-type/3-tab 数据结构当法律,它只是参考。

---

## 4. CC 的精度补充(同意方向 + 三点厘清)

1. **存储层其实已经统一了**(一套 envelope)。所以"统一格式"在存储层**大半已是事实**——真正要动的不是"把格式统一",而是"把**种类**理清"。
2. **真正该做的拆分**:把**"真记忆"(facts / moments / events)** 和 **"agent 的推理"(insight / reflection = TA 在想)** 分开——后者**不是记忆**,应该是独立的一层(agent 对用户的模型),不该和记忆挤在一个 index/一套 type 里。
3. **tab = 对统一库的"读/总结模式",不是存储 type**。前端三个 tab 应该是同一份记忆库的三种**视图/呈现**,而不是三种**存储种类**。
4. **index 为什么"没用"的根**:种类混杂(事实 vs 推理)→ query 没法统一匹配。**分清种类(或给每条标 `kind`)+ 统一格式 → index 才匹配得动**;再叠 embedding 更好。

---

## 5. 想请 Codex 看的(开放问题,先给看法不要动手)

1. **事实判断**:§1 对当前 type/tab/存储 schema 的描述,和三个 repo 代码一致吗?有没有我漏的(比如 iOS 端对 tab/type 的额外依赖、前端渲染对字段的硬假设)?
2. **"分种类"怎么落**:在统一 envelope 上加一个 `kind`(real_memory / agent_reasoning / …)够不够?还是要更细?现有 6 type 哪些该合并、哪些该升成 `kind`、哪些该从"记忆"里剥出去?
3. **tab 重做**:如果 tab = 读模式,那 story/about_me/ta_thinking 该不该还是这三个?ta_thinking 既然"不是记忆",前端是继续放在记忆 tab 里,还是单独成区?
4. **index 的真问题**:index 现在的"没用"到底是"种类混杂"导致,还是"纯关键词、没语义/embedding"导致,还是两者都有?哪个是主因?
5. **跨影响(必须评估)**:这套改动会动到——
   - **M2 MemoryCard v1**(`card_v/status/salience/.../supersedes`)和 **legacy 双写**(`title/description/her_quote` 保 iOS Garden)
   - **M3**(该记什么、记成什么 kind)
   - **召回/index/`/v1/memory/recall`**(分了 kind 之后,召回是否按 kind 过滤)
   - **iOS Garden 渲染**(tab/type 一变,前端要跟着变——Seven 说前端本来就要变)
   哪些是"存储不用动、只加字段"?哪些是"破坏性变更"?有没有平滑迁移路径?

---

## 6. 边界(这次讨论**不要**做的)

- 不要现在改代码、不要推倒 M2 已实现的存储。
- 这是和"读写主线(定稿 v1)"**并行的结构议题**,等讨论拍板后再排进 plan。M2 的"双写 legacy 保 Garden 不坏"护栏,在新 tab/格式落地前**仍然有效**。

---

## 7. 给 Codex 的输出格式

1. §1 事实核对(对/补/纠)
2. 对 §5 五个开放问题各给一段看法(倾向 + 理由 + 风险)
3. 一张"跨影响 / 迁移"表(改动点 → 影响面 → 破坏性? → 迁移建议)
4. 一句话:你觉得这个方向(统一格式 + 分 kind + tab=读模式)值不值得做、最该先动哪块
