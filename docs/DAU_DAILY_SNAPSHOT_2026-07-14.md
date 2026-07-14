# Spec — 每日 DAU 快照（冻结历史，删号不再追溯性减少）

Status: IMPLEMENTED and dual-signed on test (2026-07-14). Seven 定案。
Owners: codex2 = db(表/迁移/固化 job/读取合并) ; claude = data_track 渲染 + note。
双签走 test→main。

## 背景 / 为什么
DAU 现在是每次打开页面从 live 数据实时重算的（`db.admin_data_track_dau` 扫 chat_messages/user_logs）。
`delete_user` 是硬删 + 级联，用户删号后其消息消失，**追溯性地**减少他活跃过的每一天 →
历史 DAU 随时间下降、偏少。Seven 要求：**以后每天真实数据冻结快照**；历史不恢复，只在页面标注偏少（已做，commit c86ed9a）。

同样问题也影响 DAU 页的 app-usage 列（使用DAU/平均使用时长/会话数）——一并冻结。

## 目标
北京日一旦结束，就把当天全部 DAU 指标**算一次、写死**，之后 live 数据怎么删都不影响历史。

## 【CODEX2 · db】

**S1. 新表 + 迁移** `dau_daily_snapshot`（迁移 `0017`）
- 主键 `day TEXT`（北京日 `YYYY-MM-DD`）
- 冻结 DAU 页每日行全字段：`dau, chat_dau, tracking_dau, active_events, user_messages, tracking_events, session_dau, avg_session_sec, foreground_sec, session_count`
- 同时冻结 `first_ts, last_ts`，避免页面的 Last active 在删号后变化
- `frozen_at TIMESTAMPTZ`（写入时刻）

**S2. 固化 job**（幂等·写一次）
- 首次运行只冻结昨天，以该日建立上线分界，不把更早的已知偏少历史标成准确；之后补齐从首个冻结日到昨天之间的所有缺口。
- 每个**已结束**的北京日（day < 今天北京日）若无快照行 → 按 live 单日口径计算全部指标 → `INSERT ... ON CONFLICT (day) DO NOTHING`（**绝不覆盖已冻结的**）。零活动日也写一行用于保持连续分界，但不展示在 active-day 页面。
- `admin.dau_snapshot_scheduler` 由 `core.leader` advisory lock 保证全后端单例；赢得 leadership 后立即执行，之后默认每 300 秒补缺。仅在 `FEEDLING_ASGI_BACKGROUND=1` 的正式后台进程启动。
- 注意时区：按 `Asia/Shanghai` 判"今天/已结束"。

**S3. 读取合并** —— 给我一个干净接口
- 建议 `admin_data_track_dau` 每个 day 的 row 变成：**该天有快照 → 用快照冻结值；无快照(今天/快照上线前) → live 算**，并每行加 `frozen: bool` 标志。
- 这样我渲染时：过去的天读冻结值（稳定）、今天读 live，且能给每行标"已冻结/实时"小标。
- `since/days` 逻辑不变；若 `since` 切进某个冻结日内部，该日回退 live，以保留精确的时间过滤语义。
- `admin_dau_snapshot_bounds()` 提供首日、末日和已冻结日数，DAU summary 暴露为 `snapshot_first_day/snapshot_last_day/snapshot_days`。

**S4. 真实 PG 测试**
- 固化后该天值不因后续删数据而变（冻结生效）；
- 已冻结天再跑 job 不覆盖（幂等）；
- 今天不被固化；
- 快照缺失的天回退 live；
- `frozen` 标志正确。
- 覆盖首次分界、停机后补齐、零活动日边界和 intraday `since` 回退。

## 【CLAUDE · data_track 渲染】
- 读 S3 的合并结果；每行按 `frozen` 显示"已冻结/实时"小标（实时的醒目一点）。
- 更新已加的历史 note：一旦有了首个冻结日，note 里给出**快照上线分界日**（此前偏少、此后准确）。
- 补渲染测试。

## 分界/历史
- 首个冻结日 = job 首次运行固化的最早已结束天。之前的天永远是 live（偏少），note 已说明。
- **不做历史恢复**（Seven 定）。

## 顺带（本次一起）
- 已提交 c86ed9a：DAU 页历史偏少 note（display-only）。请 codex2 顺手 review 这条 note 一并签。

## 验证
真实 PostgreSQL 整合回归 94 passed；DB、调度、渲染和迁移已由 claude/codex2 互审双签。下一步为推 test 后冒烟，再走 test→main。
