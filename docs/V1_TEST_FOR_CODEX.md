# V1 完整测试方案 · 给 Codex 跑(整个 v1,除迁移)

> 范围 = **整个 v1 后端**(host→agent_runtime cutover 版),老卡迁移除外(单独一份)。
> 仓 `feedling-mcp`,**在分支 `feat/memory-card-migration` 上跑**(= 最新 `test` + 本测试文档 + `genesis_e2e` state 解析修复 + 一个休眠的迁移 keystone;cutover/被测代码与 `test` 一致,keystone 是新增的休眠动作不影响回归)。
> 三档:**档1 全量 pytest 回归(必跑)** → **档2 live 服务集成** → **档3 genesis live e2e**。红了对照文末"区→功能"表定位。

---

## 0. 环境(CI 同款)

```bash
# Python 3.12
pip install --require-hashes -r backend/requirements.lock
pip install pytest pytest-asyncio requests          # 测试专用
# 部分用例要 Postgres:
export FEEDLING_TEST_PG="postgresql://postgres:postgres@127.0.0.1:5432/postgres"
```
> ⚠️ **必须 `--ignore` 两个 live 脚本**:`tests/test_api.py`、`tests/e2e_model_api_test.py` —— 它们不是 pytest suite,是 live-server 脚本(import 即打服务/`sys.exit`),被 `pytest tests/` 收集会炸。`test_api.py` 放档2 单独跑。
> `test_memory_readside_docker_e2e.py` **不要跳**:它只 `read_text()` 读 compose 文本、不起 docker,是普通 pytest。
> 真正的环境性跳过只有:需 `dstack_sdk` 的少数 enclave 路测 / 需网络的用例 —— 缺环境时 pytest 报 skip(不是断言失败)就行。

---

## 档 1 · 全量 pytest 回归(必跑,确定性)

### 1A. 一把全跑(**这才是完整覆盖**,最省事)
```bash
python3 -m pytest tests/ -q \
  --ignore=tests/test_api.py \
  --ignore=tests/e2e_model_api_test.py
```
**通过标准**:全绿,或仅剩"缺 dstack_sdk/网络"的环境性跳过(pytest 报 skip,不是断言失败)。
> **1A 跑的是 `tests/` 整个目录 = 完整覆盖。** 下面的 1B 只是"按区分组、给定位用"的子集,**不是完整清单**,别拿 1B 当覆盖依据。

### 1B. 按区分组跑(仅诊断:1A 红了用这个缩小范围,非完整覆盖)

```bash
# —— genesis / onboarding(闸门①)——
python3 -m pytest tests/test_genesis_service.py tests/test_genesis_worker.py \
  tests/test_genesis_prompts.py tests/test_genesis_llm_client.py \
  tests/test_agent_runtime_genesis_gate.py tests/test_history_import_identity.py -v

# —— agent_runtime / cutover 路由(闸门③⑤ + 切换)——
python3 -m pytest tests/test_agent_runtime_discovery.py tests/test_agent_runtime_leases.py \
  tests/test_agent_runtime_spawners.py tests/test_agent_runtime_supervisor.py \
  tests/test_agent_runtime_tokens.py tests/test_agent_runtime_resident_contract.py \
  tests/test_hosted_agent_runtime_cutover.py tests/test_hosted_agent_runtime_driver.py \
  tests/test_hosted_runtime.py tests/test_runtime_v2_default_flag.py \
  tests/test_runtime_token_auth.py tests/test_enclave_runtime_token.py \
  tests/test_io_cli_auth.py tests/test_expected_consumer_commit.py -v

# —— voice backfill(闸门④,跨 supervisor/worker)——
python3 -m pytest tests/test_agent_runtime_supervisor.py tests/test_genesis_worker.py \
  tests/test_hosted_agent_runtime_cutover.py -v

# —— memory 读写召回 v1(非迁移)——
python3 -m pytest tests/test_memory_v1_schema.py tests/test_memory_v1_readers.py \
  tests/test_memory_v1_readside.py tests/test_memory_readside.py \
  tests/test_memory_readside_core.py tests/test_memory_readside_sandbox.py \
  tests/test_memory_index_selector.py \
  tests/test_memory_action_conformance.py tests/test_memory_m2_write_loop.py \
  tests/test_capture_prompt_v1.py tests/test_dream_prompt_v1.py \
  tests/test_hosted_memory_tools.py tests/test_hosted_memory_tool_loop.py -v

# —— identity ——
python3 -m pytest tests/test_identity_actions.py tests/test_identity_init_server_encrypt.py \
  tests/test_io_cli_identity.py -v

# —— chat 消费者 / 轮询 ——
python3 -m pytest tests/test_chat_resident_consumer.py tests/test_chat_resident_self_update.py \
  tests/test_chat_poll_claim_cas.py tests/test_chat_poll_client_release.py -v

# —— proactive / Dream / wake ——
python3 -m pytest tests/test_proactive_store_v2.py tests/test_proactive_runtime_v2.py \
  tests/test_proactive_scheduled_wake_v2.py tests/test_proactive_dashboard_v2.py \
  tests/test_proactive_observability_v2.py tests/test_proactive_tool_executor_v2.py \
  tests/test_proactive_agent_protocol_v2.py tests/test_proactive_background_v2.py \
  tests/test_proactive_jobs.py tests/test_proactive_gate_eval.py \
  tests/test_wake_bus.py tests/test_blob_wake.py -v

# —— perception / screen caption ——
python3 -m pytest tests/test_perception.py tests/test_perception_history.py \
  tests/test_perception_ingress_v2.py tests/test_ios_perception_contract_v2.py \
  tests/test_agent_perception_route.py tests/test_screen_caption_backend.py \
  tests/test_screen_caption_flag.py tests/test_frame_r2.py \
  tests/test_enclave_frame_caption.py tests/test_backfill_frames.py \
  tests/test_enclave_visual_plaintext.py -v

# —— enclave / crypto / 多租户 / 账号 ——
python3 -m pytest tests/test_multi_tenant_isolation.py tests/test_db.py \
  tests/test_store_cache.py tests/test_object_storage.py tests/test_access_modes.py \
  tests/test_account_recover.py tests/test_recover_orphan_survivor.py \
  tests/test_content_rewrap.py tests/test_enclave_dev_seed.py \
  tests/test_enclave_route_errors.py -v

# —— route-B / legacy(非 v1 主路径,目标=保 legacy 不崩,1A 已涵盖)——
python3 -m pytest tests/test_context_memories.py tests/test_enclave_routeb_readside.py -v

# —— providers / gateway ——
python3 -m pytest tests/test_provider_client.py tests/test_providers_manual.py \
  tests/test_model_api_path.py tests/test_model_api_prompts.py tests/test_litellm_gateway.py -v

# —— 杂项 v1 ——
python3 -m pytest tests/test_copytext.py tests/test_relationship_days.py \
  tests/test_semantic_analysis.py tests/test_data_track.py \
  tests/test_users_channel_broadcast.py tests/test_diagnostics_routes.py \
  tests/test_log_trim_caps.py tests/test_bootstrap_gates.py -v
```
**每组通过标准**:全绿。

---

## 档 2 · live 服务集成(对一个跑起来的后端)

CI 的做法:起后端 → 打 `test_api.py`。Codex 复现:
```bash
python3 backend/app.py > /tmp/backend.log 2>&1 &     # 或对 test 部署
python3 tests/test_api.py <API_URL> --multi-tenant
```
**通过标准**:`test_api.py` 全绿(多租户隔离 + 主要 HTTP 合同)。

---

## 档 3 · genesis 全链路 live e2e(对 test 部署 — 验真上传→蒸馏→done)

**前置**:`FEEDLING_GENESIS_WORKER_ENABLED=1` + `FEEDLING_RUNTIME_TOKEN_SECRET` + `FEEDLING_ENCLAVE_URL`;一个能蒸馏的 provider key。
```bash
printf 'me: 我家狗叫蛋子\nher: 蛋子今天乖吗？\nme: 上周去了西湖\n' > /tmp/t.txt
python3 tools/genesis_e2e.py upload --api-url <TEST_API_URL> --register \
  --provider anthropic --model <model_id> --transcript /tmp/t.txt --source-kind history
python3 tools/genesis_e2e.py verify --api-url <TEST_API_URL> --api-key <key> --job-id <job_id>
```
**通过标准**:verify 输出 `"ok": true, "state": "done"`;隐私抽检 `status_payload_raw_keys` 为**空**(状态接口不回明文)。
> 注:`verify` 工具已修 state 解析(GET 返回的 `state` 是 dict、读 `state.status`;旧版当字符串会永远判不出 done)。用本分支的 `tools/genesis_e2e.py`。
**多源**(对应真机全素材):`--source-kind` 换 `companion_persona`/`user_profile`/`memory_summary` 各跑(同 user 用 `--api-key --user-id`),verify 每个 job 都 done。

---

## 区 → v1 功能 映射(红了对照定位)

| 测试区 | v1 功能 |
|---|---|
| genesis_* / genesis_gate / history_import_identity | **新用户 onboarding**:蒸馏 voice/identity/memory(闸门①) |
| agent_runtime_* / hosted_*_cutover / runtime_token / enclave_runtime_token / io_cli_auth | **host→agent_runtime 切换**:spawn / persona decrypt(③)/ photo allowlist(⑤)/ 图片·gateway 回 legacy |
| agent_runtime_supervisor / genesis_worker(persona_backfill) | **老用户 voice backfill**(④) |
| memory_v1_* / memory_readside* / memory_index_selector / capture_prompt / dream_prompt / hosted_memory_tool* | **记忆**:写/落卡/读/召回/索引(v1,非迁移) |
| identity_* / io_cli_identity | **身份卡**:写/读/服务端加密 |
| chat_resident_* / chat_poll_* | **聊天**:消费者 / 轮询 claim / CAS |
| proactive_* / wake_bus / blob_wake | **主动陪伴**:Dream / 定时唤醒 / 网关 / gate 判定 |
| perception_* / screen_caption_* / frame_* / agent_perception_route / enclave_visual_plaintext | **感知**:ingress / 历史 / 截屏 caption / 照片解密(E2) |
| multi_tenant / db / store_cache / object_storage / access_modes / account_recover / content_rewrap | **数据安全**:多租户隔离 / 存储 / 账号恢复 / 重加密 |
| provider_* / model_api_* / litellm_gateway | **模型接入**:provider / gateway |
| copytext / relationship_days / semantic_analysis / ... | 文案 / 相处天数 / 语义 等 v1 杂项 |

---

## 给 Codex 的一句话

> 跑**档1(全量 pytest,必须全绿)**→ **档2(live test_api)**→ **档3(genesis e2e,真部署上传→蒸馏→done + 隐私不泄)**。这是整个 v1 后端的回归网(除老卡迁移)。任一断言失败对照上表定位到功能区。真机交互(app UI / 聊天观感 / Garden / 通知)归 hx 真机那份。
