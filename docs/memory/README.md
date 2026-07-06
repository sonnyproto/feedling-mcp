# IO 记忆系统 · 文档索引

> 2026-07-06 清理:memory v1 已落地进代码,本目录只保留**当前稳定的真相源**;一批 v1 迭代方案/讨论稿/Codex 交接稿(M1–M3、P1/P1.5、统一架构、v1 各版、里程碑/议题稿等)已删除,历史在 git log 里。

## ⭐ v1 当前真相(唯一真相源)
1. **[v1 结构定稿(bucket+thread)](IO-memory-v1结构定稿-bucket-thread.md)** — 结构唯一真相:事件即记忆 / bucket 单选 + thread 多选 / importance·pulse / supersede soft / decay 派生 / agent-first 读。
2. **[读写合同 Read/Write Contract](IO-memory-read-write-contract.md)** — 读写不变量(bucket/thread/importance/pulse/supersede)。
3. **[给 zhihao 交付(工具 + 调用流程)](IO-memory-v1-给zhihao交付-工具与调用流程.md)** — 工具契约 + 一个回合的读写流程 + 提示词 + adapter + 能力/编排边界。

## 🧭 顶层框架(非 memory 专属,xyn 的,别改)
- [RUNTIME_UNIFICATION_SPEC](RUNTIME_UNIFICATION_SPEC.md) — runtime/context 统一(`build_companion_context`,Phase A/B/C)
- [RUNTIME_ACCEPTANCE_REQUIREMENTS](RUNTIME_ACCEPTANCE_REQUIREMENTS.md) — 验收标准(VPS 用户自己的 agent 用上 IO 能力)
- [IO-runtime统一-CC对齐(对 xyn 两份 doc)](IO-runtime统一-CC对齐(对xyn两份doc).md) — CC 对齐 xyn 两份 RUNTIME 的说明

## 🚀 近期工作(在 `../superpowers/`)
- [io_cli add-memory 实现计划](../superpowers/plans/2026-07-06-io-cli-add-memory.md) — VPS 侧 file→memory/identity 二次蒸馏
- [io_cli add-memory · VPS 二次蒸馏设计](../superpowers/specs/2026-07-06-io-cli-add-memory-vps-distill-design.md)

---

### 注
- 两个老板:**Seven**(memory reframe / 提示词方向)、**xyn**(RUNTIME 统一 / build_companion_context)。RUNTIME 三份是 xyn 的,别动。
- 系统总览另见 [`../MEMORY.md`](../MEMORY.md)。
- 更早的一批 memory 文档在 `feedling-mcp-ios/Docs/`(Eval-v0 / MemoryCard-v1 / memory-core spec 等,iOS repo),本次未跨 repo 处理。
