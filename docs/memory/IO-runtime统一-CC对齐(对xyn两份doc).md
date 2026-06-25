> ⚠️ **已被取代** —— 见 **`IO-memory-子系统-spec与plan-定稿v1.md`**(冲突以定稿为准)。本文保留备查。

# Runtime 统一 · CC 对齐与归位(对 xyn 两份 doc 的回应)

> 2026-06-23 · 作者:Claude(CC) · 对齐对象:`RUNTIME_ACCEPTANCE_REQUIREMENTS.md` + `RUNTIME_UNIFICATION_SPEC.md`(xyn)
> 目的:① 给出 CC 对 xyn 两份 doc 的判断;② 把我们之前的 memory 工作(M1/M1.5/M2/M3 + recall/P1.5)**归位**到 xyn 的 Phase A/B/C 框架;③ 加一处关键执行精度;④ 暂存 identity/常驻/短期 的待聊设计。

---

## 0. 一句话

**xyn 的方向对、而且是更大的正确框架——它把"记忆"扩成"runtime/context 统一"(identity+memory+screen+recent+pending 一个 `build_companion_context`,VPS/API/proactive 共用)。我们之前的 memory 工作不是另一条线,是这个框架里 A1(统一 context 的 memory 部分)和 A4(屏幕→记忆)的零件。CC 唯一要补的精度:VPS 的 context 送达必须是 consumer-push(默认开),不能只指望 agent 自己 pull MCP——否则验收"agent 一定看到屏幕/记忆"立不住。**

---

## 1. CC 对 xyn 两份 doc 的判断

### 认可(基本全对)
- **`build_companion_context()` 一个函数、三路共用** = 我一直说的"上下文拼装器",正确。
- **VPS 优先(Phase A)**:真实目标用户是自己跑 Cloud Code/Codex 的 VPS 用户,先把这条端到端修对——对。
- **给 API 真 runtime(Phase B)**:把 hosted 的 if-JSON 改成真 tool-loop agent、复用 MCP 工具——对,且正是我和 Codex 在 M1.5 已经设计过的(agentic recall + tool-loop)。
- **屏幕做默认 context(P2)**、**proactive 唤醒填 screen+memory(P3)**、**屏幕→记忆(P7/A4)**——都对,正好补上"感知没沉淀/agent 拿不到"的缺口。
- **web search 用 provider 自带、别自己爬 DuckDuckGo(P6)**——对。

### xyn 这版比我之前视角更完整的地方
我之前一直在 memory(读/写)上收敛;xyn 把 **identity + screen + recent + pending** 一起纳入一个 context——这**顺带解决了我上轮提的"route A identity 不注入"分叉**(identity 进了共享 context,VPS 也就拿到了)。

---

## 2. ⚠️ CC 要加的一处关键执行精度:VPS 送达 = consumer-push

**这是我深挖 route A 接入后最重要的一条补充(见 `IO-memory-routeA-接入真相...md`):**

- xyn 的 spec 把 VPS 归为 "MCP pull 式,agent 自己调 `feedling_screen_*/memory_*`"。
- 但**实际**:VPS 平常聊天走 **consumer**(`/v1/chat/poll` → 甩给 agent CLI(`hermes/claude "{message}"`)→ `/v1/chat/response`)。**consumer 把整轮甩给 CLI,agent 自己 pull MCP 是 best-effort——不保证调。**

→ 所以要让验收标准 **"agent 一定看到屏幕/记忆/身份"** 成立,`build_companion_context` 的**送达必须是 consumer-push**:
```
consumer 拉 build_companion_context(HTTP)→ 注入进甩给 agent CLI 的消息 → 默认开
MCP-pull(agent 自己调)保留给"想深挖"的 agent,但不是保证路径
```
- consumer 里**已经有** `_screen_context_for_message`,只是被关键词 gate 住了——**xyn 的 P2"屏幕做默认"就改这里**(去掉关键词 gate、默认带),这正好印证"consumer 才是真送达通道"。
- 不写明这点,A2/A3 容易做成"把 screen 塞进 `feedling_chat_get_history` 返回,但 agent 不调 = 白塞"。

> 一句话:**统一 context(xyn)对;但 VPS 要靠 consumer 把它推给 agent,别赌 agent 自己拉。**

---

## 3. 我们 memory 工作的归位(re-examine spec/plan)

**xyn 的 `RUNTIME_UNIFICATION_SPEC` 是上层伞;我们的 memory spec/plan 是它的"memory 子系统"。** 映射:

| xyn 框架 | 我们已做/已设计的 | 关系 |
|---|---|---|
| **A1 build_companion_context(hx)** 的 memory 部分 | M1 readside(index/fetch)+ P1.5 `/v1/memory/recall`(共享 selector)| **直接是 A1 的 memory 零件** |
| **A2/A3 screen 默认 + proactive 填(zhihao)** | — | memory 这边提供"memory_count/identity/卡"给 A3 填 |
| **A4 屏幕→记忆(hx)** | M3(质量/eval)+ 感知→长期记忆设计 | **直接是 A4** |
| **B1 API 真 runtime** | M1.5 agentic recall + tool-loop(CC/Codex 已设计)| **直接复用** |
| 写入 | M2(insert/supersede,已实现待合)| 仍有效,挂 A4/后续 |
| C2 selector vendor | MemPalace 评估(我们已结论:不进主链路,post-M3 离线 POC)| 对齐 |

**编号撞车提醒**:xyn 用 Phase A/B/C + P1–P9(P=problem);我们之前用 P1–P5(P=phase)。**今后以 xyn 的 Phase A/B/C 为准**;我们的 M1/M1.5/M2/M3 作为 memory 子系统内部命名继续用。我之前的 `统一架构-spec/plan` **降级为"memory 子系统视角",不再作为顶层 plan**(顶层以 xyn 两份 doc + 本文为准)。

---

## 4. CC 会补的几处 caution(诚实)

1. **屏幕做默认的成本**:每轮带 screen 有 token/成本/隐私代价。建议**默认带 OCR 摘要(便宜),整图按需取**(agent 要深看才给 image),别每轮塞整图。
2. **build_companion_context 的 memory 部分要分层**:`常驻(identity 已在;将来 pinned 卡)+ 召回(/v1/memory/recall)`——常驻这层见 §6 暂存。
3. **A3 proactive 填 memory** 用的 selector 要和 A1 同一套(`/v1/memory/recall`),别又分叉。
4. **B1 API runtime** 的 tool-loop 别忘 route B 的弱模型兜底(M1.5 已结论:不调工具就服务端兜底)。

---

## 5. CC 认可的执行顺序(对齐 xyn VPS-first)

```
Phase A(VPS 端到端,先做)
  A1 抽 build_companion_context(identity+memory+screen+recent+pending)  ← hx;memory 部分=我们的 readside+recall
  A2 screen 默认进 context(改 consumer 的关键词 gate,默认带)          ← zhihao;CC 注:走 consumer-push
  A3 proactive 唤醒填 screen+memory(真填,别硬编 0/false)            ← zhihao
  A4 屏幕→记忆 capture(感知沉淀长期)                                  ← hx;=我们的 M3/感知
Phase B(给 API 真 runtime)
  B1 hosted JSON → 真 tool-loop agent(复用 MCP 工具)= M1.5 agentic    
  B2 web search 换 provider 自带
Phase C(backlog)
  C1 frames 落库(PG/对象存储)  C2 selector vendor 决策(MemPalace 离线 POC)
```

---

## 6. 暂存 · 待聊设计(hx 说先记录,告一段落再细聊)

这三块**先记下、不展开**,等其他问题落定再细聊:
1. **identity 在 VPS 怎么处理**:用 IO 人设 / agent 自己的 / 合并?(进了共享 context 后,VPS 会拿到 IO identity——但"是否覆盖 agent 自己人设"是产品判断)。
2. **常驻记忆(core/pinned)**:memory 卡缺"常驻档"(只有 identity 常驻);build_companion_context 的 memory 部分应分 `常驻 + 召回`。已记 todo:`io-memory-pinned-vs-recall-todo`。
3. **短期记忆(working)**:recent chat / 当前屏幕 / GPS / pending 这层的边界、顺序、token 预算——和长期分清。

→ 这三块是**同一簇**(context 的"常驻层"设计),建议等 Phase A 的 build_companion_context 骨架定了,一起细化。**本文先占位,不下结论。**

---

## 7. 一句话收口

> **xyn 两份 doc 我认可:runtime/context 统一(build_companion_context,VPS 优先,API 给真 runtime)是更大也更对的框架,我们之前的 memory 工作正好是 A1(context 的 memory 部分)和 A4(屏幕→记忆)的零件。CC 唯一要补的硬精度:VPS 的 context 送达要 consumer-push、默认开,别赌 agent 自己 pull MCP——否则"agent 一定看到屏幕/记忆"立不住。identity/常驻/短期 三块先占位,Phase A 骨架定了再细聊。**
