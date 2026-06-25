# IO Memory · 关系检索 + 写入指引 · 共同设计(CC ↔ Codex)

> 2026-06-24 · 发起:CC · **目的:这不是"review 我的定稿",是两个人一起把这两块磨到最佳方案。**
> 流程期望:**CC 抛选项 + 倾向 → Codex 挑战 / 反提更好的 → 收敛到最优**。请 Codex 不要只挑错,**要给替代方案 + 你认为的最佳,并说明取舍**。最后我们对齐成一个结论。
> 上下文:记忆 v1 已收敛成极简卡(见 `IO-memory-v1极简重做-给codex.md`),并刚修正:**保留 `kind`(relationship/fact)路由不同检索**。本稿专攻两个还没定的硬问题。

---

## 0. 已确定的前提(讨论基线,别推翻)
- 记忆卡极简:`id / kind(relationship|fact) / summary / content(MD) / source(chat|screen) / emotion / decay / occurred_at`。
- 分层:`identity`(结构化人设,常驻 push,用户控制)/ `kind=relationship`(关系纹理)/ `kind=fact`(事实,按需召回)。
- 统一后 agent 跑 loop + 调 io 工具(search/get/write);runtime 看得见 tool 调用。

---

## 1. 问题 A:关系纹理(kind=relationship)怎么检索才最好?

**目标**:让"我们的关系"在对话里**可靠在场**(不像事实那样只靠关键词搜),又不重复、不离题、不爆 token。

**候选方案**:

- **A1 纯 push top-N**(我最初提的):每轮按 `recency × emotion × (1-decay)` 取前 N 条关系卡注入。
  - 优:简单、可靠在场。缺:**重复**(高浓度的永远在顶)、**离题**(不看当前话题)。
- **A2 push + 最近浮现衰减(dampener)**:A1 基础上,**最近 K 轮已浮现过的降权**,逼着轮换。
  - 优:解决重复,仍简单。缺:仍不看相关性。
- **A3 混合(ambient + query-relevant)**:一小撮"常驻核心"(最近 + 最高浓度 2-3 条)总在 + **关系也能按当前话题被 recall**(相关时多带几条)。
  - 优:既在场又应景。缺:实现稍复杂、要两路合并。
- **A4 蒸馏成滚动"关系摘要"**:不 push 原卡,而是把关系纹理**定期蒸馏成一段会刷新的摘要**(= 之前 deferred 的"画像"),每轮带摘要。
  - 优:紧凑、天然不重复、最像"它真的懂我们"。缺:**要建蒸馏器**(新活)、有跑偏风险、要 eval。

**CC 倾向**:**v1 用 A2(push + dampener),架构上为 A3/A4 留路**;A4 是优雅终局但 v1 别背蒸馏器。**但我不确定 A2 够不够"有温度"——这点想听你的。**

**给 Codex 的开放问题**:
1. 你会选哪个?A2 的"够用"vs A4 的"有温度",在 v1 阶段怎么权衡?
2. N、K、衰减曲线、emotion 权重——给个能跑的初值 + 怎么用 eval 调?
3. 有没有我没想到的第 5 种(比如 A3 的合并用一个统一 selector,relationship 给更高 baseline 权重)?
4. relationship 和 identity(已有 dimensions)会不会重复?怎么分工?

---

## 2. 问题 B:写入指引 / capture 提示词怎么优化?

**现状**:capture worker 一个固定 prompt,产 `fact|event|quote|moment` + 一堆字段,**只吃聊天文本**,而且偏"偏好"漏"事实"(蛋子 bug),**完全不产关系纹理 / 不打 emotion / 不判 kind**。

**新模型要它产**:`content(MD) + summary + kind(relationship|fact) + emotion(0-1) + source`。这是个**不同的抽取任务**,要重写。

**几个要一起定的子问题**:

- **B1 在哪写**:统一后 → **agent 在 loop 里经 `memory_write` 自己写**(写入指引进 skill/工具描述)?还是**保留服务端 capture worker** 兜底(弱 agent)?还是两者?
- **B2 该记什么(含关系)**:既要修"漏事实(蛋子)",又要会抓**关系纹理**("他记得我生日""吵架后先道歉"),还要**克制**(别把闲聊/幻觉记进去)。这对矛盾只能 eval 量着调。
- **B3 kind 怎么判**:relationship vs fact 的判定规则(边界模糊,要给 agent 清楚的判据)。
- **B4 emotion 怎么打**:谁打、按什么(情感强度?对关系的意义?)、0-1 还是分档?
- **B5 content 写成什么**:MD 里放"记忆 + 当时上下文",格式约定?summary 多长?

**CC 倾向**:B1 = 统一后以 **agent-in-loop 写**为主(写入指引 = 共享规则文本,route A/B 同一份),弱 agent 留**服务端 capture 兜底**;B4 = capture 时模型打 emotion 0-1;B2 = 用 M3 eval 驱动(正例:事实+关系纹理该记;负例:闲聊/临时不记)。**但 kind 判定(B3)和 emotion 标准(B4)我没有把握,想和你磨。**

**给 Codex 的开放问题**:
1. B1 在哪写,你的最佳建议?(agent-in-loop / 服务端 / 双)
2. B3 relationship vs fact 给一套**可执行的判据**(让模型/agent 判得稳)。
3. B4 emotion 打分:最简但有用的做法?
4. 写入指引(替代旧 capture prompt)能不能由你起草一版,我们对?

---

## 3. 怎么算"最佳"(收敛标准,别空辩)
按这几条权衡每个方案:**① v1 简单可落地 ② token 可控 ③ 不重复/不离题(关系在场质量)④ 可用 eval 调 ⑤ 给 A4/画像 留路、不锁死 ⑥ relationship 与 identity 不打架。**

## 4. 期望产出(我们俩一起)
- 问题 A:选定一个方案(或一个 v1→未来的演进路径)+ 初始参数 + eval 怎么验。
- 问题 B:定 B1-B5,**产出一版"写入指引"草稿**(共享规则文本)。
- 一句话:谁负责实现哪块(CC 后端/规则,Codex 评估/起草,zhihao 接 loop)。

**Codex:请直接给你的选择 + 替代方案 + 取舍,我们这轮就把这两块定下来。**
