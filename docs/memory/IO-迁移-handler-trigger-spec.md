# 老卡迁移 · handler + trigger 实现 spec(承接 substrate)

> 2026-06-27 · 基座已落 + Codex 复验过(branch `feat/memory-card-migration`):
> `memory.upgrade` keystone、`capture_jobs.memory_migrate`、`memory/migration.py`(检测/批选/state)、
> `memory/migrate_prompt_v1.py`、`tests/test_memory_migration.py`(12 passed)。
> 本 spec = 剩下的两块:**consumer handler `_process_migrate_jobs`** + **`capture_scheduler` 触发**。

---

## 0. 关键约束(已由 substrate / Codex 复验确定)

- 检测判**原始 inner**(`/v1/memory/list` 的 envelope → consumer enclave 解密),**不是** readside 适配后的。
- 写回走 `memory.upgrade`(原地、保 id、CAS、单行 upsert,写失败返回 `db_write_failed`)。
- LLM 在锁外;`memory.upgrade` 内部自带 `memory_lock` + `old_body_hash` CAS。
- migrate 与 capture/dream **不并跑**(已在 `enqueue_memory_migrate_job` 用 active-maintenance 挡)。

---

## 1. consumer handler `_process_migrate_jobs(jobs)`(仿 `_process_capture_jobs`/`_dream`)

**每个 `is_memory_migrate_job` 的 job:**
1. `claim_proactive_job(job_id)`,claim 不到就跳过。
2. **取原始批**:`GET /v1/memory/list` → moments(含 `body_ct`)。对每张 `_decrypt_via_enclave(moment)` → 原始 inner。
   - `migration.select_legacy_batch([(moment, inner)…], batch_size=8)` → `[{id, inner, old_body_hash}]`。
   - 同时 `migration.count_legacy(all)` 得 `observed_legacy_count`(供 trigger / state)。
3. 批为空 → 这用户没老卡:写 state `done`(见 §3),完成 job。
4. **渲染 + 派生**:`build_migrate_prompt(ai_name,user_name,old_cards=渲染批, vocab=GET /v1/memory/buckets+threads)` → `call_agent(prompt)`。
5. **解析**:`upgrades, unmigrated_ids, error = parse_migrated_cards(reply, allowed_ids={批 id})`。
6. **逐张写回(关键账目,Codex 提醒)**:对每个 upgrade:
   - consumer `_build_envelope(v1 inner)`(stdlib 加密,visibility=shared)。
   - `POST /v1/memory/actions` body `{type:"memory.upgrade", id, envelope, old_body_hash}`。
   - 据返回计账:
     | upgrade 返回 | 算 | 处理 |
     |---|---|---|
     | `status:ok` | **migrated** | 完成这张 |
     | `skipped:stale` | retry | 留下一轮(被并发改过) |
     | `skipped:not_found` | drop | 不再重试(卡没了) |
     | `error/db_write_failed` | retry | 留下一轮 |
   - **`unmigrated_ids`(parse 阶段漏的)也归 retry。** 即:`migrated_this_batch = ok 的数量`;`绝不**用 `error is None` 当整批完成。
7. **更新 state**:`remaining = max(0, observed_legacy_count - migrated_this_batch)`;`migration.next_state(state, migrated=migrated_this_batch, legacy_remaining=remaining)` → 写 blob(§3)。`remaining>0` 留给下个安静窗口(下轮 job)。
8. 限速/退避:沿用 capture lane;429/额度错指数退避(5m/15m/1h)。

**红线**:绝不删卡、绝不冲并发新写(upgrade 单行 upsert+CAS 已保证)、漏卡/失败一律留下轮、形态自愈(下轮按 raw 形态重新 select)。

---

## 2. trigger(`capture_scheduler`,安静窗口)

在现有安静窗口检测点(冷场/锁屏背景/轮数,跳过 reach-out gate)加:
```
state = get_blob(user, migration.MIGRATION_STATE_BLOB)
if migration.should_enqueue(state):                      # done 则不发(便宜路径)
    enqueue_memory_migrate_job(store, trigger="quiet_window_migrate",
                               migrate_key=migration.migrate_key_for_window(user_id, window_id))
```
- `migrate_key` = 每用户每窗口一个 → 幂等。
- enqueue 内部已挡 active capture/dream/migrate。
- **自愈**:done 后想再扫,可低频(如每 N 天)用 `should_enqueue(state, observed_legacy_count=…)` 或直接清 state 让其重扫;常规靠 capture replace-all 退回时下轮 raw 形态重判(§5.5-C)。

---

## 3. ⚠️ 待定的设计决策:migration-state blob 谁持久化

handler 在 **consumer**(进程外)跑,但 state blob 要 **服务端** 存、trigger 要 **便宜读**。两个选项:

- **A(推荐)新增一个薄端点** `POST /v1/memory/migration_state`(auth.require_user,body `{status,legacy_remaining,migrated_total}`),consumer handler 完成一批后调它写 blob;trigger 直接 `db.get_blob` 读。简单、与 genesis_state 写法一致。
- **B 不存 state,handler 每轮全扫**:trigger 无便宜信号 → 要么每个安静窗口都 enqueue(handler 扫到空再 no-op),要么 trigger 自己解密采样(贵)。省一个端点但触发不优雅。

**倾向 A**(一个薄写端点),与现有 `genesis_state`/`get_blob` 一致;state 只当加速缓存(真相仍是卡形态,Codex finding 3 已保证)。**这点定了 handler 才能完整接上。**

---

## 4. 测试(交 Codex)

- handler 账目:ok→migrated、stale/db_write_failed→retry、not_found→drop、unmigrated→retry、整批漏不算完成。
- trigger:done 不 enqueue;active capture/dream 时不 enqueue;自愈(done+退回老卡)能再触发。
- e2e(可选):造 2-3 张老卡 → 跑一轮 → 验 id 不变、内容不丢、变 v1 形态、并发新写不被冲。
