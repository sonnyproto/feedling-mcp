> ⚠️ **已被取代** —— 见 **`IO-memory-子系统-spec与plan-定稿v1.md`**(冲突以定稿为准)。本文保留备查。

# IO Memory 统一架构 Spec(route A/B 收敛)

> 2026-06-23 · 作者:Claude(CC) · 状态:**v2 · Codex 已 review · plan-ready**(配套执行 plan 见 `IO-memory-统一架构-plan.md`)
> 这是一份**架构方向 spec**。目标是把"记忆"在所有接入路径、所有来源上**收敛成一条写、一条读**。v2 已折叠 Codex review 的采纳点(见 §0.4)。

---

## 0. 背景与动机(请先读,Codex 理解上下文用)

### 0.1 为什么做(领导的驱动)
**xyn 认为 route A(VPS/自建)和 route B(API/托管)两套路径差异已经太大,"像是两个不同的 app 了"。** 记忆逻辑在两条路上各长各的——提取各写各的、动作协议各有一套、route A 甚至没接召回。这份 spec 的唯一目标:**收敛差异,让记忆变成"一条写管道 + 一条读管道,所有来源都汇进去"**。一个词:**统一**。

### 0.2 已经做到哪了(已完成的前序工作)
- **M1**:readside 召回拆成 `index → fetch`,enclave 内解密。✅ 上 test。
- **M1.5**:agent 把记忆当工具(prompt-level);模型不调时服务端算法兜底,不丢召回;修了"模型被自己错话带偏"的召回 bug。✅ 上 test。
- **M2**:写入闭环——MemoryCard v1(card_v/status/salience/importance/supersedes/superseded_by/is_sensitive + summary/verbatim/follow_up)、双写 legacy、insert、supersede(软退场永不硬删、两卡原子)、协议从 prompt→coerce→executor 打通。✅ 代码+测试完成(分支 `feat/memory-m2-write-loop`),待合 main。
- **M3**:记忆"质量"(该不该记、eval),**未开工**。起点是一个已发现 bug:自然陈述的持久事实(养了狗叫蛋子)没被捕获——controller 偏"偏好"漏"事实"。

### 0.3 这份 spec 和 M3 的关系
M3 是"调判断力";**本 spec 是"收敛管道"**。两者交叉:收敛后的"propose(该记什么)"正是 M3 调的对象。建议:**先收敛管道(本 spec)→ 再在统一的 propose 上做 M3 调参**,否则会在两套提取逻辑上各调一遍。

### 0.4 Codex review 采纳点(v2 折叠,CC 终审认可)
Codex review 后,以下调整已折叠进本 spec + plan:
1. **共享"契约"而非"函数"**:route A consumer 是独立进程,**不强行 import 同一个 Python function**;共享的是 **action schema + prompt 模板 + coerce 规则 + card contract + eval fixtures**(数据/规则,不是代码)。(改 §3.1、§7)
2. **context assembler 做成后端能力**:抽成一个后端接口(`query + route → assembled context block`),route A/B 都调,**不让 consumer import `hosted/context.py`**。(改 §3.2、§7)
3. **route A 召回第一阶段先服务端注入,不先 tool-loop**。(§3.2 已是此口径)
4. **durable facts 口子在收敛 propose 时就打开**(不等完整 M3)。(§7、§9)
5. **感知进长期后置**(M3 之后);**MemPalace 不进主链路**,只作 post-M3 离线 POC。(§5、§8)

**CC 终审追加的 4 条护栏**(plan 必带,见 §9):
- (G1) Phase 1 改 route A coerce **必须回归测 route A 现有流程**,别搞坏现跑的 identity/memory 动作。
- (G2) Phase 1 给 route A 接召回注入 = route A 的**真行为变化**(原来只用 agent 自己记忆)→ 冒烟 + 盯 prompt token 膨胀。
- (G3) **"propose 跟着谁是 agent 走"**:共享的是规则文本;**不要把"服务端替 route A 跑 propose"做进去**(那是多一个 LLM,违背 route A 自带算力)。
- (G4) MemPalace POC = **条件性/可选**(M3 后召回成瓶颈才做),非必交付。

### 0.5 对齐 xyn runtime 统一(v3,2026-06-23)
本 spec **降级为"记忆子系统视角"**,顶层以 xyn 的 `RUNTIME_UNIFICATION_SPEC` + `RUNTIME_ACCEPTANCE_REQUIREMENTS` 为准。记忆是 xyn 的 **`build_companion_context`(identity+memory+screen+recent+pending)** 里的一个零件,不再自成一套。对齐清单:

**① 缺、要补进本 spec:**
1. **唤醒(proactive)时的记忆**:本 spec 原来只想了"聊天召回",**没想唤醒**。读侧必须同时服务**聊天 + 唤醒**(对应 xyn P3:唤醒现在硬编码 `memory_count=0`,要真填)。
2. **屏幕→记忆(感知沉淀)提前**:本 spec 原把它放 P4/M3 后;xyn 放 **Phase A(A4,分给 hx),现在做**(跨天逛街例子是核心验收)。
3. **记忆要和 screen/identity 在同一个 context**:不再是独立召回管道,是 `build_companion_context` 的 memory 输入,和屏幕/身份平级。
4. **常驻层**:读侧长期 = `常驻(identity / pinned)+ 召回`,不只召回(详细设计 parked,见 `IO-runtime统一-CC对齐` §6)。

**② 要改:**
1. 我们的"上下文拼装器"**并进 `build_companion_context`**,不自成一套。
2. **selector 后端化、一套**:`/v1/memory/recall` 给**聊天 + 唤醒、A + B 全用同一个**(consumer 不再自己 topK)。
3. 读侧模型 `短期 + 长期(召回)` → `短期 + 长期(常驻 + 召回)`。
4. **送达 = consumer-push 默认开**(见下"方式")。

**③ VPS agent 用工具的最终方式(双层)**:
- **底座 = consumer-push(保证)**:consumer 每轮把 `build_companion_context` 推进甩给 agent CLI 的消息——不管 agent 会不会调工具,一定看到。
- **增强 = MCP-pull(best-effort)**:把**新的** `feedling_memory_index/fetch` 配进用户 agent runtime(onboarding `claude mcp add`/Hermes)+ skill 叮嘱,强 agent 自己深挖。
- ⚠️ **真 gap**:onboarding 现在配的是**老 memory 工具**(add_moment/list),新的 index/fetch 要配进去,pull 这层才真能用。
- 写:agent 发动作 + 周期 capture 兜底(不变)。

**④ 对齐了不用动**:写入(M2 commit)、共享契约不跨进程 import、MemPalace 不进主链路(=xyn P9/C2)、弱模型服务端兜底(=xyn B1)。

**⑤ 归位到 xyn 的 Phase**:本 spec 的 readside+recall = **A1 的 memory 部分**;感知→记忆 = **A4**;agentic+tool-loop = **B1**;写入/质量 = A4/后续。(hx 负责 A1+A4)

---

## 1. 核心论点(thesis)

**记忆 = 一条写管道 + 一条读管道,由多个来源喂入;来源相关的差异全部压到边缘的"薄适配器"里。**

```
       来源(薄适配器,只产候选)            共享管道
  ┌─ chat(API / route B)──┐
  ├─ chat(VPS / route A)──┤
  ├─ 录屏 screen ──────────┼─►[ PROPOSE ]─►[ COMMIT ]─► IO 记忆库
  ├─ GPS / 系统 ───────────┤   "该记什么"判断  _execute_     (memory_moments)
  └─ 感知 perception ──────┘   + 一套 coerce    memory_action       │
                                                                    │
  任意来源的 query ─►[ 上下文拼装器 ]─► prompt ─► 模型 ◄────────────┘
                    短期层 + 长期层(召回)
```

---

## 2. 现状:已统一 vs 没统一(grounded,带代码位置)

| 环节 | 状态 | 证据 / 位置 |
|---|---|---|
| **写入 commit** | ✅ **已统一** | 唯一入口 `memory/actions.py:_execute_memory_action`;route B(`hosted/turn.py:401/975/1401/1575`)+ route A(consumer `→/v1/memory/actions`,`memory/routes.py:189`)全走它 |
| **动作协议(coerce/normalize)** | ❌ **重复两套** | 后端 `hosted_runtime.py:coerce_runtime_action`;consumer 自有 `_normalize_v2_action_type`(`chat_resident_consumer.py:2272`)、`_proactive_action_type`(:2640)——会漂移 |
| **propose(该记什么)** | ❌ **碎成 2–3 处** | 后台 controller `hosted_runtime.py:build_background_execution_messages`(:170)有一套标准;capture `model_api_runtime/prompts.py:build_memory_capture_messages`(:77)又一套;route A 靠用户 agent(不受控)。**狗 bug 住这** |
| **读召回 recall** | ❌ **route A 没接** | consumer 只调了 `/v1/memory/actions`,**没调 index/fetch** |
| **上下文拼装** | ❌ **A/B 各拼各的** | route B `hosted/context.py`(identity+recent+召回+screen+pending);route A consumer 自己拉 history+screen,**且无长期召回** |
| **感知 → 记忆** | ❌ **只到短期** | `perception/service.py` 采集屏幕/GPS(`location_signal`)→ 触发 wake + 当前屏幕临时注入;**不写 memory_moments** |

> 结论:**"终点(写 commit)"早就统一了;没统一的是上游 propose/coerce、读召回(route A 缺)、上下文拼装(A/B 分叉)、以及感知没接进来。**

---

## 3. 目标架构

### 3.1 写侧(write pipeline)
```
来源适配器 → PROPOSE(共享) → COERCE(共享) → COMMIT(已统一) → IO 记忆库
```
- **COMMIT**:`_execute_memory_action`(insert/supersede/...),**已统一,不动**。
- **COERCE**:一套动作协议(把"agent/controller 产出"规整成 executor 动作)。**统一的是"契约"(action schema + 字段规则 + conformance tests),不是强行 import 同一函数**——route A consumer 跨进程,**可保留独立实现,但必须过同一组 schema/coerce conformance 测试**,以此消除漂移。(§0.4-1)
- **PROPOSE**:一套"该记什么"判断 + 提取。**收敛的是规则文本(prompt 模板 + 捕获标准),route A/B 共用同一份规则、由各自的 agent 执行**(见 §3.3)。**(顺带:把捕获标准从"偏好"扩到"durable facts",修狗 bug——收敛时就开这个口子)**
- **来源适配器**:每个来源把原始信号转成"候选",`source_type` 标来源(chat/screen/gps/perception)。**M2 已有 source_type 字段。**

### 3.2 读侧(read pipeline = 上下文拼装器)
**读侧不只是"召回",是一个两层的上下文拼装器,route A/B 共用:**
```
query → [ 上下文拼装器 ]
          ├─ 短期层(working,每轮现拼,不持久):最近对话 + 当前屏幕/GPS(感知)+ pending
          └─ 长期层(persistent,召回):index → select → fetch 的记忆卡
        → 按统一的顺序 / token 预算拼成 prompt → 模型
```
- **拼装器做成后端能力**(§0.4-2):抽一个后端接口 `query + route → assembled context block`,route A/B 都调,**不让 consumer import `hosted/context.py`**(跨进程会脆)。内部再复用现有具体函数。
- **route A 接上长期层**:consumer 经 HTTP 调这个拼装器 / `/v1/memory/index|fetch`(**不走 MCP**——MCP 是已弃用的 route C;这些是普通后端 HTTP 路由)。**第一阶段先"服务端注入"(算法选择器,不起 LLM),tool-loop 后置**。
- **#7 提示词拼接优化** = 就是把这个拼装器收敛成一处,统一短期/长期的拼法、顺序、token 预算。

### 3.3 谁来 propose:跟着"谁是 agent"走(重要原则)
- **route B**:服务器就是 agent → 服务端 controller 做 propose(本就只有这一个 LLM,无额外开销)。
- **route A**:用户自己的 agent(通常较强)→ **让它的 agent 做 propose**(发 memory 动作走已有写通道),**不在服务端为 route A 再起第二个 LLM**(否则违背 route A"自带算力"前提)。
- 即:**propose 的"判断规则/prompt"共享一套;但"由谁执行这套判断"按路线走**(B=服务器,A=用户 agent)。读侧的短期/长期拼装则两边都走共享拼装器。

---

## 4. 短期 memory vs 长期 memory(必须显式分层)

| | 短期(working) | 长期(persistent) |
|---|---|---|
| 是什么 | 每轮现拼的工作上下文 | 记忆卡库 memory_moments |
| 内容 | 最近对话 / 当前屏幕 / GPS / pending | 召回回来的卡 |
| 生命周期 | 用完即弃,不入库 | 持久、可召回、可 supersede |
| 谁产 | 上下文拼装器(短期层) | 写管道(propose→commit) |

**感知现在只活在短期层**(当前屏幕这轮注入)。本 spec 要把感知**也接到长期层**(见 §5)。

---

## 5. 感知系统(perception)是什么 + 怎么接进记忆

### 5.1 它是什么(Codex/团队对齐用)
`backend/perception/` = IO 的**传感层**:`ingest_snapshot_v2` 采集屏幕帧、照片、**GPS(`location_signal`)** 等信号 → ① 加密存(帧在 `frame_envelopes`)② 有变化触发 **wake**(叫醒 proactive agent)③ 当前屏幕临时注入聊天。**GPS 已经在采,只是没沉淀。**

### 5.2 现状 vs 目标
- **现状**:感知 → 短期注入 + wake;**不进长期记忆**。
- **目标**:感知 → **提炼(distill)→ 长期记忆卡**,走统一写管道(source_type=screen/gps/perception)。
  - 例:"用户 3 天内多次浏览某商品 → 可能想买"——只有沉淀成卡才捕捉得到跨时间信号。
- **难点(必须正视)**:
  1. **跨时间聚合**:要周期任务扫一段时间的帧/信号,不是单帧、不是实时。
  2. **意图推断假阳性高**(反复看 ≠ 一定想买:广告?别人屏幕?)→ 严格 eval 卡、克制、周期跑。
  3. **隐私更重**:屏幕/位置是环境信息(用户没主动说),比聊天敏感;`screen_caption_enabled` 默认就是关的。**需明确同意 + 用户可见可控 + 标 is_sensitive**。
- **结论**:感知→长期是**一个新来源适配器**(挂在统一写管道上),**但排在管道收敛 + M3 写判断成熟之后**(推断更虚、隐私更重)。

---

## 6. 领导 8 件事 → 归位

| # | 事 | 归到架构哪 |
|---|---|---|
| 2,8 | VPS+API 统一 / 统一 A/B 逻辑 | **核心**:§3.1 收敛 propose+coerce + §3.2 接 route A 召回 |
| 6 | 读写独立管道、来源都走同一条 | **就是 §1 架构本身**(thesis)|
| 3,4,5 | 录屏 / GPS / 感知 进记忆 | **同一桶**:§5 感知来源适配器 → 统一写管道(GPS 已在采,缺沉淀)|
| 7 | memory 提示词拼接优化 | §3.2 上下文拼装器收敛成一处 |
| 1 | eval 用 MemPalace | §8,有保留(单独评估)|
| — | 短期 memory(本轮补充)| §4 显式分层,短期层进上下文拼装器 |
| — | VPS 上下文拼接(本轮补充)| §3.2 route A/B 共用同一个拼装器 |

---

## 7. 关键收敛点(plan 阶段要落的"共享模块")

1. **共享 `coerce` 契约(非 import)**:统一一份 **action schema + 字段规则 + conformance tests**。route A consumer **可保留自己的实现**(它跨进程),但**必须过同一组 schema/coerce conformance 测试**。**不要写成"consumer 直接 import 后端 `coerce_runtime_action`"**(与 §3.1 一致)。
2. **共享 `propose` 规则文本(非 import)**:把 controller 标准 + `build_memory_capture_messages` 收敛成一份"该记什么"规则文本(含扩到 durable facts);A/B 共用**同一份规则**,由各自 agent 执行。
3. **route A 接长期召回**:consumer → HTTP `/v1/memory/index|fetch`;先注入(无 LLM),后 tool-loop。
4. **共享上下文拼装器**:短期层(recent/screen/gps/pending)+ 长期层(召回),A/B 同一套,统一顺序/token 预算(= #7)。
5. **感知来源适配器**:感知信号 → 提炼候选 → 统一写管道(后续、eval 卡)。

> COMMIT(`_execute_memory_action`)已统一,不在收敛清单内——它是这套架构能成立的地基。

---

## 8. 关于 eval / MemPalace(有保留)

- **MemPalace 是检索库(hybrid retrieval),不是 eval 框架。**
- 若当 **eval 工具**:不匹配——eval = 一组 probe + 打分器(小段自有代码),不需要引记忆库。**建议自搭。**
- 若当 **检索/召回引擎**(embedding/向量):是"召回升级"话题,**硬门槛 = 能否在 enclave 内运行、符合加密/隐私模型**;且是 **post-M3** 的事。
- 行动:eval 自搭;MemPalace 作为"召回规模化"的候选**单独评估**(我可以拉 repo 核 license + enclave 适配,给硬结论)。

---

## 9. 约束 / 不破坏(guardrails)

- ✅ 不动写 commit(`_execute_memory_action`)、M2 的 MemoryCard v1。
- ✅ supersede 永不硬删;写入双写 legacy(title/description/her_quote)保 iOS Garden。
- ✅ enclave 隐私边界不变(明文只能看元数据,内容只在 enclave/客户端解密);感知进长期要更强同意/可见控制。
- ✅ 不破坏 M1/M1.5 已上 test 的能力;不走 MCP(route C 已弃用)。
- ✅ route A 不在服务端为它多跑 LLM(propose 由其 agent 做)。
- **(G1)** Phase 1 改 route A coerce **必须回归测 route A 现有流程**(现跑的 identity/memory 动作别搞坏)。
- **(G2)** Phase 1 给 route A 接召回注入 = route A 真行为变化(原来只用 agent 自己记忆)→ **冒烟 + 盯 prompt token 膨胀**。
- **(G3)** **propose 跟着"谁是 agent"走**:共享规则文本;**不要做"服务端替 route A 跑 propose"**(= 多一个 LLM)。
- **(G4)** **MemPalace POC 条件性/可选**(M3 后召回成瓶颈才做),非必交付。

---

## 10. Codex review 已解答的问题(记录)

以下 §1 的开放问题,Codex review 已给方向(折叠进 §0.4 / plan):
1. 共享 propose/coerce 放哪 → **共享契约(schema/prompt/规则),不强行 import 同一函数**(跨进程)。
2. route A 召回先注入还是 tool-loop → **先服务端注入**。
3. 上下文拼装器能复用多少 hosted/context.py → **抽成后端能力,A/B 都调,不直接 import**。
4. 感知 distiller 归谁 → Phase 4 再定(proactive 周期任务为候选)。
5. 短期/长期顺序与 token 预算 → Phase 1 定义(#7)。
6. durable facts 是否收敛时同步开 → **是,收敛时就开口子**。

---

## 11. 一句话

> **领导要的"统一"= 别让 route A/B 像两个 app。落到代码:写侧收敛成 propose+coerce+commit(commit 已统一,补共享 propose/coerce);读侧收敛成一个两层上下文拼装器(短期 + 长期召回,route A 接上长期);感知/录屏/GPS 作为来源适配器挂上统一写管道。eval 自搭、MemPalace 当召回候选单独评估。先收敛管道,再在统一 propose 上做 M3。**
