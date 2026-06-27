# IO v1 · 下一期 TODO(memory capture 可靠性 + 可观测 + eval)

> 2026-06-28 · 起因:resident(Codex)onboarding 测试里 chat live 通了,但用户明说"我叫 Z""我的狗叫崽崽"**没落卡**。扒下来发现这不是单点 bug,是一组下一期要补的事。
> 本期只做了应急 guardrail(skill)+ "capture now" 调试按钮 + flow-trace M0;下面是下一期的正经活。

---

## 北极星(reliability boundary)

**当用户明确给出稳定事实时,系统不能静默丢掉 —— 要么写卡,要么进可追踪的 capture queue,要么明确记录 skipped reason。**
不恢复旧 memory floor、不要求 onboarding 批量生成 memory。只补这条可靠性边界。

---

## 现状(下一期的起点,已扒清)

- io_cli **没有"写记忆"的 verb**(只有 memory-index/fetch 读 + identity-write)。记忆写入只有两条路:
  1. **inline**:agent 自然回复里自夹结构化 `memory.*` action → `execute_agent_actions` 执行。**Codex CLI 路短路只取 reply、不解析 action(`chat_resident_consumer.py:~2426`)→ 对 Codex 是死的**;CC 类靠输出格式、不稳。
  2. **capture lane**:consumer 攒够触发 → 专门 prompt 逼 agent 吐 JSON 卡 → `parse_capture_cards` → 写回。结构化 prompt,理论上对任何 agent 更稳。
- 触发是 **breakpoint-gated**:24 轮(`record_chat_append` 服务端每条消息查)/ 安静 20 分(consumer 打 `/v1/capture/tick`)。**短对话到不了阈值 → 根本没触发** = 那次"我叫 Z"没落卡的真因。
- 结论:写入设计其实是**收口到 capture lane**(consumer 驱动 + 结构化 prompt,不靠 agent 自觉);inline 是不稳的旁支。

---

## A. memory capture 可靠性(本期核心,P0)

- [ ] **A1 fast-path:显式稳定事实立即触发 capture,不等 breakpoint。** 用户明说要记 / 明显稳定事实(preferred name、role/background、long-term preferences、boundaries、协作风格)→ 立即 enqueue 一个 capture job。
  - 待定:**怎么判定"显式稳定事实"** —— ① agent 信号(又回到不稳)② 后端轻量启发式(我叫/记住/我的X叫…)③ 先用手动 "capture now" 按钮够测就行。倾向先 ②/③,别依赖 agent 自觉。
- [ ] **A2 可观测:capture 的 queued / ran / skipped(带 reason)进 flow-trace 面板** —— 显式事实不再静默消失,hx 测时一眼看到"进队了/写了/跳过为什么"。(接 flow-trace M0)
- [ ] **A3 capture 那轮的结构化输出可靠性** —— 验 Codex 在 "只许吐 JSON 卡" 的 prompt 下到底吐不吐得出可解析 JSON。若连 capture 都不稳 → 需给 resident runtime 一个**结构化 action 输出协议(reply + actions)**。【**Codex/zhihao 域**:resident runtime / Codex wrapper】
- [ ] **A4 onboarding validate 加 memory-capture readiness 检查** —— "chat live ≠ capture 可用",validate 不能只证明聊天通。
- [ ] **A5(次要/可选清理)去掉 memory 的 inline**(`execute_agent_actions` 的 memory.* 分支),全收口到 capture;**identity.* 的 inline 必须保留**。注:对 Codex 本来就没在跑,删=零变化,**不紧急**,当顺手清理。

---

## B. 可观测 flow-trace M1(P1,接 M0)

M0 已上:原语 + 端点 + 面板 + 埋了 route/genesis/memory 三组(双门控、默认关、零上线影响)。M1:
- [ ] **B1 埋 consumer**:`agent_call` / `actions parsed` / `reply posted` / `capture lane`(Codex 建议优先这组 —— 直接回答"agent 跑没跑、落没落卡")。
- [ ] **B2 blob 换 `db.log_append`** —— 现在环形 blob 是 load-modify-save,并发会丢事件;换流式追加。
- [ ] **B3 turn_id 透传** —— memory 工具请求头带上 turn_id,多轮测试不靠时间猜。
- [ ] **B4 再铺 voice 注入 / proactive / perception 埋点。**

---

## C. eval(P1)

- [ ] **C1 capture 判断 eval**(承接 M3:自然陈述的持久事实漏捕获)—— 用例覆盖"我叫 Z""狗叫蛋子"这类显式稳定事实**必须被捕获**;"顺嘴 ramen"这类**不必**。eval 驱动调 capture prompt 判断阈值。
- [ ] **C2 memory 读写质量 eval** —— 召回精度(agent 选对卡)、capture 精/召回、resolve-before-create 去重(不膨胀桶)、supersede 正确。
- [ ] **C3 语言 eval** —— 验本期刚加的语言约束:中文对话→中文字段(无 "pets"/"travel"),专有名词保留;en 对话→英文(出海不返工)。
- [ ] **C4 reliability-boundary eval** —— 显式稳定事实在短对话里**不被静默丢**(要么写、要么进队、要么 skipped+reason)。

---

## D. guardrail / skill 收尾(P2)

- [x] onboarding guardrail("not a gate" ≠ "skip all stable facts")已加 `skill.md` line 133 + `skill-api.md`(io-onboarding test `5d564d0`)。
- [ ] **D1** 若测下来 agent 还过度克制:平衡 skill 里其它几处单边"not a gate"措辞(189 onboarding prerequisite、644 summary)。
- [ ] **D2** skill 落卡 baseline 段画清 **inline(仅显式/identity/纠正)vs capture(ambient/后见之明)** 的分工,别让前台 agent 把 ambient 也 inline 落。【prompt 域 = Seven】
- [ ] **D3** 本期语言约束(5 个 prompt)是 Seven 的 prompt 域 —— 知会她、措辞她可调。

---

## E. carry-over(已做完代码、待验/待并)

- [ ] **E1 老卡迁移(§3)** 代码 + 3 轮 Codex review 完,在 `feat/memory-card-migration`;待 hx 真机测(`docs/V1-迁移-测试清单.md`)。
- [ ] **E2** flow-trace M0 在 test;hx 真机验主流程(`docs/V1-功能效果核对清单-给hx.md` 带面板签名)。
- [ ] **E3** test→main 别人负责,不归 hx。

---

## 分工速记

- **CC(我)写代码 + 出计划**:A1/A2/A4/A5、B*、C 的 harness、文档。
- **Codex review + 测** + **A3(resident runtime 结构化协议)主理**(consumer/runtime 是 zhihao/Codex 域)。
- **Seven**:D2/D3(prompt 域)、capture/落卡 prompt 的措辞。
- **hx**:真机测(E1/E2)、拍 A1 的判定策略 + 上线节奏。
