# IO Memory v1 · 极简重做(给 Codex review)

> 2026-06-24 · 作者:CC(整理 hx + Seven 讨论结论)· 状态:**设计稿,待 Codex review → 评估改造量**
> 配套(现状/执行线):`IO-memory-完整设计与现状-vNext.md`(4 轴框架)、`IO-memory-read-write-contract.md`(现行读写合同,本稿会简化它)、`IO-memory感知-模块分工-hx与zhihao.md`。
> ⚠️ 本稿是**记忆数据模型的大幅简化(做减法)**,不是加功能。核心:**砍掉为前端/复杂分类堆出来的冗余,回到极简核心。**

---

## 0. 背景与上下文(Codex 必读,别脱离这个判断)

- **两个老板**:Seven(memory reframe / agent-tools 方向,**她也在改这块**)、xyn(runtime 统一 / build_companion_context)。本稿 = hx + Seven 方向。
- **为什么要简化**:现在的 MemoryCard v1(M2)字段太多——`title/description/summary/verbatim/her_quote/context/follow_up/linked_dimension/card_v/status/salience/importance/source_type/supersedes/superseded_by/is_sensitive` + legacy 双写。**大半是为了喂 iOS Garden 前端 或 没真正用上。**
- **两个已核实的事实**(支撑"可以大砍"):
  1. **capture worker 只产 `fact|event|quote|moment` 四类,而这四类本质都是"事实"**;`insight/reflection`(TA在想)**根本没有生成器、实际是空的**。→ 复杂的 6-type 分类是空架子。
  2. **存储是 JSON**(`db.memory_*` moments / `user_blobs`),**以后加字段零成本、不用迁移** → 不需要预留字段。
- **前端会变**:Seven 明确"前端内容要变,别拿旧数据结构当依据"。→ v1 **不伺候现有 Garden 样子**,Garden 跟着新模型重做。
- **不要做的**:别假装 TA在想/复杂分类存在;别为"将来可能用"预留一堆字段;别推倒 M2 的写入终点 `_execute_memory_action`(改的是 schema 内容,不是写入管道本身)。

---

## 1. 一句话

**记忆 = 一种极简召回卡(summary + 一段 MD 正文 + 情感浓度 + 衰减 + 出处 + 时间);常驻 = identity(用户控制的人设,系统不自动改);"画像/自动蒸馏"v1 不做。检索 = index 回摘要目录 → agent 自己挑 → fetch 取正文;identity 每轮注入。** 围绕这个,记忆相关的 tool/action 大幅瘦身。

---

## 2. 记忆卡 v1 schema(最终)

```jsonc
{
  "id":            "m_xxx",
  "kind":          "relationship | fact", // 路由检索:relationship=常带(气氛灯),fact=按需查(档案柜)。不为显示,不回旧 6 类
  "summary":       "短摘要 — index 列目录 + agent 扫描匹配用",
  "content":       "一段 agent 易读的 Markdown — 记忆 / 上下文 / 使用提示(见 §写入)",
  "source":        "chat | screen",      // grounded 观察出处;v1 基本全 chat。screen=以后直接从屏幕观察到的事实(罕见);A4 的"猜测"不在这,进推理层
  "emotion_weight":  0.0,                 // 0–1:这条记忆"未来对理解用户有多重要"(不是语气强度);写入时模型打分,分档(见合同 W3)
  "occurred_at":     "ISO8601",          // 创建/发生时间(判断新旧/纠错)
  "last_referenced_at": "ISO8601"        // 上次被用到的时间;decay 从它读时派生,被再用就更新(回升)
}
```
**就这些(8 字段)。** 排序 ≈ `相关性 × emotion_weight × (1 - decay)`。
- **`emotion_weight`**:写入时模型打分(分档 0–1,见合同 W3),= "对理解用户的重要度"。
- **`source` 只有 `chat | screen`**(grounded 观察的出处)。**注意:屏幕的"猜测"(如"想买相机")不是 fact、不写这里,它进推理层(§4.1)**——别用 source 表达 inferred,免得混淆 grounded vs 猜测。
- **`decay` 不是存的字段,是读时派生**:`decay = clamp((now - last_referenced_at) / half_life, 0, 1)`(越久没被提到越高)。
  - **half_life 初值(可配)**:`relationship=30 天`、`fact=90 天`;**`emotion_weight ≥ 0.8 → half_life ×2**(重要的老得慢)。
  - **`last_referenced_at` 只在这几种情况更新(回升)**:① `fetch` 后真进了 prompt ② recall 兜底真注入了 prompt ③ agent 明确用它生成了回答。**只在 index 目录里被扫到 ≠ 被想起,不更新**(否则扫目录就让记忆无脑回春)。
  - **无后台任务,排序时当场算。**

**为什么砍 / 为什么这么少**:
- `title/description/summary/verbatim/her_quote/context/follow_up/linked_dimension` → **塌成一段 `content`(MD)**;只额外留 `summary` 给 index 匹配。
- `salience/importance` → **`emotion` + `decay`** 替代。
- `is_sensitive` / 隐私字段 → **v1 不做自动敏感**(见 §7 隐私;用户控制走 Garden 删/隐藏)。
- `source_type` 那套 → 一个极简枚举 `source`。
- `card_v / status / supersedes / superseded_by` → **v1 不做 supersede**(见 §5)。
- **legacy 双写**(title/description/her_quote 保旧 Garden)→ **v1 不再双写**(Garden 重做,见 §7)。

---

## 3. 常驻 vs 召回(关键:按"行为"分,不按"内容"分)

**唯一影响行为的线 = 常驻(每轮注入) vs 召回(按需检索)。** "identity / relationship / fact" 这类内容标签模糊、不值得定义。

| 层 | 是什么 | 存哪 | 更新 |
|---|---|---|---|
| **常驻** | identity(用户人设:名字/角色/边界/称呼/do_not_say/dimensions/stable_definitions)| `user_blobs` kind=identity(已存在,**别改**)| **用户控制**:用户编辑 / agent 提议→用户确认。**系统不自动改用户人设**(派生字段如 days 可自动)|
| **召回** | memory 卡(§2)| `db.memory_*` moments | capture 持续捕获 |

- **memory v1 = 纯召回。没有 layer / pinned / type 字段。** 常驻关系靠 identity(本来就常驻),不在 memory 卡里另立一类。
- **"画像 / 持续优化的核心摘要" v1 不做**:它要么会"自动改用户 identity"(用户不一定想要),要么需要单建蒸馏器(新活)。**v1 跳过**,以后做也是**独立一块、明说是 agent 的笔记、不碰用户 identity**。位置留好(identity 里将来可加 `core_summary`),但 v1 不实现。

---

## 4. 检索 + 注入(A3-lite,与 Codex 收敛)

> ⚠️ **能力 vs 编排边界**:**纯净分支只提供"拿记忆的能力"**(下面的 selector / 端点):`select_relationship_ambient(...)`、`select_relationship_relevant(...)`、`memory_index(kind=...)`、`memory_recall(kind=...)`、`memory_fetch(...)`。**"每轮要不要调、调几条、怎么塞进 prompt" = zhihao 的 runtime 编排(build_companion_context),不在能力分支里替他决定。** 下面的"每轮 push / 封顶 / 注入构成"是**产品目标逻辑(给 zhihao 参考)**,不是能力分支要实现的。

**按 kind 走不同方式**(关系=气氛灯一直开;事实=档案柜按需查):

- **identity = 每轮 push**(常驻人设,用户控制)。
- **relationship = 每轮 runtime push(气氛灯)** —— ⚠️ **是 runtime 算好注入,不是让 agent 去搜**(否则又会被实现成"agent 没搜就没了")。两半:
  - **ambient 2 条 = 主力**:按 `emotion_weight×0.55 + recency×0.25 + (1-decay)×0.20 - 最近浮现惩罚`,**不匹配当前消息**,靠重要度一直在(最近浮现惩罚:近 3 轮 -70% / 4-8 轮 -35% / >8 轮 0)。
  - **relevant 3 条 = best-effort**:按 `query_match×0.55 + emotion_weight×0.25 + recency×0.10 + (1-decay)×0.10`。**注意:关系卡是抽象模式(如"难过时要陪"),拿关键词配具体消息(如"被老板骂哭")很弱、常漏。** 所以 v1 它只是补充;**真正的 relevant 要语义匹配(embedding)/情绪状态匹配,后置**。可选:v1 关系**只做 ambient**,relevant 等语义再加(更干净)。
  - **去重**(一条不双算)。
- **fact = agent-first(档案柜)**:agent `index`(回 `{id, summary, occurred_at, emotion_weight, decay}` 目录)→ 自己挑相关 id → `fetch`(取 content)。**runtime recall 兜底**:agent 没查时,服务端 recall 取最多 5 条相关 fact 注入(弱模型不失忆)。
- **最近新鲜度地基(保留旧设计)**:**最近 N 条记忆卡 always 带**——**"最近"指最近 `created_at` / `last_referenced_at` 的 memory 卡,不是 recent chat**(避免和聊天历史重复);刚说过/刚确立的事保持在场;和 ambient/relevant 去重,靠 decay 自然淡出。
- **总数封顶(保留旧设计)**:**每轮注入记忆总数封顶,可配,默认 10**(token 纪律,eval 调,别 hardcode)。超了按优先级砍:**always-on 地基(ambient relationship + 最近)优先,relevant/fact 让位**——给地基留位,别被 fact 挤掉。
- **memory 不需要 resident 端点**:identity 走 `/v1/identity/get`;relationship/最近 的 push 用 `index`(kind 过滤 + 上面排序);fact 走 `index/fetch/recall`。

**v1 必做 vs 可选**:**ambient relationship 必做**;**relevant relationship 只提供接口、默认关 / best-effort**(关键词配关系弱),**语义/embedding 后再增强**。`recent_floor` 必做;`fact` agent-first + recall 兜底必做。

**参数初值**(可配,待 eval 调):`relationship_ambient=2`、`recent_floor=2`、`relationship_relevant=3`(默认关/best-effort)、`fact_recall=5`、`total_cap=10`、`recent_surface_window=8`。

> **历史依据**:这套 = 旧 `select_context_memories`(3 转折点 always + 2 最近 always + 3 关键词相关 + 封顶 8)的现代化——转折点→ambient(emotion_weight)、关键词→agent-first、保留"最近地基 + 封顶 8"。不是推倒,是把验证过的形状升级 + 两 route 统一。

**矛盾/纠错**:**不做 supersede**,改口写新一条(新日期 + content 注明"用户更新了旧说法"),agent 靠 `occurred_at` + content 判断当前。边界:矛盾卡堆积,靠 `decay` 沉底 + 以后 merge 清(v1 接受)。

> **A4 `relationship_digest`(关系摘要/画像)= 后置**,不进 v1 主链;等 relationship 卡稳定写入 + A3-lite 过 eval + 攒 20-50 条样本再做。它是"可重建缓存,非真相本体",与 identity(用户控制)、relationship 卡(事实来源)边界清楚。

### 4.1 邻接:短期 / 感知层(截屏 + 适配信息)—— 归 zhihao,记忆侧只留钩子
每轮完整上下文里还有**短期层(当前状态,push 完即弃,不是记忆卡)**:**截屏(OCR+图)+ 适配信息 perception(位置/移动/电量/user_state)+ recent chat + pending**。
- **归属**:这是 **zhihao 的感知子系统**(截屏现关键词门控,xyn P2 要改默认带;perception 已注入 context_payload)。**不是 hx 记忆层。**
- **和记忆的交点 = A4(把感知串起来 → 做猜测)**:**⚠️ A4 产出的是"推理/猜测"不是"事实"**——"看了 3 天相机"是观察,"想买相机"是**猜测**。所以 **A4 喂的是"推理层",不是 fact 层**。
  - **关键统一**:A4(屏幕模式猜)和之前 defer 的"画像/TA在想"(聊天模式猜)**是同一层 = agent 对用户的推理/猜测**,聊天和屏幕都往里喂。**两者一起后置,v1 不做。**
  - 用时当猜测:**低置信、试探性措辞("我注意到…在考虑吗?")、可推翻、衰减快**;**不灌进 fact 层污染召回**。**v1 不留 source 钩子**(`source` 只 `chat|screen`,是 grounded 出处;推理是另一层、不靠 source 表达),只在文档标"推理层后置"。
- **三层 epistemic 模型(理清)**:
  ```
  grounded(用户说的/做的)  → fact / relationship          ← v1 做
  inferred(agent 的猜测)   → 推理层(画像 + A4,聊天+屏幕都喂)  ← 后置统一
  短期(当下,push 完即弃)   → 截屏 / perception / recent / pending  ← zhihao
  ```
- 完整每轮上下文:`identity(常驻)+ 记忆(fact/relationship,召回/常带,封顶10)+ 短期(截屏/perception/recent/pending,push)`。

---

## 5. 工具 / 动作大瘦身(连锁简化)

| 现有 | v1 |
|---|---|
| `memory.create/add` | **留**(`add`,executor canonical) |
| `memory.delete` | **留**(用户在 Garden 删) |
| `memory.supersede` | **砍**(改口=新写一条 + 日期判断) |
| `memory.patch` | **砍**(要改就新写) |
| `memory.retype` | **砍**(没 type 了) |
| `memory.add_correction` | **砍**(就是一条新 add) |
| `/v1/memory/index|fetch|recall` | **留,简化**(去 layer、吐新字段、排序换 emotion/decay) |
| `/v1/memory/list|get` | **留**(Garden 时间线/详情) |
| `/v1/memory/add`(legacy envelope 直写) | **标 legacy / 逐步弃**(新逻辑只走 `/v1/memory/actions`) |
| capture worker | **简化**:产新卡形(summary + content MD + emotion + source=chat),**不再产 type / 那堆字段** |

→ 写侧基本只剩 `add` + `delete`;读侧 `index/fetch/recall` + `list/get`。**读写合同(`IO-memory-read-write-contract.md`)的动作词表要随之简化**(create/supersede/patch/delete → add/delete)。

---

## 6. 字段预留:**不预留**(重要决定 + 理由)

- **存储是 JSON,加字段零成本、不用迁移** → 没必要为"将来可能用"预留。
- **`tag` / `type` 现在都不加**:真到"漏旧卡"要 tag 预筛、或真有理由 re-split kind,**那天加也不疼**。
- 唯一现在加的"新"字段 = `source`(因为 A4 近在路线图 + 是模型一部分)。
- 原则:**预留只在"贵 + 必来"时才值;JSON 让它不贵,所以 YAGNI。**

---

## 7. Memory Garden 改造(以最终模型为主导)

- **Garden 本质 = 用户的控制台**:既然系统不自动改 identity,**改的人是用户,在 Garden 改**——编辑 identity + 看/删/纠正 memory + (将来)标隐私。
- 改法跟模型:
  - **砍掉按 type 分的 3 tab**(故事/关于我/TA在想);TA在想先去掉(画像不做)。
  - 记忆视图 = 召回卡时间线/搜索,卡显示 `content(MD) + 日期 + emotion(浓度可视化) + source 图标 + decay(旧的淡化)`。
  - 关系视图 = identity,**用户可编辑**。
- **隐私 v1**:不做自动敏感门禁(`MEMORY_SENSITIVE_GATING_ENABLED` 默认 off,gate 代码休眠保留);用户控制 = Garden 里**删**(将来加用户手动"隐藏"开关)。
- **节奏**:v1 后端砍字段时**先把旧 Garden tab 隐藏/简化**(别显示坏卡);**完整 Garden 重设计 = schema 锁定后照模型做**(后端模型定 → 前端跟着长,不是前端先定样子)。

---

## 8. 延后(v1 明确不做)

**推理层(画像 + A4 统一)= agent 对用户的猜测**(聊天模式猜 + 屏幕模式猜),**不进 fact 层;v1 不做(要上线,先上扎实的事实+关系),和 eval 一起做(它是猜测,必须 eval 兜着防说错)——放上线后那一波**;tag/embedding(漏旧卡升级)、supersede/merge/decay 自动清理(逻辑后做)、自动敏感分类、proactive 填充。

---

## 9. 迁移 / 兼容

- **M2 MemoryCard v1 → v1 极简**:存量卡把可读字段(title/description/her_quote/summary)**合并进新 `content`(MD)**,补 `emotion/decay/source` 默认值;或在早期阶段直接以 v1 为新基线(看实际有多少存量)。**Codex 评估迁移成本。**
- **写入终点 `_execute_memory_action` 不推倒**——改的是它产出/接受的字段形,不是整条写入管道。
- **读写合同**随之简化(动作词、字段)。

---

## 10. 给 Codex 的核对点

1. 这个 v1 schema(8 字段)对照现有写入/读侧/capture/enclave item builder,**改造量多大、哪些必须改**?
2. 工具瘦身(砍 supersede/patch/retype/add_correction)对现有调用方有无破坏?route A/B 哪里在用这些?
3. **不预留字段(靠 JSON 随时加)**这个判断对吗?有没有哪个字段其实"贵+必来"、值得现在加?
4. 矛盾卡不做 supersede、靠日期 + agent 判断——可接受吗?还是至少留个极简 supersede?
5. 迁移:存量 M2 卡怎么过渡到极简 v1 最省事?
6. `emotion / decay` 由谁产、怎么算(capture 时模型打分?decay 后端按时间算?)——给个最简实现建议。
