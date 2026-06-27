# V1 cutover 测试 · 给 Codex 跑(非迁移)

> 这份是**能自动跑的**部分,交给 Codex。真机手测部分见另一份(hx 真机文档)。
> 仓:`feedling-mcp`(后端),分支:`test`(cutover 已在 test:`95522e8`)。
> 跑法分两档:**1 = pytest(确定性、必跑)**;**2 = live e2e(对 test 部署,需凭证)**。

---

## 档 1 · pytest 套件(确定性,先跑这个 — 不需 live 凭证)

**环境**(CI 同款):Python 3.12 → `pip install --require-hashes -r backend/requirements.lock` → `pip install pytest pytest-asyncio requests`。部分用例要 Postgres,设 `FEEDLING_TEST_PG=postgresql://postgres:postgres@127.0.0.1:5432/postgres`。

**命令(仓根)**:
```bash
python -m pytest \
  tests/test_genesis_service.py \
  tests/test_genesis_worker.py \
  tests/test_genesis_prompts.py \
  tests/test_genesis_llm_client.py \
  tests/test_agent_runtime_genesis_gate.py \
  tests/test_enclave_runtime_token.py \
  tests/test_runtime_token_auth.py \
  tests/test_agent_runtime_spawners.py \
  tests/test_agent_runtime_supervisor.py \
  tests/test_hosted_agent_runtime_cutover.py \
  tests/test_io_cli_auth.py \
  tests/test_enclave_visual_plaintext.py \
  tests/test_identity_actions.py \
  tests/test_history_import_identity.py \
  -v
```
> `test_enclave_visual_plaintext.py` 是 test 最新加的(`9b25544 fix(enclave): decrypt raw photo envelopes`)—— 照片走 enclave 解密的回归,和闸门⑤/E2 图片路相关,一并跑。

**通过标准:全部 PASS。** 文件 → 闸门映射(哪条红了对应哪块坏):

| 测试文件 | 覆盖的闸门 / 行为 |
|---|---|
| `test_genesis_service.py` / `test_genesis_worker.py` / `test_genesis_prompts.py` / `test_genesis_llm_client.py` | **闸门① genesis 蒸馏管线**(create/chunk/finalize/worker/persona_build/identity/memory) |
| `test_agent_runtime_genesis_gate.py` | founding genesis 阻塞 spawn、backfill **不**阻塞 |
| `test_enclave_runtime_token.py` / `test_runtime_token_auth.py` | **闸门③ P0** enclave 收 runtime token 解密 |
| `test_agent_runtime_spawners.py` | **③** persona decrypt 注入 + **⑤** photo allowlist(`_IO_CLI_VERBS`) |
| `test_agent_runtime_supervisor.py` | **④** `persona_version` pickup / respawn / lazy backfill / cap / cooldown |
| `test_hosted_agent_runtime_cutover.py` | **cutover 路由**:`should_route = enabled and not has_image`(图片回 legacy)、gateway 需 LITELLM |
| `test_io_cli_auth.py` | **⑤** io_cli verb allowlist |
| `test_enclave_visual_plaintext.py` | **⑤/E2** enclave 解 raw photo envelope(test 新增 `9b25544`) |
| `test_identity_actions.py` / `test_history_import_identity.py` | **identity 写读**(独立于 voice,不受 cutover 影响) |

> 想一把全跑回归:`python -m pytest tests/ -q`(慢,但最稳)。

---

## 档 2 · genesis 全链路 live E2E(对 test 部署 — 验真上传→蒸馏→done)

**前置**:`<TEST_API_URL>`;一个能蒸馏的 model provider key(`--register` 时带 provider/model)。worker 必须开(`FEEDLING_GENESIS_WORKER_ENABLED=1` + `FEEDLING_RUNTIME_TOKEN_SECRET` + `FEEDLING_ENCLAVE_URL`)。

**步骤**:
```bash
# 1) 造个测试历史
printf 'me: 我家狗叫蛋子\nher: 蛋子今天乖吗？\nme: 上周去了西湖\n' > /tmp/t.txt

# 2) 注册+加密上传+finalize(history 源)
python tools/genesis_e2e.py upload \
  --api-url <TEST_API_URL> --register \
  --provider anthropic --model <model_id> \
  --transcript /tmp/t.txt --source-kind history
# → 输出含 job_id + api_key(记下)

# 3) 轮询到 done + 隐私抽检
python tools/genesis_e2e.py verify \
  --api-url <TEST_API_URL> --api-key <上一步的key> --job-id <上一步的job_id>
```
**通过标准**:
- verify 输出 `"ok": true, "state": "done"`。
- 隐私抽检 `status_payload_raw_keys` 为**空**(状态接口绝不回明文 transcript/raw_text/chunks/plaintext)。
- (可选)`GET /v1/genesis/imports/<job_id>` 里出现 `persona` blob / identity / memory 产出。

**多源(对应真机 A2)**:把 `--source-kind` 换成 `companion_persona` / `user_profile` / `memory_summary` 各跑一遍(同一 user,用 `--api-key --user-id` 不再 register),再 verify 每个 job 都 done。

---

## 档 2b · 针对性 curl(可选)

```bash
# genesis 状态
curl -s -H "Authorization: Bearer <api_key>" <TEST_API_URL>/v1/genesis/imports/<job_id> | jq '{job:.job.status, state:.state.status}'

# 闸门④ voice backfill endpoint(对一个"老用户" api_key)
curl -s -X POST -H "Authorization: Bearer <api_key>" <TEST_API_URL>/v1/genesis/persona_backfill | jq .
# 期望:创建一个 source_kind=companion_persona_backfill 的 genesis job(或 already/exists 幂等)
```

---

## 给 Codex 的一句话

> 先跑**档 1 pytest**(必须全绿,这是 cutover ①③④⑤ + 路由 + identity 的回归网);再跑**档 2 genesis live e2e**(验真部署上 上传→蒸馏→done + 隐私不泄)。红了对照上表定位。真机交互(onboarding UI / 聊天 voice / Garden)不在你这档,归 hx 真机。
