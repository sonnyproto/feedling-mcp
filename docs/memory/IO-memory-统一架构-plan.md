> ⚠️ **已被取代** —— 见 **`IO-memory-子系统-spec与plan-定稿v1.md`**(冲突以定稿为准)。本文保留备查。

# IO Memory 统一架构 · 执行 Plan(5 阶段)

> 2026-06-23 · 作者:Claude(CC) · 状态:**待 Codex review**(架构见 `IO-memory-统一架构-spec-给codex-review.md` v2)
> 配套 spec 已折叠 Codex review 采纳点 + CC 4 条护栏(G1–G4)。本 plan 把收敛拆成 5 个阶段,**主线 Phase 1→2→3,Phase 4/5 后置/条件性**。

---

## 0. 总览

| Phase | 目标 | 主线? |
|---|---|---|
| **P1 统一协议 + route A 接召回** | 让 route A/B 不再像两个 app(协议、上下文、召回对齐)| ✅ 主线 |
| **P2 统一 propose 规则(+ 修狗 bug)** | "该记什么"不再分叉,捕获扩到 durable facts | ✅ 主线 |
| **P3 M3 eval + 质量** | 系统化评估 propose/commit/recall 三段 | ✅ 主线 |
| **P4 感知来源适配器** | 屏幕/GPS/录屏 进长期记忆 | ⬜ 后置(P3 后)|
| **P5 MemPalace 离线 POC** | 仅验证能否提升召回,不进主链路 | ⬜ 条件性(召回成瓶颈才做)|

**贯穿原则(每阶段都守)**:
- COMMIT(`_execute_memory_action`)不动,M2 的 MemoryCard v1 不重做。
- 统一靠**共享契约(schema/prompt/规则/fixtures)**,不强行跨进程 import 同一函数。
- **propose 跟着"谁是 agent"走**(B=服务器,A=用户 agent),不为 route A 多跑服务端 LLM。
- supersede 永不硬删、写入双写 legacy 保 Garden、enclave 隐私边界不变、不走 MCP。
- **行为变了就要卡 gate**:**P1 是明确行为变化**(route A 首次注入 IO 召回)→ gate 见 P1 验收(flag off 逐字节回旧 + flag on 能召回 + token 预算 + 不回归 + 2 个真实 smoke);P2 起改捕获行为 → 必须带 probe。
- **⚠️ 灰度备忘(route B 待定)**:P1 取消 per-user allowlist 是因为 **route A 的 flag 在 consumer 侧、本身就是 per-deployment 灰度、用户量小**。**route B 不同**——它的召回开关是 **server 端、面向大用户量**;等 route B 也上类似召回(P2+)时,**重新评估是否需要 server 端 canary / 灰度机制**,别把"route A 不需要 allowlist"直接套到 route B。

---

## Phase 1 · 统一协议 + route A 接召回

**目标**:协议、上下文拼装、召回三处对齐;route A 首次用上 IO 长期记忆。**不改"该记什么"的判断(那是 P2)。**

**做**:
1. **统一 action schema + conformance tests**:`memory.create / patch / supersede / delete` 的字段口径定一份契约,**并配一组 conformance 测试**。route A consumer **可保留自己的实现**(跨进程),但**必须过同一组 schema/coerce conformance 测试**——**不写成"consumer 直接 import 后端 coerce 函数"**。
2. **抽"上下文拼装器"成后端能力**:`query + route → assembled context block`(短期层 + 长期层);route B 内部复用现有 `hosted/context.py` 函数,route A 经 HTTP 调这个能力。
3. **route A 接长期召回**:consumer → HTTP(拼装器 / `/v1/memory/index|fetch`)→ **服务端注入**(算法选择器,**不起 LLM**)。**先注入,不做 tool-loop。**
4. **定义短期/长期在 prompt 的顺序 + token 预算**(= #7),A/B 统一。

**不做**:MemPalace、感知长期、完整 M3 eval、merge/decay、tool-loop、改捕获标准。

**交付**:统一 schema 契约文档 + 后端拼装器接口 + route A 召回注入 + 顺序/预算定义 + 测试。

**验收(P1 是明确的行为变化,gate 要硬)**:
- **flag off → 行为完全回到旧逻辑**(逐字节一致)。
- **flag on → 能召回到 IO 长期记忆卡**。
- **prompt token 不超过预算**。
- **route A 现有 identity/memory action 不回归**(过 conformance + 旧流程回归)。
- **≥2 个真实问答 smoke**:① 命中 memory(答得出);② 不命中 memory(不瞎编)。
- route A/B 走同一 action schema(过 conformance 测试);拼装器对 A/B 输出一致结构。

**护栏**:
- **(G1)** 改 route A coerce/接召回**必须回归测 route A 现有流程**(现跑的 identity/memory 动作不能坏)。
- **(G2)** route A 召回注入是**真行为变化**(原来用 agent 自己记忆)→ 冒烟 + 盯 prompt token 膨胀。

---

## Phase 2 · 统一 propose 规则(+ 修狗 bug)

**目标**:"该记什么"收敛成一份共享规则,A/B 共用;**把捕获从"偏好"扩到"durable facts"**,修掉"蛋子"漏记。

**做**:
1. **抽共享 propose 规则文本**:把后台 controller 标准 + `build_memory_capture_messages` 合成一份"该记什么"规则;hosted controller 和 route A agent **共用同一份规则文本**。
2. **扩捕获标准到 durable facts**:宠物 / 人 / 地点 / 物品 / 长期身份事实 / 关系,不再只偏"偏好"。
3. **统一 `memory.create / supersede / patch` 输出格式**(对齐 P1 schema)。
4. **补最小 probe**:至少覆盖①"我养了狗叫蛋子"这类事实**该被捕获**;②临时/闲聊**不该被记**(负例)。

**不做**:完整 eval 平台、感知、MemPalace、merge。

**交付**:共享 propose 规则文本 + 最小 probe 集 + 打分脚本。

**验收(不要求 A/B 逐字一致——执行 propose 的模型不同)**:
- "我养了狗叫蛋子,是比熊"能被捕获成卡(probe 通过)。
- 负例(随口/临时)不被误记。
- **A/B 共用同一 propose 规则 + 输出同一 action schema**;**关键 probe 在 A/B 都通过**。
- **不要求逐字 / 逐行为完全一致**(模型不同,只对齐规则 + schema + 关键 probe 结果)。

**护栏**:
- **(G3)** 共享的是**规则文本**;**不要做"服务端替 route A 跑 propose"**(那是多一个 LLM)。route A 仍由其 agent 按规则 propose、走已有写通道。
- 改了捕获行为 → **最小 probe 必须先过**才放量(eval 当闸门)。

---

## Phase 3 · M3 eval + 质量

**目标**:系统化评估记忆质量,据此调 prompt/selector/card schema。

**做**:
1. **自建 eval fixtures**:覆盖 宠物 / 关系 / 偏好 / 亲密 / 敏感边界 / 纠正旧事实(supersede)/ 多轮事实合并。
2. **三段都测**:propose(该不该记、记成什么)、commit(状态/链接对不对)、recall(是否按正确优先级用记忆、没查到别瞎答)。
3. **两类打分器**:code grader(确定性:id 命中、状态正确)+ model grader(语义:回答有没有用对、有无编造)。
4. **按 eval 调**:prompt / selector / card schema。

**不做**:感知长期(P4)、MemPalace 接入。

**交付**:eval 框架 + fixtures + 对比报告。

**验收**:三段可重复打分;新 propose/recall **不低于现状基线**;**敏感误取 = 0、编造 = 0**;paraphrase 子集较关键词有提升。

---

## Phase 4 · 感知来源适配器(屏幕/GPS/录屏 → 长期)

**前提(硬)**:P3 eval 稳定 + 同意/可见控制设计完成 + 敏感字段策略明确。

**做**:
1. **感知 distiller**:周期任务,跨时间聚合帧/信号,提炼成"候选"(如"3 天内多次浏览某商品→可能想买")。
2. 候选 → **统一写管道**(propose→commit),`source_type=screen/gps/perception`。
3. **严格 eval 卡**(假阳性高)、克制、周期跑。
4. **隐私**:标 is_sensitive,用户可见可控,默认保守。

**交付**:感知→记忆适配器 + 其 probe。

**验收**:跨时间信号能沉淀成卡;假阳性受控;敏感可见可控;不污染 Garden。

---

## Phase 5 · MemPalace 离线 POC(条件性)

**前提**:P3 后**召回成为瓶颈**(关键词/top-N 不够)才启动。

**做**:
1. 用 IO eval fixtures 导出**脱敏**数据。
2. 比较 **当前 index selector vs MemPalace hybrid retrieval**。
3. 验证:**enclave/local-only 可运行** / **supersede 后旧索引失效** / **不形成第二套 source of truth** / 不破坏加密模型。

**交付**:POC 报告 + go/no-go 结论。**不进主链路,除非全部门槛通过。**

---

## 依赖与顺序

```
P1(协议+routeA召回)──► P2(统一propose+durable facts)──► P3(eval+质量)
                                                            │
                                                            ├─► P4(感知→长期)  [需 P3 稳 + 隐私设计]
                                                            └─► P5(MemPalace POC) [条件:召回成瓶颈]
```

## 每阶段闸门

| Phase | 放量前置 |
|---|---|
| P1 | flag off 逐字节回旧 + flag on 能召回 + token 在预算 + route A 现有 action 不回归(过 conformance)+ ≥2 真实 smoke(命中/不命中)+ G1/G2 |
| P2 | 最小 probe 过(蛋子被记 + 负例不误记)|
| P3 | 三段 eval 不低于基线 + 敏感误取=0 + 编造=0 |
| P4 | 隐私/同意设计完成 + 感知 probe 假阳性受控 |
| P5 | enclave 可运行 + supersede 失效 + 不成第二 source of truth |

---

## 一句话

> **先把两条路的"协议 + 上下文 + 召回"对齐(P1),再把"该记什么"收敛并修狗 bug(P2),然后用 eval 系统化质量(P3);感知进长期(P4)和 MemPalace(P5)后置/条件性。每阶段"行为变了才要 eval",护栏 G1–G4 贯穿。**
