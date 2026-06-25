# readside M1 · Review + 建议(Claude → Codex)

> 2026-06-21 · 作者:Claude(Opus 4.8) · 给正在执行代码的 Codex
> 已读:联合工程 spec、M1 zhihao plan、给 zhihao 的问题、plan 演进交接、以及 `context_memory_selection.py` + `app.py` readside 实现。
> 结论先行:**readside M1 方向对、执行克制,通过。** 下面是 3 个要盯的点 + 下一步优先级。

---

## 0. 一句话

readside M1 是个扎实、低风险的底座(铁轨铺好了)。但要记住三件事:**① eval 要当"切主链路"的闸门;② 这套的真价值在 route A 的 agent 语义挑,别用 route B 算法 pick 的效果给架构判死刑;③ top-50 查询无关,卡多会漏相关旧卡。**

---

## 1. 做得好的点(认可,别改)

1. **adapter-first / 不迁移 / 不改表** —— 线上有活数据 + iOS Garden,这个护栏对。
2. **Contradict 用 `conflicts_with + needs_resolution`,不用 `status=CONTRADICTED`** —— 比 Claude 原版好。一张卡可以"既 active 又冲突",不该塞进 status。保留这个改法。
3. **隐私分级**:index 不返回 verbatim/her_quote/follow_up/sensitive_scope,fetch 才给;backend 日志不打明文。对。
4. **scope 克制**:M1 只读不写,commit/supersede/decay 推 M2。风险管理得当。
5. **字段纪律**("每个字段必须被一个操作使用")保住了,schema 没膨胀。

---

## 2. 三个要盯的点

### ① eval 是"切主链路"的闸门,不是可选尾巴

- 现在没 eval 做 readside **没问题**,因为 readside 只是铺铁轨,**主聊天还没切过去、行为没变**。
- **但红线**:等要把主聊天召回**真切到 index/fetch、或改 capture** 时——那一刻行为变了,**必须先有 eval(哪怕 10 道 probe 题)才能切**。否则无法证明新路比现状的关键词召回更好。
- **建议**:把"有最小 eval 能对比老路 vs 新路"**写成切主链路的前置条件**。

### ② 价值是 route-dependent,别用 route B 给架构判死刑

| 路线 | pick 谁做 | 覆盖率 | 精准 | 定位 |
|---|---|---|---|---|
| **route B(model_api,后端驱动)** | 算法(复用 context_memory_selection) | **每轮都召回(稳)** | **不提升**(还是关键词) | **只减负**(少解密、少塞 token) |
| **route A(自建,agent 驱动 + skill)** | 用户自己 agent 语义挑 | **尽力而为**(不保证每轮查,但有时命中) | **真提升**(语义,免费,不占服务器 LLM) | **真正价值所在** |

- **关键**:这俩不是"谁更好",是 **覆盖率(B 稳) vs 质量上限(A 高)** 的取舍。route A 的 skill 召回**不保证每次都查,但查的时候更准** —— 这是它的真实形态,别期望它 100% 触发。
- **风险**:如果只做 route B 算法 pick,很容易得出"这套 index/fetch 没啥用"的错误结论。**没用的是 route B 那条,不是架构。**
- **建议**:**别用 route B 的效果评判这套架构的价值;真正验证要等 route A 的 agent 语义挑接上**(哪怕只是部分命中)。

### ③ top-50 是"查询无关"的,卡多会漏相关旧卡

- `_memory_readside_candidates` 纯按元数据排序取 50(`is_open_thread → salience → importance → 时间 → id`),**不接收用户的问题/关键词**(M1 plan §3.1 明确"backend 不做语义相关性")。
- **问题**:相关但"旧/低 salience"的卡排到第 51 名就进不来,enclave/agent 永远看不到 → 召回不到。老路(解密全部)反而没这问题。
- **现状**:用户卡 <50 不暴露;**卡一多就是真问题**。
- **建议**:M2 前给 index 加一层"按问题/关键词/向量的**相关性预筛**",而不是纯元数据 top 50。先记下来,别当成已解决。

---

## 3. 下一步优先级(我的排法)

1. **readside 底座留着,别急着往 core 堆功能**(先别写 commit/supersede)。
2. **下一步 = 让 readside 产生第一次真价值 + 可测量**:
   - **优先接 route A 的 agent 语义挑**(用 skill 让用户 agent 走 index→挑→fetch)。接受它是 best-effort、不保证每轮——但这是唯一能证明"agentic 召回 > 关键词"的地方。
   - **切之前立一个最小 eval**,对比"老关键词召回 vs 新 agentic 召回"。有数再切。
3. **route B 那条**:可做,但定位写清是"减负"(省 token/少解密),**别指望提精准**;要 route B 也提精准只能 post-M1 加 embedding。
4. **commit / supersede / decay(联合 spec 大头)**:是对的 M2 目标,但**等 readside 真被用上、被测过再开**,别提前。

---

## 4. 一句话收口

> **readside M1 通过、底座扎实(Contradict 改得比原版好)。三个盯防:① eval 当"切主链路"的闸门(现在没切所以没 eval 没事,真切就得有);② 真价值在 route A 的 agent 语义挑(best-effort 但更准),别用 route B 算法 pick(稳但只减负)给架构判死刑;③ top-50 查询无关,卡多会漏,M2 前补相关性预筛。下一步重点:接 route A + 立最小 eval,别急着堆 core。**
