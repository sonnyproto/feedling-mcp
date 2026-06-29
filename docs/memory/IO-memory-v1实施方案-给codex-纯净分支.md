# IO Memory v1 · 整体实施方案(给 Codex review → 产出 zhihao 的纯净"能力"分支)

> 2026-06-24 · 作者:CC · 状态:**待 Codex review**(评估改造量 + 怎么切最干净)
> 目标:交给 zhihao 一个**只提供"记忆能力"、不含"编排"的纯净分支**——他在新 agent-runtime 架构里自己组织使用。**之前为旧两 route 加的编排/多余东西,回滚到 main 的纯净版本。**
> 依据:`IO-memory-v1极简重做-给codex.md`(模型)、`IO-memory-read-write-contract.md`(写规则)、`IO-memory-关系检索与写入-共同设计-CC与Codex.md`(检索/写入收敛)。

---

## 0. 核心原则:能力 / 编排 / 死代码 三分

> ⚠️ **基线改为 `origin/test`,不是 main**:main 已停在 2026-06-10、落后 test **144 提交**(别人业务代码 perception/agent CLI 等都在 test),从 main 切=丢真实代码、合不回去。**test 才是事实主干**,且 perception 在 test、正是 A4/新 runtime 要对接的。

| 类 | 处置 | 判据 |
|---|---|---|
| **能力(capability)** | **进分支** | "给定输入返回记忆"的纯能力:存储、端点、selector、executor、加密读侧、合同、工具契约(skill 在 io-onboarding 仓另出) |
| **旧 memory 编排(orchestration)** | **在 test 基线上移除/不带**(zhihao 在新架构重做) | "怎么把记忆塞进一个回合":consumer 注入、route B 上下文组装、tool-loop 接线 |
| **死代码** | **删** | 无调用 / 无生成器 / 被 v1 简化作废 |
| **别人的业务代码**(perception/agent/locality…) | **原样保留、不碰** | 不是 memory 的,test 上已有 |

> **分支 = 从 `origin/test` 切 + 做 v1 memory 能力 + 删旧 memory 死代码 + 移除旧 memory 编排;别人业务代码不动。** "纯净"= memory 这块干净,不是整个分支只有 memory。zhihao 在 test 这套上接新 runtime。

---

## 1. 能力(进纯净分支)—— 交给 zhihao 的"积木"

| 能力 | 是什么 | 主要文件 |
|---|---|---|
| **存储 + v1 卡 schema** | `id/bucket(string)/threads(string[])/summary/content(MD)/importance/pulse/status/source/occurred_at/last_referenced_at`(结构见 `IO-memory-v1结构定稿-bucket-thread.md`)| `memory/service.py`、`memory/actions.py`(`_memory_inner_from_action` 改 v1 字段) |
| **读 · 检索** | `/v1/memory/index\|fetch\|recall`(**`bucket`/`thread` filter**、`limit` 可配不 hardcode、`index` 不含 content / `fetch` 才含、`status≠active` 不返回、排序 `相关性×importance×(1-decay)`)+ 选择函数(ambient 最近+importance / recent floor)| `memory/routes.py`、`memory_readside_core.py`、`memory_index_selector.py` |
| **写 · 落库** | `/v1/memory/actions`(`add`/`supersede`(soft)/`delete`,route A/B 规范化等价) | `memory/routes.py`、`memory/actions.py`、规范化函数 |
| **resolve-before-create 能力** | `GET /v1/memory/buckets\|threads`(或聚合现有卡)——给写入提示注入现有词表,逼复用 | `memory/routes.py`(新增) |
| **加密读侧** | enclave 解密 index/fetch + 敏感 gating | `enclave_app.py` memory readside 部分 |
| **写规则(合同)** | 何时写/bucket·thread(resolve-before-create)/importance·pulse/content MD/supersede/「已记好」 | `IO-memory-read-write-contract.md` §3 |
| **工具契约** | **`memory_search`→index(可带 bucket/thread)/ `memory_fetch`→fetch / `memory_write`→actions / `memory_recall`→recall**(+ `identity_get`);`follow_thread`=`memory_search(thread=X)`,非独立工具 | 契约文档(待出) |
| **读取/调用提示词(skill)** | agent 怎么调 tool、写规则引用合同 | ⚠️ **在 `io-onboarding` 仓**——配套 / 另一个 repo PR,不混后端分支 |
| **conformance 测试** | A/B 规范化等价、recall、readside、bucket/thread filter、supersede soft、敏感 gate | `tests/test_memory_*` |

**检索"能力"边界**:**"给定 query/bucket/thread/limit 返回排好序的记忆"= 能力(进分支);"每轮调它 + 注入 prompt + 带底色"= 编排(zhihao)。**

---

## 2. 旧 memory 编排:在 test 基线上移除(不进分支,zhihao 重做)

> 注:**不是"回滚到 main 的版本"**(main 已废),而是**在 test 基线上直接移除/不带这些旧 memory 编排文件的相关逻辑**。

| 编排 | 文件 | 为什么移除 |
|---|---|---|
| route B 上下文组装(记忆部分) | `hosted/context.py:_model_api_context_messages` | "怎么把记忆塞进 route B 回合";新架构 zhihao 用 build_companion_context 重组 |
| tool-loop 接线 | `hosted/chat_routes.py:_run_model_api_memory_tool_loop` | 回合内怎么跑工具 = 新 runtime 的事 |
| route B 手搓 JSON 契约 | `hosted_runtime.py:coerce_runtime_action` 等 | 被真 agent runtime 取代(xyn P5) |
| consumer 记忆胶水 | `tools/chat_resident_consumer.py`(recall 注入 + 规范化) | route A 编排;新架构里 agent 直接调工具 |

> 这些**回滚到 main 版本**(或直接不进新分支)。zhihao 在 agent-runtime 里用第 1 节的能力重新编排。

---

## 3. 端点:保留 / 标 legacy / 删除(纠正之前误删 get/delete)
```
保留(Garden 控制台 + 检索 + 写能力):
  /v1/memory/list      Garden 时间线
  /v1/memory/get       Garden 看详情      ← 不是死代码,是用户控制能力,保留
  /v1/memory/delete    Garden 删记忆      ← 同上,保留
  /v1/memory/index | fetch | recall | actions
标 legacy(只给旧 iOS/import/envelope 直写,不进新工具契约):
  /v1/memory/add
删除/废弃:
  /v1/memory/retype                 (没 type 了)
  /v1/memory/verify 的旧 tab/floor 逻辑  (tab-floor 概念要没了)
```
> 人话:`get/delete` 是 Garden 控制台能力(看详情、删记忆),现在 0 调用只是旧 Garden 流程没了,**新 Garden 要用,不能砍**。我之前把它们当死代码是误判,已纠正。

**死代码(删)**:
- **insight/reflection** 类型 + 全套 **anchor 机器**(`_validate_anchor_ids`/`_reflection_time_cap_ok`/anchor 要求,无生成器)。
- **重/展示字段**(title/description/her_quote/verbatim/context/follow_up/linked_dimension/card_v/salience/source_type)+ **TAB_FOR_TYPE** + **kind(relationship/fact)** + **legacy 双写**。
- ⚠️ **不删**:`supersede` 机器(v1 **保留为 soft supersede**,只是改成产 v1 字段)、`importance`(v1 复用,**别当 salience 一起删**)、`patch`(降级成 supersede,不删执行器可复用)。

---

## 4. 推理层(未来,不进 v1 / 纯净分支)
**画像 + A4(屏幕/设备推理)统一成一个"推理层" = agent 对用户的猜测**(聊天+屏幕都喂),试探性、可推翻、**不进 fact 层**。**后置(和 eval 一起,上线后)**。
> **不用 `source=sensed`**:`source` 只 `chat | screen`(grounded 观察出处);**推理/猜测是另一层,不靠 source 表达**(免得混淆 grounded vs inferred)。纯净分支不为推理层留 source 钩子,只在文档标"推理层后置"。

---

## 5. 实施阶段(怎么产出纯净分支)

**执行口径(基线 = current `origin/test`,非 main——见 §0;就地进化,不是从零搬)**:
> **test 现状**:已有你的 **M1/M1.5/M2**(`backend/memory/*`、写闭环)→ **就地改成 v1**;**没有**里程碑的 recall/agent-first/sensitive-gate/conformance(在 worktree 未 merge)→ **按 v1 重建**(worktree 那批当参考)。
```
从 current origin/test 切分支 → 就地改 memory M2→v1 + 删旧死代码/编排;别人业务代码不碰
P1 schema 就地改 v1(bucket/threads/importance/pulse/...)+ 旧卡 adapter + actions(add/supersede/delete)
P2 index/fetch/recall 按 v1 重建:bucket/thread filter + v1 返回(index 不含 content、fetch 才含、limit 可配、status 过滤)
P3 选择函数(ambient 最近+importance、recent floor)+ GET buckets|threads(resolve-before-create)
P4 删死代码(insight/reflection/anchor/kind/重字段/双写)+ 移除旧 route A/B 编排 + 改合同/工具契约
P5 conformance(参考 worktree)+ sensitive-gate(参考 worktree)+ 真机 smoke
```

**存量迁移 = adapter 优先(别一上来物理迁移)**:
```
旧 M2 卡读取时 adapter 成 v1 shape:
  title/description/her_quote → content;旧 salience/importance → importance;补 pulse 默认
  旧 type → bucket 默认归桶(moment/quote→"我们的关系"、fact/event→按内容或"未分类");threads 留空
新写入直接 v1 shape;list/get 同时展示新旧;稳定后再批量物理迁移
```

**v1 专属测试(必加)**:
```
add 写 v1 字段(bucket/threads/importance/pulse)、不写 legacy title/description/her_quote
index 只返回 id/bucket/threads/summary/importance/source/occurred_at(+派生 decay),不含 content;fetch 才含 content
index(bucket=X) / index(thread=Y) filter 生效;follow_thread 跨 bucket 捞到同线卡
limit 可配(0=全;不 hardcode 50);status≠active 不返回
supersede:旧卡转 superseded、链新卡、不硬删、新卡继承 bucket/threads;index 默认不返回 superseded
ambient 选择函数 = 最近+高importance(不依赖 query);recent floor 用 created/referenced 不是 recent chat;去重
last_referenced_at 只在 fetch/注入后更新(扫目录不更新);pulse 不进排序
旧 M2 卡经 adapter 可被 index/fetch;resolve-before-create:GET buckets/threads 返回现有词表
memory.create→add / patch→supersede / retype→400
list / get / delete 保持可用
```

---

## 6. 给 Codex review 的点
1. **能力/编排/死代码 三分对不对?** 有没有我归错类的(尤其哪些"能力"其实和编排耦合、不好干净切)?
2. **纯净分支怎么切最干净**:路线 A(从 main 长)vs 路线 B(从当前删)——哪条返工少?给建议。
3. **v1 schema 改造量 + 存量迁移**(M2 重卡 → v1 极简卡)。
4. **recall/A3-lite 选择函数算能力还是编排?** 我归"选择函数=能力、每轮调=编排",认同?
5. **回滚第 2 节编排到 main 会牵连什么?**(它们和 identity/screen/proactive 等可能耦合)
6. enclave 读侧的能力部分能否干净随分支带走(它和其他 enclave 逻辑耦合度)?

---

## 7. 给 zhihao 的交付(纯净分支里有什么)
```
HTTP 能力:  /v1/memory/index | fetch | recall | actions(+ identity_get)
工具契约:   memory_search / memory_fetch / memory_write / memory_recall(+ identity_get)—— 接进你的 agent loop(不叫 memory_get)
读写合同:   read-write-contract(何时读写、kind、emotion_weight、content、纠错)
skill 提示词:agent 怎么调这些 tool(v1 精简版)—— ⚠️ 在 io-onboarding 仓,作为另一个 repo PR / 配套文档,不在后端纯净分支里
测试:       conformance + readside + recall + 敏感 gate
不含:       任何"怎么把记忆塞进回合"的编排(你在 agent-runtime 里组织)
```

**一句话**:纯净分支 = **一组干净的记忆能力(存储/读/写/加密/合同/工具/skill/测试)**,**不含编排**;编排回滚 main、死代码删除、推理层后置。zhihao 拿能力,在新架构里自己接。
