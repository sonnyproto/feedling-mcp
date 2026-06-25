# IO Memory v1 · 结构定稿(merged:我们 v1 + Seven baseline)

> 2026-06-25 · 作者:CC(合并 Seven `结构基线` + hx 决定)· 状态:**v1 结构唯一真相**;2 处待 Seven 确认(§9)。
> 取代:`IO-memory-v1极简重做-给codex.md` 的"kind/emotion_weight/不做 supersede"部分、`IO-memory-结构方案-给seven确认.md`。冲突以本文为准。
> 配套:`IO-memory-read-write-contract.md`(读写规则,已同步本文)、`IO-memory-v1实施计划-test基线.md`(后端实施,基线 current test、干净 v1)、`IO-memory-v1-给zhihao交付-工具与调用流程.md`(给 zhihao)。

---

## 1. 三层模型

```
身份层 identity   独立、常驻每轮、用户控制、系统不改、不进卡库索引

记忆层 memory     一种卡(事件即记忆,不分 type);bucket 归类 + thread 横穿
  索引(轻,agent 平时扫):bucket / summary / threads / 视标
  内容(拣中才读):content
  ↑ 同一条 thread 把不同 bucket 的卡串成一条线(follow_thread)

推理层(deferred) agent 对用户的"猜测/画像"(聊天+屏幕都喂);v1 不做,和 eval 一起、上线后
```

---

## 2. 卡 schema(一种卡,统一格式,不分 type)

| 字段 | 层 | 是什么 |
|---|---|---|
| `id` | — | 卡 id |
| `bucket` | 索引 | **主话题(单选)**:`我们的关系`/`工作`/`妈妈`…。**平铺、几十个、超粒度、复用**(§9 待 Seven 确认平铺 vs 层级) |
| `threads` | 索引 | **线索(多选,string[])**:`工作压力`/`蛋子`/`冷战`…。**既是检索抓手,也是 follow_thread 横穿桶的连线**(thread ≈ tag,多选) |
| `summary` | 索引 | 一句话:这卡是啥(agent 一眼判断要不要进去读) |
| `importance` | 视标 | **0–1:看不看**(对长期理解用户多重要)。**写时模型打,客观、不随时间变** |
| `pulse` | 视标 | **0–1:回忆时的情绪强度/激活度**。**只影响 agent 表达色彩,不进检索排序**(§9 待确认) |
| `status` | 视标 | `active`/`superseded`/`archived`。非 active 默认不返回 |
| `source` | 视标 | `chat`/`screen`(grounded 出处;推理/猜测不在这,进推理层) |
| `occurred_at` | 视标 | 发生/创建时间 |
| `last_referenced_at` | 视标 | 上次被用到的时间;**`decay` 读时从它派生,被再用就更新(回升)** |
| `content` | 内容 | 一段事件正文(MD:记忆 / 上下文 / 使用提示);fetch 才返回,index 不返回 |

**排序(读时算)**:`decay = clamp((now - last_referenced_at)/half_life, 0,1)`;综合 ≈ `相关性 × importance × (1-decay)`。half_life 初值:relationship-ish 30d / fact-ish 90d;`importance≥0.8 → half_life×2`。`pulse` 不参与排序。
`last_referenced_at` **只在**:fetch 后真进 prompt / recall 真注入 / agent 明确用它答 —— 才更新(**扫目录不算**)。

---

## 3. 存储(就是俩字段 + filter,别想复杂)

- `bucket` = 一个字符串;`threads` = 字符串数组。**没有图/外键。**
- **跨桶关联是"不同卡挂了同一 thread 值"自然冒出来的**,`follow_thread(X)` = `where X in threads`(跨 bucket)。
- **resolve-before-create(命门)**:写卡时把**用户现有 bucket 列表 + thread 列表喂给模型**,逼它复用现成的,实在没有才新建。**结构简单,难的是词表别膨胀/分裂。**
- 需要一个小能力:`GET /v1/memory/buckets|threads`(或从现有卡聚合)给写入提示用。

---

## 4. 检索(agent 驱动,不硬截)

- **identity** = 每轮 push(常驻)。
- **底色 ambient(气氛灯)= runtime push,不是 agent 查**:每轮带几条 **最近 + 高 importance**(任意 bucket;不给关系桶开小灶,"最近的也最贴近最近关系")。
  - **能力(hx 提供)**:`index` / `search()` **不传 query、按 importance×recency 排序、取 top-N**——runtime 每轮调它推底色。便宜、可缓存、保持人设连续(闲聊也带)。
- **agent 自己查(agent-first)**:agent 选 `bucket` / `thread` 去 `index` → 看目录(summary/threads/视标,**不含 content**)→ 挑 1-3 张 `fetch` 取正文 → 需要时 `follow_thread` 跨桶串。**该查 agent 自己调,闲聊不调。**
- **index 默认全返回(无 limit 旋钮)**:目录轻(无 content),agent 自己扫挑;**大了靠 bucket/thread 收范围,不盲截**(盲截会漏卡)。只留一个不可见安全上限防极端 dump。气氛灯 ambient 的 top-N 是它自己的,≠ index。
- **去重**:一张卡可能被多条路命中(桶 / thread / 最近),最后去重。
- **不做 recall 兜底 / preflight / should_read**:我们默认 agent 会 call tool,该查自己 `search→fetch`;recall 以后或作"省 token 捷径",非兜底。

---

## 5. 写入(resolve-before-create + importance/pulse + supersede)

- **何时写**:只记长期有用;不记闲聊/临时情绪/玩笑/角色扮演/未确认猜测/只是引用已有。
- **事件即记忆,不分 type**:记一件完整的事(人/事/情绪/过程),fact/relationship/insight 用 thread 贴标签,不做卡 type。
- 输出 `memory.add`:`bucket`(复用现有)+ `threads`(复用现有)+ `summary` + `content`(MD三段)+ `importance` + `pulse` + `source`。
- **纠错 = `memory.supersede`(soft)**:旧卡转 `status=superseded`、链到新卡、**永不硬删**;新卡继承旧卡的 bucket/threads(同一主题)。
- **delete**:仅用户明确要"删/遗忘"才真删。
- 别说"已记好"(异步)。

---

## 6. 提示词(怎么约束 agent)

**写入指引**(关键:把现有桶/线塞进去逼复用):
```
判断这轮有没有值得长期记的事。不记:闲聊/临时情绪/玩笑/角色扮演/没被确认的猜测/只是引用已有。
要记 → memory.add:
- bucket(选1,主话题):优先从现有桶选 {现有bucket列表};没有才新建,克制别造近义。
- threads(选多,1-4):优先从现有线选 {现有thread列表};同一条线一个名(蛋子≠狗狗)。
- summary 一句话;content MD三段(记忆/上下文/使用提示)。
- importance 0-1(多重要,不是多激烈);pulse 0-1(当时情绪多强)。
- 纠正旧事实 → memory.supersede(target=旧卡id),不硬删。
不要说"已记好"(异步)。
```
**读取指引**:
```
长期记忆可能相关时:
1. 选 bucket(这轮聊哪个话题)→ memory_search(bucket=该桶)看目录。
2. 看 summary/threads 挑 1-3 张 → memory_fetch 取正文。
3. 想搞清来龙去脉 → follow_thread(某thread)跨桶串。
4. 没命中别编。会话开始已带几条底色(最近+高importance)。
```

---

## 7. 平铺 vs 三层(为什么 v1 选平铺)

| | 平铺(推荐) | 三层 |
|---|---|---|
| 卡放哪 | 选 1 个粗桶 + 挂 thread | 定 3 级路径,每级都纠结 |
| 分裂 | 桶层一次 | **每级都分裂**(冲突/矛盾/沟通…) |
| 深度 | 一致 | 凑不齐(蛋子硬塞 3 层) |
| 细分 | **thread 给(多选+横穿,更强)** | 靠层级(固定、僵) |
| 维护 | 合并两桶=改 bucket | 合并子枝=改整条路径 |
→ **平铺 + thread 覆盖三层想要的一切,且无放置纠结/每级分裂/难维护。** 真要层级:用**路径字符串**(`a/b/c` 一个字段、前缀筛),不建树表。

---

## 8. 延后(v1 不做)
- **推理层 / 画像**(聊天+屏幕的猜测,统一一层)—— 和 eval 一起、上线后。
- **"做梦"**:合并近义 bucket/thread、退化 dormant、抽象成新卡、提醒 open_thread —— 加法+闸门、永不删。
- **感知三层**:基线档(TEE 滚动统计、不落卡)→ 显著偏移才进事件卡 → 跨时间模式刷新(A4)。
- **embedding**(语义召回升级)、自动敏感分类、Garden 字段(等 UI 再定)。
- (注:tag 已经以 **thread** 形式纳入 v1,不再单列。)

---

## 9. 待 Seven 确认(只剩 2 个)
1. **bucket 平铺 vs 层级**?baseline 写了"三层"又写"平铺/几十个"——我理解"三层"指**整体架构三层**(身份/索引/内容),**bucket 本身平铺**。建议平铺(§7)。**确认?**
2. **pulse 进不进检索排序**?你说"不影响看不看",我定成 **pulse 只影响表达色彩、不进排序**(排序只 importance×recency×activation)。**对吗?**
> 其余已对齐:importance/pulse 拆、supersede soft、底色=最近+高importance、bucket 单选 + thread 多选 + 写时复用现有词表、limit 可配。

---

## 10. 实施(指向)
能力/编排/死代码三分、纯净分支、迁移(旧 M2 卡 adapter→bucket/thread,默认归桶策略)、测试 —— 见 `IO-memory-v1实施方案-给codex-纯净分支.md`(需按本文把 kind→bucket/thread、emotion_weight→importance/pulse、supersede 回归 同步)。读写合同 `IO-memory-read-write-contract.md` 同步。
