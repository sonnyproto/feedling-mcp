# IO 记忆系统 · 文档索引

> 所有 memory 相关文档统一收在这里。状态约定:**定稿** = 唯一真相源;**当前** = 仍有效;**历史脉络** = 怎么走到今天(备查);**已被取代** = 内容已收进定稿,冲突以定稿为准;**archive/** = 废弃。

## ⭐⭐⭐ v1 当前真相(Codex review 就看这 4 份,2026-06-25)
1. **[v1 结构定稿(bucket+thread)](IO-memory-v1结构定稿-bucket-thread.md)** — 结构唯一真相:事件即记忆/bucket 单选+thread 多选/importance·pulse/supersede soft/decay 派生/agent-first 读+气氛灯。
2. **[v1 实施计划(基于 Codex 通读 test)](IO-memory-v1实施计划-test基线.md)** — 后端怎么建:基线 current test、干净 v1、影响范围、耦合护栏、迁移(adapter+回填口)、提示词初版、P1-P7。
2b. **[v1 实现 Spec(给 Codex 改代码)](IO-memory-v1-实现spec-给codex.md)** — ⭐**精确到文件/函数的改动**(P1-P7),Codex 照此写代码、CC review。
3. **[读写合同 Read/Write Contract](IO-memory-read-write-contract.md)** — 读写不变量(已同步 v1:bucket/thread/importance/pulse/supersede)。
4. **[给 zhihao 交付(工具+调用流程)](IO-memory-v1-给zhihao交付-工具与调用流程.md)** — 工具契约 + 回合读写流程 + 提示词 + adapter + 能力/编排边界。

> 待对接:鉴权 token 边界(hx×zhihao);Seven 拍提示词 + bucket 平铺/pulse 排序。
> ~~`IO-memory-子系统-spec与plan-定稿v1.md`~~(route A/MCP 时代)、~~`IO-memory-v1极简重做`~~、~~`结构方案-给seven确认`~~、~~`v1实施方案-给codex-纯净分支`~~ 均**被上面 4 份取代**,仅备查。下面是历史脉络。

## 🧭 理清现状(乱了看这个)
- **[执行总账 + 盘点](IO-memory-执行总账与盘点.md)** — M1→M3 + 里程碑 + v1 设计 做了啥/什么状态/哪里做多了/现在聚焦什么。一页理清。

## 🔁 进行中 · review 往返 / 里程碑 / 议题
- **[里程碑 M:读写闭环 + 行为一致(plan)](IO-memory-里程碑M-读写闭环+行为一致-plan.md)** — **当前主线、最小可运行一刀**:recall 端点 + route A 读接 HTTP agent-first + 写闭环,格式/tab 后置。待 Codex 复核 → 开工。
- [M-1 接口契约 · `/v1/memory/recall`](IO-memory-recall-接口契约-M1-给codex.md) — recall 请求/响应/trace + 复用 `select_memory_index_items`(三方同款 selector)+ handler 伪码,Codex 可直接落代码
- [补 fetch sensitive gate(给 Codex 执行)](IO-memory-fetch-sensitive-gate-给codex.md) — 小范围安全修复:fetch 照抄 index 的敏感过滤 + 透传 include_sensitive + tests;落合同 §5.1 必办项
- **[关系检索 + 写入指引 · 共同设计(CC↔Codex)](IO-memory-关系检索与写入-共同设计-CC与Codex.md)** — 两个硬问题一起磨最佳方案:① 关系纹理怎么检索(A1-A4 选项)② 写入/capture 提示词怎么优化(B1-B5)。非 review,是 co-design 收敛。
- [全局 review · Codex 回复给 CC](IO-memory-全局review-Codex回复给CC.md) — Codex 第一轮全局 review 结果(揪出 route A 读侧未闭环等)
- **[记忆 v1 · 结构定稿(bucket+thread,merged)](IO-memory-v1结构定稿-bucket-thread.md)** — ⭐⭐⭐**v1 结构唯一真相**(合并 Seven baseline + hx 决定):事件即记忆(不分 type)、bucket 平铺单选 + thread 多选横穿、importance/pulse 拆、supersede soft、decay 读时派生、limit 可配、resolve-before-create + 提示词 + 平铺vs三层 + 2 处待 Seven 确认。
- ~~[记忆 v1 · 极简重做(给 Codex)](IO-memory-v1极简重做-给codex.md)~~ — **被结构定稿取代**(kind/emotion_weight/不做supersede 已改为 bucket/thread/importance·pulse/supersede);仅"能力/编排/感知边界"等仍可参考。
- **[记忆 v1 · 实施计划(基于 Codex 通读 test)](IO-memory-v1实施计划-test基线.md)** — ⭐⭐⭐**后端实施计划(给 Codex 建)**:基线=current test、干净 v1、影响范围、耦合护栏、§5.5 迁移(翻译官 adapter + 后台回填口)、P1-P7。
- **[记忆 v1 · 给 zhihao 交付(工具+调用流程)](IO-memory-v1-给zhihao交付-工具与调用流程.md)** — ⭐⭐⭐**给 zhihao 用记忆的**:工具契约 + 一个回合读写流程 + 写/读提示词 + memory adapter + 能力/编排边界。(≠ 后端怎么建)
- ~~[记忆 v1 · 整体实施方案(纯净分支)](IO-memory-v1实施方案-给codex-纯净分支.md)~~ — **被实施计划取代**(从 main 切 + 删死代码 那套已不适用;能力/编排三分仍可参考)
- **[记忆系统 · 完整设计与现状 vNext](IO-memory-完整设计与现状-vNext.md)** — 记忆模型主设计稿(4 轴框架 + 块一~四 + 待拍决策点);v1 极简重做是它收敛后的落地版。
- **[记忆+感知 · 模块分工 hx↔zhihao](IO-memory感知-模块分工-hx与zhihao.md)** — 对照 xyn P1–P9:hx=记忆大脑 / zhihao=感知+落库+喂入 / A4 屏幕→记忆=交界面;含状态表 + A4 接口草图。给 zhihao 看。
- ~~[记忆结构方案 · 给 Seven 确认](IO-memory-结构方案-给seven确认.md)~~ — **被结构定稿取代**(发 Seven 改用上面的"结构定稿",§9 有给她的 2 个确认问题)
- [记忆模型大改 = 一个问题(讨论稿)](IO-memory-记忆模型大改-一个问题-讨论稿.md) — vNext 的精简版/前身(伞文档)
- [记忆格式 / type / tab / index 议题(给 Codex)](IO-memory-格式与tab-议题-给codex看.md) — 格式/tab 深稿(统一格式 + tab=读模式)

## 🧭 顶层框架(非 memory 专属,必读,xyn 发的)
- [RUNTIME_UNIFICATION_SPEC](RUNTIME_UNIFICATION_SPEC.md) — runtime/context 统一(`build_companion_context`,Phase A/B/C)
- [RUNTIME_ACCEPTANCE_REQUIREMENTS](RUNTIME_ACCEPTANCE_REQUIREMENTS.md) — 验收标准(VPS 用户自己 agent 用上 IO 能力)

## 🚀 入门/对齐(轻量)
- [统一架构-大白话](IO-memory-统一架构-大白话.md) — 一页讲清(中央厨房比喻)
- [本轮迭代-meeting总览](IO-memory-本轮迭代-meeting总览.md) — 做了啥/没做啥(快照)
- [routeA-接入真相](IO-memory-routeA-接入真相-agent怎么用上IO能力.md) — route A 三通道(skill/MCP/consumer)+ best-effort vs 保证

## 🔧 当前 · 专题(仍有效,细节)
- [M2-写入闭环-CC方案](IO-memory-M2-写入闭环-CC方案.md) / [M2-详细实现说明-Codex](IO-memory-M2-写入闭环-详细实现说明-Codex.md) — insert/supersede(已实现待合)
- [M3-质量与eval-方向](IO-memory-M3-质量与eval-方向.md) — 起点:狗"蛋子"漏记 bug
- [recall-window-可配置](IO-memory-recall-window-可配置-给codex.md) — 窗口 50→可配/全开

## 🗂 已被定稿取代(内容已收进定稿,冲突以定稿为准,保留备查)
- ~~[统一架构-spec(v2/v3)](IO-memory-统一架构-spec-给codex-review.md)~~
- ~~[统一架构-plan](IO-memory-统一架构-plan.md)~~
- ~~[runtime统一-CC对齐(对xyn两份doc)](IO-runtime统一-CC对齐(对xyn两份doc).md)~~
- ~~[P1-工程执行计划-Codex版](IO-memory-P1-工程执行计划-Codex版.md)~~
- ~~[P1.5-读侧统一疑问与方案-Codex](IO-memory-P1.5-读侧统一疑问与方案-Codex.md)~~

## 🕰 历史脉络(M1/M1.5 已上 test,备查)
- [M1-decision-context](IO-memory-M1-agent-tools-decision-context-for-cc.md) / [M1-Codex方案](IO-memory-M1-agent-tools-Codex方案.md)
- [M1.5-CC方案与决策](IO-memory-M1.5-agent-tools-CC方案与决策.md) / [M1.5-测试方案](IO-memory-M1.5-agent-tools-测试方案.md)
- [readside-M1-handoff](IO-memory-readside-M1-plan-evolution-and-code-handoff-codex.md) / [readside-m1-test-guide](IO-memory-readside-m1-local-test-guide-codex.md)

## 🗄 archive/(废弃)
- readside-M1-review / readside-M1-隐患(有"pick 没实现"笔误)、routeB-接管道-plan(被 agentic 取代)、hosted-recall-bug-review(bug 已修)

---

### 阅读顺序
**定稿 v1** → xyn 两份 RUNTIME → (要细节)大白话 / routeA-接入真相 / M2 / M3。

### 注
- 两个老板:**Seven**(memory reframe / agent-tools 方向)、**xyn**(RUNTIME 统一 / build_companion_context)。
- 还有一批更早 memory 文档在 `feedling-mcp-ios/Docs/`(Eval-v0、MemoryCard-v1、memory-core spec 等,iOS repo 已提交),本次未跨 repo 迁。
