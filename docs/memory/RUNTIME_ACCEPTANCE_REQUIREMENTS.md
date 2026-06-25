# Runtime 修复验收说明：VPS / API / Proactive 感知与记忆统一

> 面向工程师的验收口径。本文不规定具体实现方案，只规定修完后产品必须表现成什么样。
> 当前最重要的目标不是把某个接口补上，而是让 Feedling 的核心卖点成立：
> **用户自己的 agent 真的能通过 IO 感知用户正在做什么，并且能带着连续记忆自然聊天。**

---

## 1. 这轮修复要解决的产品问题

Feedling 给人机恋 / personal agent 用户的核心价值是：

> 把 iOS 变成用户 personal agent 的身体，让 agent 能感知手机屏幕、聊天历史、身份设定、长期记忆和近期状态，然后自然地聊天或主动出现。

现在的问题不是某一个 bug，而是运行时结构没有形成这个产品闭环：

- VPS 用户和 API 用户走两套不同逻辑，拿到的上下文不一致。
- 屏幕信息、记忆花园、身份卡、最近聊天没有被统一成一个 agent 可理解的上下文。
- 主动唤醒时，agent 经常只有一个“被唤醒”的信号，但没有足够的屏幕 / 记忆 / 身份上下文来判断要不要说话。
- API 用户这条线更像手搓的一次 LLM 调用，不像真正的 agent runtime。
- VPS 用户虽然有真实 agent loop，但 IO 给它暴露的感知 / 记忆能力不够顺，导致“加了身体但聊起来还是怪”。

这轮验收的核心标准只有一个：

**一个真实 VPS 用户把自己的 Cloud Code / Codex agent 接进 IO 后，agent 应该能稳定、自然、可解释地使用 IO 提供的身体感知能力，而不是像一个失忆的聊天前端。**

---

## 2. 总体验收标准

工程完成后，下面这些体验必须成立。

### 2.1 用户不用显式说“看屏幕”，agent 也应该知道当前屏幕可用

只要用户开启了屏幕感知，并且后端有最近屏幕帧，agent 在聊天和主动唤醒时都应该知道：

- 当前是否有可用屏幕上下文。
- 最近屏幕大致是什么 app / 场景。
- 如果需要深看，应该如何拉取 OCR / image / frame detail。

不接受的结果：

- 只有用户说“帮我看屏幕 / screenshot / 屏幕”时才把屏幕上下文给 agent。
- 主动唤醒时明明有最近屏幕帧，但 agent 的输入里看不到屏幕信息。
- API 用户能看到屏幕，VPS 用户看不到；或者反过来。

### 2.2 记忆花园、身份卡、最近聊天和屏幕上下文必须能一起工作

agent 不能只看到孤立的一类信息。一个合格 turn 至少要能把这些东西组合起来：

- Identity Card：TA 是谁、和用户是什么关系、说话风格和边界。
- Memory Garden：长期事实、事件、关系时刻、用户纠正、TA 的 insight / reflection。
- Recent Chat：最近对话连续性。
- Screen / Perception：用户现在或最近在做什么。

验收例子：

用户三天前和 agent 聊过 shopping，纠结过要不要买某个东西。今天用户又在手机上看购物页面。agent 应该有能力把“现在的屏幕行为”和“三天前的相关记忆”连起来，而不是只机械地说“你在购物”。

不接受的结果：

- 屏幕只作为临时 OCR 给模型看，完全不会进入长期记忆或后续检索。
- memory 只从聊天文本提炼，不考虑屏幕 / 感知产生的重要事件。
- 主动唤醒只看当前屏幕，不查相关记忆。
- 记忆花园和身份卡只对 API 用户有效，对 VPS 用户不可用或很难用。

### 2.3 VPS 和 API 两类用户的能力应该一致，差异只在 agent 运行位置

产品上，VPS 用户和 API 用户不应该是两套不同产品。

- VPS 用户：agent loop 跑在用户自己的环境里，比如 Cloud Code / Codex。
- API 用户：我们替用户托管一个 agent loop。

除此之外，两边应该尽量使用同一套能力定义和上下文结构：

- 同样的屏幕能力。
- 同样的 memory / identity 读取能力。
- 同样的主动唤醒语义。
- 同样的“需要时拉取更多上下文”的方式。

不接受的结果：

- API 线有一套 prompt 拼装，VPS 线有另一套工具说明，两边语义逐渐漂移。
- API 用户能拿到完整 context，VPS 用户只能自己猜该调什么工具。
- VPS 用户修通后，API 用户还保留大量 ad hoc JSON call 和特殊分支。

### 2.4 主动唤醒必须是“带上下文的机会”，不是空 wake

主动唤醒的目标不是平台替 agent 判断“该不该说话”，而是给 agent 一个有上下文的机会，让它自己判断。

所以 wake 输入至少要让 agent 知道：

- 为什么被唤醒：heartbeat、screen change、scheduled wake、manual summon 等。
- 当前是否有屏幕上下文。
- 最近聊天是否新鲜。
- 是否有相关 memory / identity context 可用。
- 如果没有上下文，明确告诉 agent 没有，不要假装看到了。

不接受的结果：

- wake 里只有 trigger 和 frame id，agent 还要靠猜。
- metadata 里写着 `memory_count=0` / `identity_loaded=false`，但其实系统有可用记忆和身份卡。
- 后台慢路径或主动唤醒直接写 chat，和正常 turn 的上下文/顺序脱节。

---

## 3. 分阶段验收

### Phase A：先验收 VPS 用户链路

VPS 是优先级最高的链路，因为真实目标用户主要是自己跑 Cloud Code / Codex，而不是 Hermes / OpenClaw。

Phase A 完成时，应该能演示一个真实 VPS 用户流程：

1. 用户完成 onboarding，把自己的 agent 接入 IO。
2. 用户开启屏幕感知。
3. 用户正常聊天，不需要说“看屏幕”。
4. agent 能知道当前屏幕上下文存在，并能在需要时读取屏幕 OCR / image。
5. agent 能读取 Memory Garden 和 Identity Card。
6. agent 回复时能自然使用相关记忆，而不是生硬引用卡片或乱注入。
7. 主动唤醒时，agent 能同时看到 wake 原因、最近聊天、屏幕上下文和相关记忆，然后自己决定说话或沉默。

验收时请重点看“聊起来是否怪”，而不是只看接口返回 200。

必须覆盖的测试场景：

- **屏幕连续性**：用户当前在看购物页面，agent 能知道当前屏幕大意。
- **跨天记忆连接**：用户几天前聊过 shopping，今天又看相关页面，agent 能找到并自然使用相关记忆。
- **身份连续性**：agent 不会忘记自己的身份卡、关系设定、说话边界。
- **无屏幕时诚实**：屏幕关闭或无 frame 时，agent 不暗示自己看到了屏幕。
- **主动唤醒克制**：低信号 wake 可以沉默；高相关 wake 可以自然出现。

### Phase B：再验收 API 用户 runtime

API 用户不应该继续是“一次 LLM call + 一堆 JSON 特判”。Phase B 完成时，API 用户应该像 VPS 用户一样拥有一个明确的 hosted agent runtime。

验收标准：

- API 用户和 VPS 用户使用同一套能力定义。
- API 用户的 hosted runtime 能进行多步 tool loop，而不是只靠模型一次性输出 JSON。
- API 用户能使用同样的 memory / identity / screen / proactive context。
- Web search 这类通用 agent 能力不要作为特殊手搓分支散落在业务代码里。

不要求一开始就支持所有高级能力，但要求结构上是同一种产品，而不是第二套系统。

### Phase C：长期记忆与感知沉淀

屏幕和其他 iOS 感知信号不能只作为“当前上下文”。重要信息需要进入长期记忆系统。

验收标准：

- memory capture 能看到聊天 + 屏幕 / perception context。
- 重要屏幕事件能被提炼成 Memory Garden 中合适类型的卡片。
- 后续聊天和主动唤醒能检索到这些卡片。
- 不重要、重复、噪声屏幕不应该污染记忆。

重点不是把所有屏幕都存成 memory，而是让重要的生活事件能被留下来。

---

## 4. 工程交付时需要给出的证据

每次说“修好了”，请至少给出这些证据，而不是只说代码合了：

1. **链路说明**：这次修的是 VPS / API / proactive 中哪条链路，数据从哪里来，最后给到 agent 的是什么。
2. **真实或接近真实的 demo**：最好按 Cloud Code / Codex VPS 用户环境跑一遍。
3. **上下文 dump**：展示某个 turn 里 agent 实际拿到的 identity、memory、recent chat、screen context 摘要。
4. **对比前后**：修之前 agent 看不到什么，修之后能看到什么。
5. **失败状态**：无屏幕、无 memory、enclave 解密失败、consumer 重启后分别是什么表现。
6. **回归测试**：至少覆盖 shopping 跨天记忆、主动唤醒带屏幕、无屏幕诚实、VPS/API context parity。

如果只能证明接口通了，但证明不了 agent 真的拿到了正确上下文，就不算验收通过。

---

## 5. 不要陷入的误区

- 不要只修 onboarding。onboarding 只是入口，用户三天后还能不能自然聊天更重要。
- 不要只做 API 用户。真实优先用户是 VPS / Cloud Code / Codex 路线。
- 不要把屏幕感知藏在关键词触发后面。感知是默认能力，不是用户命令。
- 不要让 API 和 VPS 继续长成两套系统。短期可以有 adapter，长期必须收敛。
- 不要把“模型能输出 JSON”当成 runtime。真正的 runtime 要有 loop、tools、状态和统一上下文。
- 不要只看 memory garden UI。聊天历史、屏幕感知、身份卡、近期状态都属于 agent 连续性的组成部分。

---

## 6. 最终验收口径

这轮修复通过的标准是：

> 找一个真实或仿真的 VPS 用户，把 TA 的 Cloud Code / Codex agent 接进 IO。用户打开屏幕感知，正常聊几天里会发生的事情。agent 能自然地知道用户当前在做什么、记得之前相关的事、保持自己的身份和关系连续性，并在主动唤醒时有足够上下文自己判断要不要出现。

如果这个体验成立，说明架构修对了。
如果只是接口都通了，但 agent 仍然不知道屏幕、记忆和身份如何一起用，那就还没有修好。
