# 落卡 Capture Lane — 验证交接文档（给工程师独立验证用）

> 日期：2026-06-26 ｜ 分支：`test` ｜ 范围：**VPS / resident 路**（API 用户经 agent_runtime 跑同一 consumer，自动覆盖）
> 目的：把"会话断点触发的回顾落卡"机制建到 VPS 上。承接《IO 记忆 · 落卡 + Dream 完整方案》第一部分（Dream 是后续 Phase）。

---

## 0. 一句话

记忆落卡**不是 agent 每轮主动调工具**，而是**会话断点**（静默超时 / 退后台·息屏 / 轮数兜底）触发后，由 resident agent **回看对话窗口**做一次落卡评估，产出 0–2 张"厚卡"写入加密记忆库。落卡与"AI 主动找你说话"是**两条正交的 lane**：关掉主动陪伴 **不会** 停止记忆落卡。

---

## 1. 改了什么（commits，均在 `test` 分支）

| commit | 内容 |
|---|---|
| `2136073` | **PR A** capture job 基座：typed job（`job_kind=memory_capture` + `capture_key` 幂等 + 单飞）、poll 对 capture job **跳过 reach-out wake gate**、consumer 按 kind 分发 |
| `457ba01` | **PR B** 触发 coordinator：`append_chat` 钩子更新 window/轮数、`/v1/device/events` 边界事件、`/v1/capture/tick` 静默兜底、`capture_state` blob 去重 |
| `e148da2` | **PR C.1** 落卡 prompt + reply parser（`build_capture_prompt` / `parse_capture_cards`） |
| `bf2cf66` | **PR C.2** 原生 capture handler：window→原生 `call_agent`→parse→封 v1 信封→`/v1/memory/actions`，**不写 chat、不过投递门、不走 run_tool_loop_v2** |
| `a3dc780` | io_cli memory/screen 子命令认证修复（host-all token 兼容） |
| `858b80f` | capture handler 取 enclave identity 时 `verify=False`（仅对 enclave URL；后端调用仍验 TLS） |

## 2. 触发与流程

```
会话断点（任一命中）
  ├─ 静默超时（默认 FEEDLING_CAPTURE_QUIET_SEC=1200s）   ← 后端 /v1/capture/tick 兜底（consumer 60s 调一次）
  ├─ 退后台/息屏/显式收尾（iOS app_presence background → /v1/device/events）
  └─ 轮数兜底（默认 FEEDLING_CAPTURE_TURN_BACKSTOP=24 轮）
        │  （min interval 防抖 FEEDLING_CAPTURE_MIN_INTERVAL_SEC=600s；同一 window 多信号 → capture_key 折叠成一个 job）
        ▼
   enqueue memory_capture job（单飞 + capture_key 幂等）
        ▼
   resident consumer poll/claim（capture job 不走 reach-out wake gate）
        ▼
   _process_capture_jobs：读 window（after/until message ids）+ 现有桶(/v1/memory/buckets)+线索(/v1/memory/threads)+identity
        ▼
   原生 call_agent(落卡 prompt) → {"cards":[...]}（action: add/merge/supersede/noop；type: event/fact/quote/moment）
        ▼
   每张卡 consumer 端封 v1 信封（客户端加密）→ /v1/memory/actions（add / supersede）
        ▼
   completed：cards_added / cards_superseded / noop_reason 落 status
```

**关键不变量**：capture gate 绝不看 `ambient / scheduled / delivery / broadcast / user_state`。**关主动陪伴 ≠ 停记忆**（有测试钉死）。

## 3. 源文件

- `backend/proactive/capture_jobs.py` — capture job 基座（typed job、enqueue、单飞、幂等）
- `backend/proactive/capture_scheduler.py` — 触发 coordinator（capture_state、断点判定、quiet/turn 阈值）
- `backend/memory/capture_prompt_v1.py` — 落卡 prompt + `parse_capture_cards`
- `tools/chat_resident_consumer.py` — `_process_capture_jobs`（原生 handler）、`fire_capture_tick`、按 kind 分发
- `backend/proactive/routes.py` — `/v1/capture/tick`、`/v1/device/events` 边界、capture 状态字段
- `backend/core/store.py` — `append_chat` 钩子 + capture status 字段白名单

## 4. 测试清单（自动化，全部 PASS）

跑法（每个测试模块用独立 throwaway Postgres，CI 同款）：
```bash
# 需要可达的 Postgres（维护库）。CI 用 FEEDLING_TEST_PG。
export FEEDLING_TEST_PG="postgresql://<user>@127.0.0.1:5432/postgres"
python -m pytest tests/test_capture_prompt_v1.py tests/test_proactive_jobs.py tests/test_chat_resident_consumer.py -v
```

### 4.1 落卡 prompt + parser — `tests/test_capture_prompt_v1.py`（11 passed，无需 DB）
- `test_prompt_renders_with_context_and_escaped_json`
- `test_prompt_falls_back_to_neutral_defaults`
- `test_parse_normal_card`
- `test_parse_empty_is_clean`（"没值得记的" → 空，无错）
- `test_parse_drops_noop`
- `test_parse_coerces_insight_reflection_out`（落卡只产 event/fact/quote/moment）
- `test_parse_handles_json_fence`（```json 围栏）
- `test_parse_handles_prose_wrapped_and_clamps`（散文包裹 + importance/pulse clamp 到 [0,1]）
- `test_parse_garbage_returns_reason`
- `test_parse_drops_hollow_card`（无 summary/content 丢弃）
- `test_parse_caps_threads_at_eight`

### 4.2 基座 + 触发 — `tests/test_proactive_jobs.py`（48 passed，含以下 capture 项）
- `test_capture_job_polls_and_claims_when_ambient_is_off` ← **invariant：关主动陪伴仍落记忆**
- `test_capture_device_boundary_ignores_proactive_switches` ← **invariant**
- `test_capture_enqueue_single_flight_per_user`（单飞）
- `test_capture_enqueue_is_idempotent_by_capture_key`（幂等）
- `test_capture_coordinator_dedupes_same_window_across_signals`（同窗口多信号折叠成一个）
- `test_capture_quiet_tick_noops_without_new_messages`（无新消息静默 tick 不落）
- `test_capture_turn_backstop_enqueues_only_when_due`（轮数到点才落）
- `test_capture_completion_advances_state_and_blocks_same_window`（完成后推进 state、同窗口不重复）

### 4.3 原生 handler — `tests/test_chat_resident_consumer.py`（109 passed，含以下 capture 项）
- `test_capture_job_add_card_writes_envelope_without_chat_or_delivery` ← **不写 chat / 不投递**
- `test_capture_job_supersede_card_writes_supersede_action`
- `test_capture_job_supersede_without_target_falls_back_to_add`（无 target 不丢，回退 add）
- `test_capture_job_empty_cards_completes_noop_without_memory_write`
- `test_capture_job_bad_json_fails_without_crash_or_memory_write`（坏输出不崩、不写）
- `test_capture_identity_context_prefers_enclave_plaintext_and_filters_ciphertext`
- `test_capture_get_json_disables_tls_verification_for_enclave_only`（仅 enclave 关 TLS 验证）
- `test_fire_capture_tick_posts_backend_endpoint`

### 4.4 回归（确认没弄坏 resident 主线）
本地 throwaway PG 跑过：resident 主线回归套件 + Round-3 V2 全绿（无回归）。CI（GitHub Actions "CI" workflow）对每个 commit 跑全套并 green。

## 5. VPS 真机 e2e（已实跑，建议工程师复现）

测试账号 `usr_fcdd97377f29c7a7`（VPS resident consumer）。复现步骤：
1. 确认后端是新镜像：`POST /v1/capture/tick` 返回非 404（端点存在）。
2. 确认 consumer 是新码并跑起来：启动日志含 `capture_tick=True capture_tick_interval=60s`。
3. 有一段足够长的静默窗口时（或 `POST /v1/device/events` 报 `app_presence` background），coordinator enqueue 一个 memory_capture job。
4. consumer 自动 claim 并跑落卡 handler（调真 agent）。
5. 验证：`POST /v1/memory/index` 卡数增加；`POST /v1/memory/fetch {ids:[...]}` 看新卡全文。

**实跑结果（2026-06-26）：**
```
capture tick enqueued=True reason=enqueued quiet_for=10249
capture job completed id=cap_ea5855e783364034 cards=2 added=2 superseded=0 identity=True
```
- memory 卡数 **38 → 40**（静默触发 → handler 回看 **55 轮** → 写 2 张卡 → completed，**未写 chat、未投递**）。
- 落卡质量（解密后）：两张都归入"我们的关系"桶（复用、未造近义新桶），importance/pulse 分级合理，内容是"厚"的归纳（非流水账）。

## 6. 成功标准（验证应满足）

- [ ] 自动化测试：4.1/4.2/4.3 全 PASS；resident 主线回归无新增失败。
- [ ] invariant：`ambient/proactive` 关闭时，capture 仍能 enqueue + claim + 落卡（见 4.2 两项）。
- [ ] handler hard rule：capture 路**不写 chat、不过投递门、不走 run_tool_loop_v2**（见 4.3）。
- [ ] 去重：同一 window 多信号只产生一个 job；完成后同窗口不重复（4.2）。
- [ ] 加密边界：明文不入库；卡经客户端 v1 信封加密后写 `/v1/memory/actions`。
- [ ] VPS e2e：能真实触发 → 落卡 → `/v1/memory/index` 看到新卡。

## 7. 已知待办（非阻塞，后续 Phase）

- **Phase-2**：proactive **主动唤醒(reach-out)** 目前仍走"模拟工具路"（`run_tool_loop_v2` + `/v1/proactive/tool/execute`），它功能完整（感知/记忆/屏幕工具 + send_message/schedule_wake）。要"消灭双路"，需先把**原生唤醒路补到同等能力**（原生插件 + 动作解析 + perception digest），**再退役模拟路**。直接删会回归，**勿直接删**。
- **Dream**（《方案》Part 2）：夜间纯整理 lane（合并/厚化/消矛盾），复用同一 capture 基础设施（`job_kind=memory_dream`），待排。
- 阈值（静默分钟数 / 轮数 / min interval）留实测调。
- 建议补《方案》第三部分的 **eval**（golden set + 三失败模式：碎片化/监控感/漂移）来量化落卡质量。
