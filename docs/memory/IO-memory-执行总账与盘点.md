# IO Memory · 执行总账 + 盘点(理清"做了啥 / 哪里做多了")

> 2026-06-24 · 作者:CC(给 hx 理清用)· 状态:盘点快照
> 目的:把 M1→M3 + 本轮里程碑 + v1 设计 一次理清,看清**做了哪些、各自状态、哪里做多了、现在聚焦什么**。

---

## 一句话
**记忆的"读写地基"做扎实了(M1→里程碑 M);但中间为"富前端 + 复杂纠错"堆了不少东西(M2 重字段/supersede/6-type),现在方向简化了 → 这些显得做多了。下一步 = v1 极简 + 把工具交给 zhihao。**

---

## 一、时间线:做了什么

| 阶段 | 目标 | 产出 | 状态 |
|---|---|---|---|
| **M1 readside** | 记忆能被安全读出 | `/v1/memory/index`+`/fetch`、enclave 加密解密 | ✅ 上 test |
| **M1.5 agent tools** | agent 优先读 + 服务端兜底 | tool-loop + fallback + 修"被自己错话带偏" | ✅ 上 test |
| **M2 写入闭环** | 记忆能写进去 | insert/supersede、**MemoryCard v1(重 schema 15+ 字段)** | ✅ 分支完成(待合/真机) |
| **里程碑 M(本轮)** | route A 读写收口 + 两 route 行为一致 | `/v1/memory/recall`、route A agent-first HTTP、写命门、读写合同、fetch sensitive gate | ✅ 代码完成(Codex worktree;待真机 smoke + PG 测试 + commit) |
| **M3 质量/eval** | "该记什么"判断对(狗=蛋子 bug) | 只有方向文档 | ❌ 未开工 |
| **v1 极简重做(设计)** | 砍冗余、8 字段卡、常驻=identity、工具瘦身 | 设计稿 `IO-memory-v1极简重做-给codex.md` | 📝 设计完成,待 Codex 评估改造量 |
| **Agent Runtime plan** | API 也跑真 agent loop、统一 API/VPS | xyn 的 plan(runtime 层,**不是你的活**) | 📝 xyn 推,未实现 |

**核实结论**:**记忆读写后端(index/fetch/recall/actions/enclave/写入终点)= 真做完了,而且要变成新 runtime 的工具地基。** 这是大头、是地基。

---

## 二、哪里"做多了"(盘点)

分三种,性质不同:

### A. 真·从一开始就没用上(纯过度)
- **insight/reflection(6-type 里的 2 类)+ 全套 anchor 校验机器**(`_validate_anchor_ids`/`_reflection_time_cap_ok`/anchor 要求)—— **没生成器、永远空**。这是最明确的"做多了",从 day 1 就没真跑。

### B. 当时合理、被"简化方向"超越(不是错,是被后来的决定砍掉)
- **M2 的重 MemoryCard schema(15+ 字段:title/description/summary 拆分/verbatim/her_quote/context/follow_up/linked_dimension/card_v/salience/importance/source_type/supersedes/superseded_by/is_sensitive)** → v1 砍到 **8 字段**。
- **M2 的 supersede 软退场原子机器** → v1 用 `add+delete` 覆盖,自动版后期再极轻回来。
- **6-type + TAB_FOR_TYPE 3-tab 映射** → v1 塌成一种 kind。
- **legacy 双写(保旧 Garden)** → Garden 重做,不再双写。
> 这些当时是为"富前端 Garden + 精细纠错"做的,**那个时候是合理的**;现在方向变成"前端重做 + 极简卡",才显得多。

### C. 死端点(清理即可)
- `/v1/memory/get`(0 调用)、`/retype`(0,没 type 了)、`/delete`(0,被 memory.delete 取代)、`/verify`(近乎死)。

---

## 三、为什么会"感觉做多了"(根因,不怪你)
**产品方向在你做的过程中简化了两次:**
1. **前端要重做**(Seven):原来记忆的一堆字段/type/tab 是为旧 Garden 样子服务的 → 前端一重做,这些字段失去存在理由。
2. **runtime 要统一**(xyn 的 plan):记忆从"自己拼 prompt"退成"被 agent loop 当工具调" → 很多包装/拼装逻辑被上层吸收。

**每一步当时的决定都说得通;是累积的简化,事后回看才露出"多"。不是方向错,是方向收窄了。**

---

## 四、现在聚焦什么(三句话定范围)
1. **留(地基,别删)**:记忆存储 + `/v1/memory/index|fetch|recall|actions` + recall/selector + enclave + 写入终点 → 成为 zhihao agent loop 的工具后端。
2. **简化(v1,你做)**:卡 → 8 字段;砍 6-type/重字段/supersede 机器/anchor/tab/legacy 双写;写动作只留 `add+delete`;读 `index→挑→fetch`。
3. **交付 + 延后**:把**工具契约**给 zhihao(他放进 loop);M3 质量、A4 屏幕→记忆、自动 supersede/merge、embedding、Garden 重做、画像 → **延后**。

---

## 五、一句话收口
> **做了:记忆读写地基(扎实,变工具底座)。做多了:为旧富前端+复杂纠错堆的字段/类型/supersede(B 类,被简化超越)+ 一块从没用上的 insight/reflection(A 类)+ 几个死端点(C 类)。现在:v1 极简化(砍 A/B/C)+ 留地基 + 交工具给 zhihao + 其余延后。** 不是白做——是方向收窄,把多余的削掉就好。
