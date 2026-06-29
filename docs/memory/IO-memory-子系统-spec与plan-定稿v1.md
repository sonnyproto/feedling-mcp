# IO Memory 子系统 · Spec + Plan(定稿 v1)

> 2026-06-23 · 作者:Claude(CC) · 状态:**定稿,待 Codex 全局 review**
> **这份是 memory 子系统的唯一真相源**,合并 spec + plan。以下旧文档**全部降级为历史/被取代**(内容已收进本文,冲突以本文为准):
> `IO-memory-统一架构-spec-给codex-review.md`、`IO-memory-统一架构-plan.md`、`IO-runtime统一-CC对齐(对xyn两份doc).md`、`IO-memory-P1-工程执行计划-Codex版.md`、`IO-memory-P1.5-读侧统一疑问与方案-Codex.md`。
> **顶层框架(非本文,必读)**:`docs/memory/RUNTIME_UNIFICATION_SPEC.md` + `docs/memory/RUNTIME_ACCEPTANCE_REQUIREMENTS.md`(**xyn** 发的 runtime 统一)。memory 子系统是它的零件。

---

## 0. ⚠️ 给 Codex 的两个硬要求(先读)

### 0.1 全局 review(防止再漏 —— 这是重点)
上一轮发生过**重大遗漏**:route A 的"读"一直是**老机制(服务器关键词选 + 塞 chat_get_history)**,而不是设计本意的 **agent 语义优先**——而且新 readside(index/fetch)根本没接进 route A。原因是内容散在十几份 md、没人做全局核对。**不许再发生。** 所以 Codex 这次必须:

1. **review 全部 `docs/memory/*.md`**:本文与其它 md 是否一致、有没有互相矛盾、有没有遗漏的功能点。
2. **review 三个 repo 的代码**,逐条对照"功能点清单(§7)"与 xyn RUNTIME doc 的 P1–P9 / Phase A-C:
   - **feedling-mcp**(后端):`memory/`(routes/actions/service/readside_core)、`enclave_app.py`、`hosted/`(context/turn/chat_routes)、`hosted_runtime.py`、`proactive/`、`perception/`、`tools/chat_resident_consumer.py`、`mcp_server.py`。
   - **feedling-mcp-ios**:Garden 解码、identity/memory 展示(`MemoryViewModel` 等)。
   - **io-onboarding**:`skill.md` / `skill-resident-agent.md` 等(route A 的工具集 + 读/写 skill)。
3. **产出一张 gap 表**:每个"需求/功能点"→ 对应代码在哪 / 没有 → 缺口。**doc 有但代码没有、代码有但 doc 没提,两个方向都要查。**

### 0.2 review 时不能缺失
本文每个功能点(§3 读 / §4 写 / §7 清单)Codex 都要确认"是否覆盖、放哪个 Phase、有没有遗漏"。**缺内容/功能点 = review 不通过。**

---

## 1. 背景与上下文(读懂用)

- **两个老板**:**Seven**(早期 memory reframe + "把 memory 做成 agent tools、agent 自己决定何时查"的方向)、**xyn**(runtime/context 统一:`build_companion_context`、VPS 优先、"两套路径像两个 app"、Phase A/B/C)。
- **已完成**:M1 readside(index/fetch,enclave 解密,上 test)、M1.5(agent tools 优先 + 服务端兜底 + 修"被自己错话带偏"bug,上 test)、M2(写入闭环 insert/supersede,分支已完成待合)。M3(质量/eval)未开工。
- **本质问题(xyn)**:VPS 用户(自带 Cloud Code/Codex)和 API 用户在后端走两套代码,体验不一致,像两个 app。目标:**让用户自己的 agent 能稳定、自然地用上 IO 的屏幕/记忆/身份**。
- **route A 接入真相**(关键):VPS 平常聊天 = **consumer(HTTP 转发)** poll→甩给 **agent CLI**→写回;**agent 直连 IO 的 HTTP 端点(可选、best-effort)**读写记忆/身份。consumer 不居中调度 agent 的工具调用。详 `IO-memory-routeA-接入真相...md`。
- **⚠️ 重大现实修正(2026-06-23 全局 review + git 史确认)**:`feedling-mcp` 的 **MCP server 已被整个删除**(commit `e4c8bbc`,zhihao,2026-06-12,删了 `mcp_server.py` + 整个 `backend/mcpsrv/` 包,含 `tools_memory.py`),**架构已转纯 HTTP/REST**。所以**本定稿早先所有"agent 经 MCP 调 index/fetch / 往 MCP 工具集加工具"的表述一律作废,改为 agent 直连 HTTP 端点**(`/v1/memory/index|fetch|recall`)。onboarding skill 里残留的 `feedling_memory_*` MCP 工具引用也都 stale,需一并清。**决策(用户拍板)**:route A 读 = **agent 优先、直连 HTTP**(consumer 仅兜底);见 §3。

---

## 2. 顶层归位:memory 是 `build_companion_context` 的零件

- xyn 的 **`build_companion_context`** = 把 `identity + memory + screen + recent + pending` 组装成一份 context,**VPS/API/proactive 共用一个函数**(现在各拼各的)。
- memory 子系统 = 它的 **memory 那格(A1)** + **屏幕→记忆(A4)**。
- **memory 召回 = agent 优先**(agent 语义挑为主)+ 小 baseline 兜底;`build_companion_context` 里 identity/screen/recent/pending 是 push(常驻/短期),只有 **memory 召回这一层是 agent 优先**。
  - 说明:agent 优先**早就定了**,而且这正是 xyn 文档要求的**"两 route 行为一致"**(route B 已经是 agent 优先,route A 也要一致)——**不是改 xyn 的 spec**,是把它的"一致"要求落到 memory 召回上。xyn spec 里给的数据结构只是参考(见 §下)。

---

## 3. 读侧设计(三层 + 两 route 一致)

> 📜 **读写的基础行为规则(何时读/fetch、敏感 gating、何时 create/supersede/patch、"已记好"等)以 [`IO-memory-read-write-contract.md`](IO-memory-read-write-contract.md)(NORMATIVE)为准。** 本节只讲**架构与计划**,不复述规则细则;冲突以合同为准。

### 3.1 三层(build_companion_context 的内部分层)
```
常驻层(always-on, push):identity + (将来)pinned 核心卡   ← 不看 query,每轮带
召回层(agent 优先):memory 召回                          ← agent 语义挑(主)+ 服务端 baseline(兜)
短期层(working, push):screen / GPS / recent chat / pending
```

### 3.2 两条 route 的读(行为要一致)
| | route B | route A |
|---|---|---|
| 召回主路径 | agent 调 memory 工具(语义)| agent **直连 HTTP `index`→自己语义挑→`fetch`**(MCP 已删)|
| 召回兜底 | 没调 → 服务端关键词召回 | consumer 调 **`/v1/memory/recall`** 推小 baseline(服务端 selector)|
| 常驻/短期 | 服务端 push(`_model_api_context_messages`)| **consumer push**(默认开)|

> **⚠️ 机制不对称是故意的,别"修平"**:两 route 的**行为一致**(都 = agent 语义优先 + 算法兜底),但**机制本就该不同**:
> - **route B 看得见**(hosted runtime,loop 在自己手里,能解析 LLM 的 `tool_calls`)→ **条件式串行**:先让 agent 调工具,**没调/调空才注入算法兜底**(现已实现:`hosted/chat_routes.py` 解析 tool_calls + `_memory_fallback_instruction_message`)。不重复、不费冗余 token。
> - **route A 看不见**(consumer 把整轮甩给 agent CLI,且 baseline 必须在 agent 跑之前注入)→ 只能**无条件并行**:always 推小 baseline 当 floor,agent 再自己深挖。**这点冗余是它"看不见"被迫付的税**,靠"baseline 保持小 + skill 告诉 agent 可能已带几条、只在要更深时再 fetch"来压。
> - **结论**:一致 = "agent 挑不出来时都有算法兜底",**不是**"两条都 always 双推"。**别让 route B 跟着 route A always 双推**——那是白烧 token,route B 没有 A 的"瞎",不该付那份税。

**两个端点分工(关键,别混)**:
- **`index` + `fetch` = "agent 自己挑"** → 给 route A 主路径(agent-first):agent 先 `index` 看摘要、**用自己的语义判断挑**、再 `fetch` 取正文。agent 优先的价值就在这一步是 agent 在挑,不是服务端在挑。
- **`/v1/memory/recall` = "服务端 selector 替你挑"** → 给 **consumer 兜底 / proactive 唤醒 / route B fallback**(这些路径里没有 agent 在 loop 里挑)。

**要做的(读)**:
1. **route A 主路径改成 agent 直连 HTTP `index`/`fetch` + 改 read skill**(从"用 context_memories"改成"先 `index`、看摘要、语义挑、再 `fetch`";用 agent 自己的 HTTP/curl 能力 + API key,**不是 MCP 工具**——MCP 已删)。← 修上轮重大遗漏。同时清掉 skill 里 stale 的 `feedling_memory_*` MCP 引用。
2. **`/v1/memory/recall`(后端共享 selector)**:consumer 兜底 + **唤醒** + route B fallback 全用同一个(consumer 不再自己 topK)。
3. **唤醒(proactive)读填 memory + identity**(xyn P3:现在硬编码 `memory_count=0/identity_loaded=false`,要真填)。
4. **consumer-push 默认开**:route A 因为 consumer 甩给 CLI、看不见 agent 是否调工具,所以底座是"每轮推小 baseline",agent 再自己深挖。
5. **召回窗口可配置**:`FEEDLING_MEMORY_READSIDE_LIMIT` 默认 50 / 可配 / `0`=全开 + `HARD_MAX`;`enclave moments[:50]` 两处硬编码必须一起改。

### 3.3 送达机制(说清,别做错)
- **route B**:服务端 in-process 组装 + 调 enclave 解密 → 拼进 prompt。
- **route A**:**主**=agent **直连 HTTP** `index`→自己挑→`fetch`(pull,agent-first);**兜底**=consumer 经 HTTP 调 `/v1/memory/recall` → 注入 agent CLI 的消息(push)。**MCP 通道已删(e4c8bbc),两条都走 HTTP。**

### 3.4 key 与接入(MCP→HTTP 的落地,已厘清)
- **只有一个 per-user key:`FEEDLING_API_KEY`**(老 MCP key 本就默认用它,见 `chat_resident.env.example`)。**不需要新 key**。onboarding 发(`POST /v1/users/register`)→ 用户在 iOS Settings 复制 resident consumer config(`FEEDLING_API_URL`+`FEEDLING_API_KEY`)→ 配进 consumer。所有 HTTP 端点鉴权统一 `X-API-Key: <FEEDLING_API_KEY>`。
- **HTTP 和 MCP 功能等价**,唯一实质差别:**MCP 把工具 schema 自动喂给模型(可被发现),HTTP 不会**。→ **agent 不会凭空知道 `/v1/memory/*` 存在**,所以"会不会发生 agentic 召回"**全靠 onboarding/skill 把"端点 + 鉴权头 + 何时调"讲清楚**。这是现在唯一的承重点(MCP 时代靠自动发现兜底,已没了),也是验收要盯的(见 §6:看后端端点命中日志,证明真有 agent 在调,而非只剩 baseline)。
- **agent 怎么拿到 key —— 按接入方式分**:
  - **CLI 模式**(consumer 用 `subprocess.run` 起 `hermes`/`claude --print`,`chat_resident_consumer.py:2109` **不传 `env=`**)→ 子进程**继承 consumer 的 env**,`FEEDLING_API_URL`/`FEEDLING_API_KEY` **已经在**。**开箱即用,只改 skill 文案。**
  - **HTTP 模式**(agent 是独立服务)→ 不继承,需在 agent 服务自己 env 配 `FEEDLING_API_KEY`(onboarding 的 config 已给这俩值,让服务读到即可)。**与 MCP 差不多,onboarding 约束好就行。**
  - **聊天客户端模式**(Claude.ai/Desktop 走 MCP)→ 这条**真被删 MCP 打断**,是非常驻路径,单独处理(不在本子系统主路径)。

---

## 4. 写侧设计

> 📜 **写的基础行为规则(何时不写 / create vs supersede vs patch vs delete / 两写入口分工 / "已记好"时序)以 [`IO-memory-read-write-contract.md`](IO-memory-read-write-contract.md)(NORMATIVE)为准。** 本节只讲**架构与计划**(commit 终点、MemoryCard、两 route 驱动差异、要做的工程项),不复述规则细则。

### 4.1 commit(已统一,不动)
`memory/actions.py:_execute_memory_action` 唯一写入终点。**MemoryCard v1**:明文 `card_v/status/salience/importance/source_type/supersedes/superseded_by/is_sensitive`;密文 `summary/verbatim/follow_up`;**双写 legacy `title/description/her_quote`** 保 iOS Garden。**supersede 软退场永不硬删、两卡原子**。

### 4.2 两条 route 的写(结构一致,驱动不同)
| | route B(服务端驱动)| route A(agent skill 驱动)|
|---|---|---|
| 每轮 | background controller 判断写 | running capture(回复后判断,skill 已有)|
| 周期 | 24 轮 capture 批量补 | **每 N 轮 sweep(新增进 skill)** |
| 额外 | — | **读写耦合(新增进 skill)**:读 index 时发现用户说了 index 里没有的持久事实 → 顺手写 |

**要做的(写)**:
1. **action schema + conformance tests**:route A/B 同一份 schema/规则,**共享契约不跨进程 import**,过同一组 conformance 测试。
2. **共享 propose 规则文本** + **扩到 durable facts**(修"我养了狗叫蛋子"漏记;捕获从"偏好"扩到"宠物/人/地点/物品/长期身份事实")。
3. route A 写加 **每 N 轮 sweep + 读写耦合**(skill)。
4. **屏幕→记忆(A4)**:感知周期 distill → 走统一写管道(`source_type=screen/gps`),严格 eval 卡 + 隐私标记。

### 4.3 不做(原则护栏)
- ❌ **服务端对 route A transcript 跑 capture**(= 给 route A 跑服务端 LLM,破"用户自带算力"原则)。route A 写全靠 agent skill。
- ❌ 硬删(supersede 软退场)。❌ merge / decay / contradict 复杂仲裁(后续)。

---

## 5. Phase 归位(xyn A/B/C ↔ memory 子系统)

| xyn Phase | memory 子系统对应 | 负责 |
|---|---|---|
| **A1 build_companion_context(memory 部分)** | §3 读侧三层 + agent 优先 + `/v1/memory/recall` + 唤醒填 memory | **hx** |
| **A2/A3 screen 默认 + 唤醒填** | §3.2-3(memory 提供 memory_count/卡给 A3)| zhihao(memory 配合)|
| **A4 屏幕→记忆** | §4.2 感知 distill → 写管道 | **hx** |
| **B1 API 真 runtime** | M1.5 agentic + tool-loop(复用)| 后续 |
| **C2 selector vendor** | MemPalace 离线 POC(不进主链路)| 条件性 |

**memory 子系统内部顺序**:① action schema+conformance ② `/v1/memory/recall` 后端 selector ③ route A 读接 agent 优先(skill 教 HTTP `index`/`fetch` 直连)+ consumer baseline 调 recall ④ 写加 sweep+耦合 ⑤ 唤醒填 memory ⑥ A4 屏幕→记忆。

---

## 6. 测试 / 验收(对齐 xyn acceptance)

- **跨天记忆**:前天逛街纠结、今天看同款页 → agent 召回相关记忆(核心验收)。
- **身份连续**:agent 不忘自己人设/关系。
- **无屏幕诚实**:没 frame 时不假装看见。
- **VPS/API 一致**:两边对 memory 的读/写表现尽量一致(读都 agent 优先+服务端兜底)。
- **写**:猫/蛋子能被记;supersede 后不返旧卡;弱模型不丢召回(兜底)。
- 行为变了才卡 gate:route A 接召回(行为变)→ flag off 逐字节回旧 + flag on 能召回 + token 预算 + 不回归 + ≥2 真实 smoke。改捕获(写)→ 必须带 probe。

---

## 7. 功能点全清单(Codex 逐条核对,别漏)

**读**:① 三层(常驻/召回/短期)② route B agent 优先+服务端兜底 ③ route A agent 优先(**直连 HTTP `index`/`fetch`,MCP 已删**)+ consumer baseline ④ route A read skill 改教 HTTP `index`/`fetch` 自己挑(替代 context_memories)+ 清 stale MCP 引用 ⑤ `/v1/memory/recall` 共享 selector(consumer 兜底+wake+route B fallback)⑥ 唤醒读填 memory+identity ⑦ consumer-push 默认开 ⑧ 召回窗口可配置(50/可配/全开/HARD_MAX,含 enclave 两处)⑨ build_companion_context memory 部分 agent 优先(对齐 xyn)。

**写**:⑩ commit 统一 + MemoryCard v1 + 双写 legacy ⑪ insert/supersede(软退场永不硬删)⑫ route B 每轮 controller + 24 轮 capture ⑬ route A 每轮 capture + 每 N 轮 sweep + 读写耦合 ⑭ action schema + conformance(不跨进程 import)⑮ 共享 propose 规则 + durable facts ⑯ A4 屏幕→记忆。

**质量**:⑰ M3 eval probe(该记/不该记/该 supersede/该承认不知道)⑱ 敏感误取=0、编造=0。

**不做/parked**:⑲ 服务端对 route A 跑 capture(不做,违背理念)⑳ MemPalace(post-M3 离线 POC)㉑ 常驻层(identity/pinned)+ 短期边界 深设计(parked,等 build_companion_context 骨架)㉒ merge/decay/contradict 仲裁。

---

## 8. 给 Codex 的 review 输出格式

```md
## 一致性
docs/memory 内部矛盾点:
本文 vs xyn RUNTIME doc 矛盾点:

## 功能点核对(§7 逐条)
功能点 → 代码位置/Phase → 缺口

## 代码 review(三个 repo)
feedling-mcp 缺口:
feedling-mcp-ios 缺口:
io-onboarding 缺口:

## 反向核对(代码/需求有、本文没提的)
遗漏点:

## 结论
可否进 plan 执行:
最该先做(memory 子系统第一步):
最大风险:
```

---

## 9. 一句话

> **memory 是 xyn `build_companion_context` 的零件:读=三层(常驻/短期 push + 召回 agent 优先)、两 route 行为一致(agent 语义挑 + 服务端 baseline 兜底)、route A 走 HTTP 直连 `index`/`fetch`(MCP 已删,skill 教会 agent 调,修上轮遗漏);写=统一 commit + insert/supersede,route A 用 skill(每轮 capture+每 N 轮 sweep+读写耦合),服务端不替 route A 跑 LLM;屏幕→记忆(A4)提前做;常驻/MemPalace/merge 后置。Codex 必须全局 review 全部 md + 三个 repo 代码,逐条核对 §7,别再漏。**
