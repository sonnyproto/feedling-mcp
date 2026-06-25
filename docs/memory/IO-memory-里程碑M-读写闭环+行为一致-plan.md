# IO Memory · 里程碑 M:读写闭环 + 两 route 行为一致(可运行小里程碑)

> 2026-06-24 · 作者:CC · 状态:**待 Codex 复核 → 然后开工**
> 隶属:`IO-memory-子系统-spec与plan-定稿v1.md`(真相源)。本文件 = **从定稿里切出的"最小可运行"一刀**,只做"读写跑通 + 行为一致",**其余全部后置**。
> 关键背景:MCP server 已被删(`e4c8bbc`,zhihao,2026-06-12),架构转纯 HTTP;route A 读的 agent-first 通道改 **HTTP-direct**(详见定稿 §3.3/§3.4)。

---

## 0. 一句话 & 为什么先做这个

**先交一个能端到端跑、且 route A/B 行为一致的读写闭环;memory 格式/type/tab 的大改放到这个里程碑之后讨论。** 理由:格式大改影响面太广(M2/Garden/index/M3),不该 block 住"先让两条路一致地能读能写"这件更基础、可验证的事。

---

## 1. 目标(里程碑达成的定义)

1. **读 · 行为一致**:route A 和 route B **都是** "agent 语义优先召回 + 算法兜底"。
   - route B:agent tool-loop + 没调到才 fallback(**现状已基本有,本里程碑主要是验证不回归**)。
   - route A:agent 直连 HTTP `index`→自己挑→`fetch`(主)+ consumer 调 `/v1/memory/recall` 兜底(底座)。
2. **写 · 闭环**:route A/B 都能把一条用户陈述的持久事实落成记忆卡(经 HTTP `/v1/memory/actions`,**不经已删的 MCP**)。
3. **可运行**:有 flag、有冒烟、flag off 能逐字节回旧。

---

## 2. 范围

### ✅ 范围内
- `/v1/memory/recall` 后端共享 selector(query→index→selector→fetch→items+trace)。
- route A consumer 兜底:从"自己 `index`+score topK"改为**调 `/v1/memory/recall`**;**默认开**(至少 test)。
- io-onboarding skill:route A 读改成**教 agent 用 HTTP `index`/`fetch`(agent-first)**;**清掉所有 stale 的 `feedling_memory_*` MCP 引用**;确认写仍走 HTTP。
- route A 写,分两层:
  - **(命门)写路径本身能 work**:MCP 删了之后,写靠 "agent 提 `memory.*` → consumer `POST /v1/memory/actions`" 落库——**先确认这条链通**(见 §6 待确认)。
  - **(锦上添花,建在命门之上)** skill:每轮 capture + 每 N 轮 sweep + **读写耦合**(读 `index` 时发现用户说了 index 里没有的持久事实 → 顺手写)。**读写耦合依赖读路径(范围内②),所以排在读之后。**
- 验收:flag off 回旧 + flag on 召回 + 写得进 + 两 route 行为一致 + token 预算 + 冒烟。

### ⛔ 范围外(后置,本里程碑不碰)
- **记忆格式 / type / tab / index 大改**(独立议题,见 `IO-memory-格式与tab-议题-给codex看.md`,里程碑后讨论)。
- proactive 唤醒填 memory/identity(定稿后续步骤)。
- A4 屏幕→记忆。
- 常驻/pinned 层、MemPalace、merge/decay、完整 M3 eval。
- 聊天客户端(Claude.ai/Desktop 走 MCP)那条非常驻路径。

---

## 3. 交付物 & 顺序

| # | 交付 | repo | 说明 |
|---|---|---|---|
| **M-1** | `POST /v1/memory/recall` | feedling-mcp | 共享 selector;复用现有 `memory_index_selector`/readside core,**别新造第三套**(proactive 已有 index/fetch adapter,后续也复用它) |
| **M-2** | consumer 兜底改调 recall + 默认开 | feedling-mcp/tools | `_memory_recall_for_message` 不再自己 topK,改调 `/v1/memory/recall`;`FEEDLING_ROUTE_A_MEMORY_RECALL` test 默认 on |
| **M-3** | onboarding skill 读改 HTTP index/fetch + 清 stale MCP | io-onboarding | read 规则:相关时先 `index` 看摘要→语义挑→`fetch`→没命中不编;鉴权用 env `FEEDLING_API_KEY` + `X-API-Key`;删 `feedling_memory_*` MCP 工具引用 |
| **M-4(命门)** | route A 写路径能 work(HTTP) | io-onboarding + consumer | 确认 agent 提 `memory.*` 动作 → consumer `POST /v1/memory/actions` 落库这条链完整(MCP 写工具已删);skill 写规则对齐。**这条不通,先修这个。** |
| **M-5(锦上添花)** | skill 每轮 capture + 每 N 轮 sweep + 读写耦合 | io-onboarding | 建在 M-4 之上;**读写耦合依赖 M-3 的读路径**,故排其后 |
| **V** | 验收脚本/冒烟 | all | 见 §5 |

**顺序**:M-1 → M-2 → M-3 →(M-4 命门先验)→ M-5 → V。**(M-4 若发现写被 MCP 删带断,优先修 M-4 再叠 M-5)**

---

## 4. 验收(= "可运行 + 行为一致" 的硬标准)

1. **flag off → route A 逐字节回旧**(G1)。
2. **flag on → route A 召回生效**:consumer 经 recall 注入到记忆;**且后端日志能看到真实 agent 调 `index`/`fetch`**(证明 agent-first 真的活,不只剩 baseline)。
3. **route B 不回归**:agent tool-loop + fallback 行为不变。
4. **两 route 行为一致**:同一个"命中/不命中"问答,A 和 B 都 = 命中能答、不命中不编。
5. **写闭环**:在 A 和 B 各说一条持久事实(如"我养了狗叫蛋子"),都能落成卡(经 `/v1/memory/actions`)。
6. **token 在预算**:baseline 保持小;skill 提示 agent"可能已带几条,只在要更深时再 fetch"。
7. 各冒烟 ≥2:命中 / 不命中。

---

## 5. 风险 / 护栏

- **(G1)** route A 接召回是真行为变化 → flag 控、flag off 回旧、盯 token 膨胀。
- **(承重点)** HTTP 无工具自动发现 → **skill 写得好不好 = agentic 召回成不成**。验收靠后端端点命中日志,不只看接口 200。
- **不碰** M2 commit / MemoryCard v1 / legacy 双写(保 Garden)。

---

## 6. 待 Codex 确认(开工前)

1. **route A 写现在到底靠什么?** MCP 写工具(`feedling_memory_add_moment` 等)已删——现在 route A 落库是不是 "agent 在回复里提 `memory.*` 动作 → consumer `execute_memory_actions` → `POST /v1/memory/actions`"?这条链完整吗?有没有断点?(M-4 取决于此)
2. `/v1/memory/recall` 复用现有 selector / readside core 是否可行,有无隐藏耦合?
3. 这一刀切得对吗——**该在里程碑里的没漏、不该在的(尤其格式/tab、proactive)没混进来**?
4. route B 现有 fallback(`hosted/chat_routes.py` 解析 tool_calls + `_memory_fallback_instruction_message`)是否真满足"行为一致"的对照基准?
