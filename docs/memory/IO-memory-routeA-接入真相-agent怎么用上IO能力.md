# route A 接入真相:agent 到底怎么用上 IO 的能力(完整说明)

> 2026-06-23 · 作者:Claude(CC) · 起因:hx 担心"route A 配了那么多工具/接口,agent 到底有没有用?怎么接入的?onboarding md 还是怎样?"
> 已核对:`io-onboarding`(skill.md / skill-resident-agent.md)+ `feedling-mcp` consumer + MCP 工具集。

---

## 0. 一句话

**route A 的 agent 是"用户自己的 agent",我们够不到它的 loop。我们只能通过三条通道间接接入:① onboarding skill(指令)② MCP 工具(配在 agent runtime 里,agent 自己调)③ consumer(服务端转发 + 兜底注入)。其中只有 ③ 是"保证发生"的;① ② 都是 best-effort——agent 不听话/不调工具就白配。** 这就是"做了那么多 agent 没用"的根源,也是 route A 和 route B 的本质区别。

---

## 1. route A 的三条接入通道

```
                        ┌─────────────── 用户自己的机器 / VPS ───────────────┐
  IO 后端  ◄── HTTP ──►  consumer(转发服务) ──CLI/HTTP──►  用户的 agent runtime  │
   │  ▲                   (poll→甩消息→写回;P1 注入召回)      (Hermes / CC / …)    │
   │  │                                                          │  ▲              │
   │  └────────────── MCP(feedling_* 工具)────────────────────┘  │              │
   │                  (agent 自己调:读写 memory/identity/chat)     │              │
   └── onboarding skill.md(指令:怎么连、该记啥、怎么用工具)──────┘              │
                        └────────────────────────────────────────────────────────┘
```

### 通道① · onboarding skill(md = 指令,不是代码)
- `io-onboarding/skill.md` + `skill-resident-agent.md` 是**给 agent 读的指令**:怎么连 IO、Step 0 核对关系、4 趟 memory pass、写 identity、发首条问候。
- 关键原话:**"All judgment — what to remember — is yours"** + **"Do not wrap {message} in a new identity/persona prompt. IO is a new transport for the same agent."**
- 含义:**IO 不给 agent 新人格;agent 用自己的判断和人设**。skill 只是"叮嘱",听不听是 agent 的事。

### 通道② · MCP 工具(`FEEDLING_MCP_URL/KEY`,配在 agent runtime 里)
- onboarding 让用户给 agent 配 `FEEDLING_MCP_URL/KEY` → agent 的 runtime(如 Hermes/CC 的 MCP 配置)里就有了一批 `feedling_*` 工具,**agent 自己调**:
  - identity:`feedling_identity_init / get / nudge / replace / verify / set_relationship_days`
  - memory:`feedling_memory_add_moment / get / list / retype / delete / verify`
  - chat:`feedling_chat_get_history / post_message / post_image / verify_loop`
  - 感知:`feedling_screen_analyze / latest_frame / summary / …`
  - 其它:`feedling_bootstrap / onboarding_validate / push_*`
- **这是 best-effort**:工具在那儿,但**agent 选择调才生效**;skill 叮嘱它调,但不保证。

### 通道③ · consumer(转发服务,服务端驱动)
- `feedling-chat-resident`:`poll /v1/chat/poll → 甩给 agent CLI(hermes/claude `{message}`)→ POST /v1/chat/response`。
- 它**把整轮甩给 agent CLI 跑**(`--max-turns`/`--resume`),**不居中调度 agent 的工具调用**。
- 它能**主动注入上下文**(screen,P1 加了 memory 召回)——**这条是"保证发生"的**,不依赖 agent。

---

## 2. 谁驱动 / 保证还是 best-effort

| 能力 | 走哪条 | 谁驱动 | 保证吗 |
|---|---|---|---|
| 转发消息、写回复 | ③ consumer | 服务端 | ✅ |
| 召回注入(P1)| ③ consumer | 服务端 | ✅ |
| agent 主动读/写 memory(index/fetch/add)| ② MCP | **agent** | ❌ best-effort |
| 写 identity / memory(onboarding 4 趟)| ② MCP | agent(setup 时)| ❌ best-effort(但 onboarding 有 validate 兜) |
| 每轮注入 IO identity(常驻人设)| —— | **没有** | ❌ route A 不做(用 agent 自己人设) |

---

## 3. 回答你的核心疑问

### Q「我配置那么多,怎么接入的?onboarding md 还是怎样?」
**三条都用上了**:
- 配置的"指令"= onboarding skill md(通道①)。
- 配置的"工具"= MCP `feedling_*`(通道②,落在 agent 自己 runtime 的 MCP 配置里,**不是我们的进程**)。
- 配置的"通路"= consumer 转发服务(通道③)。

所以"接入"不是一个地方,是**指令(md)+ 工具(MCP)+ 转发(consumer)**三件套。

### Q「agent 到底有没有用?」(你最担心的)
- **通道③(consumer)做的事一定发生**(转发、P1 召回注入)。
- **通道②(MCP 工具)是 best-effort**:agent runtime 里有工具、skill 叮嘱它调,但**调不调由 agent**。弱 agent / 不听话 / 没把 MCP 配上 → **白配**。
- 这正是 route A 和 route B 的区别:**route B 的 agent 是我们做的、loop 我们把控**(我们替它调工具);**route A 的 agent 是用户的,我们只能给工具 + 叮嘱 + 兜底**。

### Q「常驻层是不是 onboarding 时注入过?」
- identity 是 **onboarding 时被写进 IO 的**(agent 用 `feedling_identity_init` 写)——**这是"写过一次",不是"每轮注入"**。
- 平常聊天:**route A 不每轮注入 IO identity**,agent 用自己 runtime 的人设(skill 明令别包新人格)。
- 所以"常驻层(每轮都带的人设/核心事实)"**route A 现在没有**——这就是 A/B 最大的分叉。

---

## 4. 现状的几个真 gap(为什么会觉得"白配了")

1. **新 readside(index/fetch)没进 route A 的工具集**:onboarding 让 agent 用的是**老 memory 工具**(`add_moment/get/list`),**新的 `feedling_memory_index/fetch`(M1/M1.5)route A agent 根本没被告知去用**。→ 你做的新召回,route A 现在够不着。
2. **MCP 全是 best-effort**:agent 不调就没用;没有"保证 agent 每轮查记忆"的机制。
3. **consumer 只兜"读注入"**(P1),不兜"写"(写仍靠 agent 主动发 memory 动作)。
4. **常驻层缺失**:route A 每轮不注入 IO identity/核心事实,persona 层 A/B 像两个 app。

---

## 5. 对照 route B(为什么 route B 好理解)

| | route B | route A |
|---|---|---|
| agent 是谁的 | **我们的**(hosted runtime)| 用户自己的(Hermes/CC)|
| loop 谁控 | **我们**(controller + tool-loop + fallback)| 用户 runtime 控,我们够不到 |
| 工具怎么用 | 我们替它调 / 兜底 | 给 MCP 工具 + skill 叮嘱,**它自己调** |
| identity 常驻 | **每轮注入** | 不注入(用 agent 自己人设)|
| 保证程度 | 高(我们把控) | 低(best-effort + consumer 兜底)|

**一句话**:route B 是"我们开车";route A 是"把工具和说明书塞给用户的车,它开不开、用不用,我们只能兜底"。

---

## 6. 这跟 P1 / P1.5 的关系

- **P1(consumer 召回注入)= 给通道③加了"读兜底"**——正因为通道②(MCP/agent 自觉)靠不住,才用 consumer 兜底注入。**方向对**。
- **P1.5 要补的**:
  1. **新 readside 进 route A 工具集**(让 agent 能调 `feedling_memory_index/fetch`,而不是只有老的 add/list)——否则 agentic recall 无从谈起。
  2. **后端 `/v1/memory/recall`**:consumer 兜底走它(共享 selector)。
  3. **常驻层**:route A 每轮注入 IO identity(+ 将来 pinned 核心卡)——**但"用 IO 人设还是 agent 自己人设"是产品判断,要 hx/Seven 拍**。
- **agentic route A 的本质** = 让用户 agent 的 runtime 配上新 MCP 工具 + skill 叮嘱它调 → **best-effort**;consumer 兜底是 floor。**不是 consumer 居中 tool-loop**(consumer 把整轮甩给 CLI,不居中)。

---

## 7. 结论

> **route A 的"接入"= onboarding skill(指令)+ MCP 工具(配在 agent 自己 runtime,agent 自调=best-effort)+ consumer(转发+兜底=保证)。你担心"agent 没用"是对的:MCP 那条全靠 agent 自觉,而且新 readside 还没进它的工具集。route B 好理解是因为 agent 和 loop 都是我们的。所以 route A 要"真用上",要么靠 consumer 兜底(保证、但不是 agent 自己用),要么把新工具 + skill 配进用户 runtime 让它自觉调(best-effort)。常驻层(identity)route A 现在每轮不注入,是 A/B 最大分叉,且用谁的人设要产品拍板。**
