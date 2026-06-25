# IO 记忆 + 感知 · 模块分工(hx ↔ zhihao)

> 2026-06-24 · 作者:CC(给 hx / zhihao 对齐分工用)· 状态:**建议分工,待两人确认调整**
> 背景:对照 xyn RUNTIME spec 的 P1–P9 统一计划。**runtime 骨架(P5 真 runtime、P1 `build_companion_context` 框架、P6 web search)归 xyn**;本稿只切 **memory + 感知** 这两大块在 **hx ↔ zhihao** 之间的分工。
> 一句话切法:**hx = 记忆大脑(写得进/读得出/该记什么/怎么组织);zhihao = 感知 + 落库 + 喂入(屏幕/帧/位置怎么采、存、进上下文);A4 屏幕→记忆 = 两人的交界面。**

---

## 0. 总分工表

| 模块 | 归属 | 状态 | 对应 P |
|---|---|---|---|
| 记忆读写核心(index/fetch/recall/actions、两 route 一致、加密读侧、写闭环)| **hx** | ✅ 做了(M1/M1.5/M2/里程碑 M)| P1(memory 部分)P4 |
| 记忆模型重做(格式/type→facet/tab=读法/index 只盖事实)| **hx** + Seven | ⏳ 待拍(D1–D2)| — |
| 该记什么的质量(M3:capture 判断、漏记"蛋子"、eval)| **hx** | ❌ 没做 | — |
| 长期卫生(merge / decay / 矛盾消解)| **hx** | ❌ 没做 | — |
| 常驻核心卡(pinned)层 | **hx** | ❌ 没做(现只有 identity 常驻)| — |
| 隐私 / 控制权(用户 看/标/藏/删)| **hx** + 产品 | 🟡 v1 门禁先关;v2 待 | — |
| embedding 向量召回 | **hx** | ❌ later | — |
| 感知采集 + 落库(帧 / VLM 字幕 / differ / ingress / store)| **zhihao** | ✅ 大致做了 | P8 |
| 屏幕进**默认**上下文(去掉关键词门控)| **zhihao**(+ context)| ❌ 没做(仍 `_model_api_should_attach_screen` 关键词门)| P2 |
| 唤醒预取**感知**(proactive 用 screen)| **zhihao** | 🟡 部分(感知有,memory 侧空)| P3 |
| **A4 屏幕 → 长期记忆(蒸馏)** | **交界面(两人)** | ❌ 没做 | **P7** |

---

## 1. hx(记忆大脑)模块详情

**已完成(地基)**:记忆读写核心 —— index/fetch、recall(共享 selector)、actions(create/supersede/patch/delete)、route A/B 行为一致、enclave 加密读侧、M2 写闭环、读写合同、敏感 gate。

**待做(按建议顺序)**:
1. **记忆模型重做**(块一):一种格式 + 旧 4 type 降为 facet + insight/reflection 退役 + index 只盖事实。**先拍 D1/D2。**(详 `IO-memory-完整设计与现状-vNext.md`)
2. **M3 该记什么的质量**:eval 驱动,把捕获从"偏好"扩到"事实"(修"蛋子"漏记),防污染。
3. **常驻 pinned 层**:一小撮核心事实每轮常驻,不靠召回。
4. **隐私 v2**:转"用户控制"(v1 自动门禁已关)。
5. **长期卫生**:merge / decay / 矛盾消解。
6. **embedding 召回**(later)。
7. **A1 的 memory 部分**:召回 agent 优先(已做),等 xyn 的 `build_companion_context` 骨架来装。

---

## 2. zhihao(感知 + 落库 + 喂入)模块详情

**已完成**:感知子系统(`perception/` + `screen/`)—— 帧采集、VLM 字幕、differ_v2、ingress、PG 落库(`perception/store.py` + `user_blobs/perception_items` + migration)。**P8 基本解决。**

**待做**:
1. **P2 屏幕进默认上下文**:去掉关键词门控,让 agent 默认就能看到屏幕(API 现在藏在 `should_attach_screen` 关键词后)。
2. **P3 唤醒预取感知**:主动唤醒时把 screen 真正喂进决策(感知侧;memory 侧由 hx 补)。
3. **A4 的感知侧**(见交界面)。

---

## 3. 交界面:A4 屏幕 → 长期记忆(P7,两人对接口)

这是**唯一需要两人对齐接口**的模块。建议切法:

```
zhihao(感知侧)                          hx(记忆侧)
跨时间聚合 帧/OCR/app context     ──候选事件──▶   记忆写入判断(该不该记)
提炼成"感知候选"(如:3 天内多次     {source_type:    → 过 capture/propose 规则
浏览某商品 → 可能想买)              screen, ...}     → 写成长期卡(经 /v1/memory/actions)
                                                   → 严格 eval 卡假阳性
```

**接口约定(待两人定稿)**:
- zhihao 产出**结构化"感知候选"**(不是原始帧):字段含 `summary / 证据(出现次数/时间跨度)/ source_type=screen`。
- hx 这边**不直接吃原始帧**——只吃"候选",走和聊天 capture **同一套**该记什么判断 + 写通道(避免两套写入逻辑)。
- **克制 + 严格 eval**:屏幕信号假阳性高,宁可漏不可乱记(否则污染记忆)。

> 谁先动:**zhihao 出"候选"的格式 + 触发(什么算够格的跨时间信号);hx 出"候选→卡"的判断 + 写入 + eval。** 接口字段一拍,各做各的。

---

## 4. 不在本分工内(xyn)
- **P1** `build_companion_context` 统一骨架(hx 提供 memory 零件)。
- **P5** 真 runtime 替手搓 JSON 协议。
- **P6** 删手搓 web search。

---

## 5. 阻塞决策(动手前要拍)
- **记忆模型 D1–D5**(hx + Seven):TA在想做不做、type→facet、pinned 谁定、短期→长期边界、隐私 v2。详 vNext §7。
- **A4 接口**(hx + zhihao):"感知候选"的字段 + 触发条件。
- **真机验收**(里程碑收尾):跑真 agent 看后端日志确认 agent-first 命中。

---

## 6. 一句话给 zhihao
> 记忆的"地基"(读写核心)hx 做完了;**感知采集+落库(P8)你也做得差不多**。现在两条线**唯一真正交汇**的是 **A4 屏幕→长期记忆**:你出"感知候选"信号,我(hx)出"候选→记忆卡"的判断和写入。其余 P2/P3 感知侧归你、记忆模型/M3/卫生归我、runtime 骨架归 xyn。**先把 A4 的接口字段对一下,就能各自开工。**
