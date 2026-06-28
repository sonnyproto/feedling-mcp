# V1 e2e 减负方案(Part A = Codex 自动 / Part B = hx 手动)

> 目标:把 hx 手测压到只剩 **UI + 主观质量**;**后端正确性 + 事实不丢** 全部交给 Codex 自动 e2e。
> 0628 夜定:开放设计点 CC 已替 hx 拍掉(见下),Codex 可直接开跑,无需等 hx。

---

## 关键前提 / 已拍的决定

- hx 本机 resident:`/Users/hx/resident-runtime`,真 `test-api` + 真 enclave,driver=**claude**(env `AGENT_CLI_CMD`;⚠️ live 待确认,旧 log 有 codex-agent.sh)。
- **决定①(账号隔离)**:Codex 自动部分 **注册自己的 throwaway user**,用 Codex 自己的 consumer 跑 —— **完全不碰 hx 的账号/key/Garden,零污染**。(不依赖、不污染 hx resident。)
- **决定②(真实 loop vs 后端)**:确定性后端链路 = Codex 自动(Part A);真实 app 聊天体验 + UI + 主观质量 = hx 手动(Part B)。
- **断言原则**:只断言 **结构 + 事实存活 + 状态机**,不断言 LLM 措辞质量(质量 = eval,另算)。LLM 非确定性 → 用「包含 / ≥N / 状态」+ 可重试。
- **定位**:上线前 smoke,不进每-commit CI(每跑真打 LLM,花 token/慢)。

---

## Part A —— Codex 自动测(overnight,throwaway user,零污染)

逐条断言(只看结构+事实存活+状态):

1. **genesis / onboarding**:seed 一段含已知事实("我叫 Z""狗叫蛋子")的历史 → 跑蒸馏 → 断言 identity 写了(名字非空)+ ≥N memory 卡 + **有卡含该事实** + 能解密读回 + job=done。
2. **capture 写记忆**:发含"我叫 Z"的消息 →(Codex 自己的 consumer / force capture)→ 断言 **有卡含 Z** + 能读回。
3. **memory 读**:`index`/`fetch` 返回 seeded 卡;claude driver 下再验 **agent 真走了 index→fetch**(flow-trace)+ 回复含事实。
4. **route / flow-trace**:消息 → 走 `agent_runtime` + 对应 trace 事件冒出。
5. **migration 回归**:`c9c5a5e` 后的 legacy→v1(id 稳 + 能解密 + CAS stale + done),纳入回归。

**产出**:① 一条命令跑完的报告(逐条 pass/fail);② harness 提到 **分支 `feat/v1-e2e-suite`**(不直接进 test,留给 hx review 后合);③ 失败项贴断言 + 现象。

---

## Part B —— hx 手动(明早,e2e 盖不了的)

1. **真机 Garden UI**:迁移 / capture 后卡片显示对、没乱、内容在(API e2e 看不到界面)。
2. **真实 chat loop**:发条消息,回复自然、不卡(主观体感)。
3. **主观质量**:voice/人设、召回相关性、重写读起来顺不顺(= eval 的人看部分,TODO C)。
4. **review Part A 结果** + 拍下面几个确认。

---

## hx 明早 review / 拍板清单

- [ ] Part A 报告:过没过、有没有真问题。
- [ ] 确认 resident **live driver 是不是 claude**(env 写 claude,旧 log 有 codex-agent.sh)。
- [ ] 覆盖项要不要加 / 砍。
- [ ] `feat/v1-e2e-suite` review 后合 test。
- [ ] eval(主观质量)排期(TODO C)。

---

## 分工

- **CC**:断言清单 / seed 规格 + 本方案文档(已出)。
- **Codex**:建 harness + 自动跑 Part A,产出报告 + 分支。
- **hx**:跑 Part B + 拍 review 清单。
