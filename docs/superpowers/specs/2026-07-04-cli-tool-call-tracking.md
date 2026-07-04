# Spec: CLI/Agent 工具调用追踪（供 data-track 消费）

- **作者**：Claude（看板侧）
- **面向实现**：zhihao（埋点侧）
- **分支**：test（改动只落 test，永不动 main）
- **状态**：待实现
- **关联**：`backend/admin/data_track.py` 重设计（进行中，未提交）

---

## 1. 背景 & 目标

工具调用（`web_search` / `memory_index` / `memory_fetch` / proactive
`agent_tool_calls_v2` / state actions）目前只存在于：

1. 单次回合的**临时 trace**（`model_api_runtime/memory_tools.py` 的 `trace["tool_calls"]`）；
2. 需用户手动开启的 **`debug_trace`**（`v1_flow_trace` blob，绝大多数 prod 用户是关的）。

`tools/chat_resident_consumer.py` 的 turn-timing（`f1a8149` / `9a36fe8`）也只是
`log.info` 观测行，**未落库**。

⇒ 这些都**无法跨用户聚合**。

**目标**：每次工具调用留一条**持久、可按 user_id GROUP BY** 的记录，覆盖**聊天**和
**主动（心跳 / 屏幕）**两条 lane，让 data-track 能回答：谁在用什么工具、用得多不多、成没成、
分别在 chat 还是 proactive 场景。

## 2. 数据形状（唯一硬要求）

新增一条 `user_logs` 流 **`tool_calls`**（沿用 `tracking_events` 那套机制），
**每执行一个工具写一条**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ts` | epoch/iso | 时间戳 |
| `tool` | str | 工具名，如 `web_search` / `memory_index` / `memory_fetch` |
| `context` | enum | **`chat`** / **`proactive_heartbeat`** / **`proactive_screen`** —— 拆分的关键 |
| `ok` | bool | 调用是否成功 |
| `latency_ms` | int? | 可选，单次耗时 |
| `error` | str? | 可选，失败时短原因（不含敏感内容） |

`context` 判定用现成信号即可：

- **chat** lane → 固定 `chat`；
- **proactive** lane → 从 job 的 `job_kind` / `wake_kind` / `trigger` 判：
  `screen_watch` / `scene_change` / `screen_tick` / `broadcast_opened` → `proactive_screen`；
  以 `heartbeat` 开头 → `proactive_heartbeat`。
  （看板侧已有同款分类 `data_track._classify_proactive_kind`，两边对齐即可。）

## 3. 埋点位置（建议，你定）

- **聊天**：`backend/hosted/chat_routes.py` / `backend/hosted/turn.py` 中工具被解析并执行处
  （`_model_api_chat_tool_calls` / `_agent_tool_calls_from_reply` 附近）。
- **主动**：`backend/proactive/agent_protocol_v2.py::agent_tool_calls_v2` 执行处；
  若能复用 `proactive/runtime_v2.py::_record_turn_metric` 的路径就复用。

## 4. 复用 & 约束

- 复用 `core/store.py::append_tracking_event` 那套 `db.log_append` + `db.log_trim` +
  `db.log_prune_older_than`。
- 保留期对齐 `tracking_events`：`FEEDLING_TRACK_EVENT_RETENTION_DAYS`（90d）/ `_MAX`（2000）。
- **埋点必须永不影响回合**：`try/except` 包住，失败静默（参考 turn-timing 的 `noqa: BLE001`）。

## 5. 看板会怎么用（验收标准）

`backend/db.py::admin_data_track_snapshot` 加一条：

```sql
SELECT user_id, doc->>'tool' AS tool, doc->>'context' AS context,
       (doc->>'ok')::bool AS ok, COUNT(*)::int
FROM user_logs
WHERE user_id = ANY(%s) AND stream = 'tool_calls'
GROUP BY user_id, tool, context, ok
```

看板即可展示：每用户 / 全量的**工具使用次数、成功率、chat vs 心跳 vs 屏幕的分布**。
→ 只要 `tool_calls` 流的 `context` 三枚举和本文一致，看板侧零额外协调就能接。

## 6. 非目标

- **不**替代 turn-timing 日志（那是延迟观测，互补关系）。
- **不**记录工具的**参数 / 返回内容**（只记 名字 + 成败 + 场景 + 耗时，避隐私与体积）。
- **不**在客户端 `chat_resident_consumer` 落库（host-all 用户跑在服务端；埋点走后端回合路径，
  覆盖面才全）。

---

## 分工备注

- 看板侧（`admin/data_track.py` + `db.py` 的 `admin_data_track_snapshot`）：Claude，进行中。
- 本埋点（`tool_calls` 流）：zhihao。
- codex（main）2026-07-03 已确认不碰 `admin_data_track_snapshot` / `_record_turn_metric`，无重叠。
