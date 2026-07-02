# Genesis mode-aware onboarding — 真实 test 部署 e2e 方案

配套实现:`feat/genesis-onboarding-fix`,已合 test(`b167e9d`)。执行人:Codex。

## 环境 / 前置
- API:`https://test-api.feedling.app`(已部署本次改动;跑前确认 test deploy 成功)。
- Provider:**deepseek**(model 用 `deepseek-reasoner` 或账号已配的)。key 走 env **`GENESIS_E2E_PROVIDER_API_KEY`**(hx 已提供 deepseek key,自己 export,别落仓库/别打印)。
- 工具:`tools/genesis_e2e.py`(upload/verify);如需传 `mode` 或做 B/C/D/E/F,**按需扩展该工具**(加 `--mode` 透传到 plaintext 请求体)。
- Fixture:用 `Docs/sample-ai-companion-persona-fixture.json` / `Docs/onboarding-raw-fixtures/`,或造一份**含若干条明确、可核对事实**的合成聊天史(带 ground-truth 事实清单,便于核准确性)。

## 铁律
- 真实部署 e2e(不是本地 fake-decrypt):验的是 AEAD/enclave 真解密。
- key 只走 env,绝不打印/落盘。
- 每个场景用 `--register` 开 **throwaway 账号**,跑完清理(host-all 卫生)。

## 场景与断言(对应收敛版 6 断言 + R2/R5,真机版)

### A. onboarding —— R2 身份吃满卡
- 步骤:`--register` 开账号配 deepseek → 上传大 fixture(mode=onboarding)→ verify 到完成。
- 断言:
  - `chat_ready=true`;identity 有 name/dimensions/category/self_introduction。
  - **memories_created 明显 > core(~5)**——证明前台真写了全量(不是还只写 5 条)。
  - identity/greeting **命中 fixture 的关键 ground-truth 事实**(对比修前更全;至少不漏明显事实)。
  - greeting 非空。

### B. add_memory —— 只加记忆,不碰身份/天数
- 在 A 的已 onboarding 账号上,上传一段**新记忆材料**(mode=add_memory)。
- 断言:
  - 花园 memory 条数**增加**。
  - identity **逐字节不变**(存一份 A 完成后的 identity,B 后比对)。
  - `relationship_started_at` / 相处天数 **不变**。
  - 不产生 voice/persona 写入(job 完成、`identity_status=skipped`)。

### C. update_identity —— 只换身份,不写记忆/不动天数
- 在已 onboarding 账号上,上传**新角色卡**(mode=update_identity)。
- 断言:
  - identity **变了**(body 变;name/维度等按新卡)。
  - memory 条数 **不变**(没偷写记忆)。
  - `relationship_started_at` / `created_at` / anchor 字段 **保留不变**。

### D. update_identity 无身份 → 409
- **全新** `--register` 账号(没 onboarding),直接 update_identity 上传。
- 断言:HTTP **409**,error=`identity_not_initialized`;不硬造身份卡。

### E. mode 不串(幂等带 mode）
- 同一份材料内容(同 input_hash),先 add_memory 再 update_identity。
- 断言:**两个 job 都执行**,第二次不被"复用旧 job"跳过。

### F. provider 硬错误 → failed(R5,不被假身份吞)
- 用一个**无效/欠费的 provider key** 跑 onboarding。
- 断言:job 最终 **`failed`**、带真实 error(不是写一张 generic 身份当成功、不是无限 processing)。

## 交付
- 一份跑测报告(中文):每个场景 pass/fail + 关键数值(memory 条数、identity 前后 diff、相处天数、HTTP 码、job 状态/error)。
- A 场景附"身份/记忆 vs ground-truth"的准确性对照(漏抽/误抽/命中)。
- 任何 fail → 指出是实现问题还是 fixture/环境问题。
