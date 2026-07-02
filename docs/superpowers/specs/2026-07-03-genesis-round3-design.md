# Genesis Round 3 — 改身份重建 persona(名字生效) + 有限重试 + iOS 显式 mode

状态:CC 写方案 → Codex review → 执行 → CC review。
分支:后端 `feat/genesis-onboarding-fix`(接 Round2,已在 test);iOS 在 `fix/genesis-error-hardening`(基于 main)。
前置:Round1/Round2 已合 test;`FEEDLING_GENESIS_COMBINED_MAP=true` 在 test。

---

## #A update_identity 重建 persona(修真 bug:改了名 AI 还说旧名)

### 现象 / 根因
用户改身份、名字换了,但聊天里 AI 还报旧名。根因:AI 的"我是谁"来自 **genesis_persona blob**(onboarding `persona_build` 生成的 markdown,resident agent spawn 时读 —— `spawners.py:_persona_from_blob` / `supervisor.py:646`)。旧名 baked 在这段 persona 里。而 Round1/2 的 update_identity **只换 identity 卡、没重建 persona blob**(Round2 §1.5 的 ②),于是 persona(旧名)压过 identity 卡字段(新名)。

**这修正 Round2 的 ②**:改身份**必须重建 persona**,否则改了等于没改。

### 改法(注意 persona_build 的真实输入)
`persona_build` **不吃 identity 卡**;它吃三样:`persona_material`(上传的**角色卡原文**)+ `behavior_notes` + `founding_exemplars`(后两者来自 voice)。名字是从 **persona_material(角色卡原文)** 进 persona 的。

`_run_plaintext_update_identity_job` 里,`replace_identity_preserving_anchor` 成功后:
1. 用 **`persona_material` = 本次 update_identity 上传的【角色卡原文】**(job 已有该材料)+ **现有 voice**(从当前 voice 产物取 behavior_notes/founding_exemplars)重跑 **`persona_build`(现有 prompt,不改)**。
   - ⚠️ 不是"喂新 identity 卡";喂的是**新角色卡原文**。新名字通过角色卡原文进 persona。
2. 写**新 persona blob**(`service.write_persona_artifact` / `GENESIS_PERSONA_BLOB`),使 **persona_version 变化 → supervisor 下一 tick respawn**,agent 拿到新名字/自我介绍。
3. self_introduction、signature 等也随 persona 一并更新(它们本就属人设)。

### 约束 / 注意
- persona_build 是**一次 LLM 调用**;改身份是显式动作,可接受。
- 仍**不动相处天数**;voice 复用现有(不重抽 voice_map),只重跑 persona_build。
- 若没有现有 voice(极少),persona_build 用新 identity 材料兜底,别失败。
- persona 重建失败**不该吞**:失败要把 job 标 failed(对齐 #B 的错误传播),别留"卡换了 persona 没换"的半更新态。

---

## #B combined_map / fact_write 有限重试(提成功率)

### 现状
只有身份派生有 3 次重试(`foreground_identity.max_attempts`)。combined_map、_fact_write、voice/persona 无独立重试。

### 改法
给主力抽取/写入步加**有限重试**,复用 `foreground_identity._provider_failed` 的思路区分瞬断 vs 硬错误:
- **combined_map**(每块,主力,失败面最大)、**`_fact_write`**:瞬断 / 空返回 → 重试,**cap 2 次**。
- **硬错误(provider 402 / 额度 / 鉴权)→ 不重试,立即 failed**(重试只烧钱;对齐 Round1 R5)。
- **greeting 不加重试**(已有模板兜底,失败不致命)。

### 约束
- cap 住浪费(2 次);重试只针对"能连但空/瞬断",不针对硬失败。
- 不动 prompt。

---

## #C iOS 三入口显式传 mode(Part 2,补上)

后端已支持 `mode`;老 app 靠 `client_job_id` 前缀兜底。现在让 iOS **显式传**,新老/以后都清晰:
- ChatEmptyStateView(onboarding)→ `mode: "onboarding"`
- GardenMaterialSheet → `mode: "add_memory"`
- IdentityMaterialSheet → `mode: "update_identity"`
- 加到 `uploadGenesisPlaintext`(及 history_import 兜底)请求体。显式 mode 优先于前缀。
- 在 iOS 分支 `fix/genesis-error-hardening`(基于 main,含三 sheet)上做。

---

## 互相影响
| 改动 | 影响 | 注意 |
|---|---|---|
| #A persona 重建 | update_identity(它)+ agent respawn | 只在 update_identity 触发;onboarding 本就建 persona;add_memory 不碰 |
| #B 重试 | onboarding + add_memory(都走 combined_map/_fact_write) | 硬错误绝不重试(别把 402 重试放大) |
| #C iOS mode | 三入口 | 显式 mode 优先,前缀兜底保留(老 app) |

---

## 验收(真机 e2e,复用 tools/genesis_e2e.py)
1. **#A(核心)**:已 onboarding(名=X)→ update_identity 传新名 Y 的卡 → **respawn 后聊天里问"你叫什么",AI 答 Y**(不再答 X)。persona blob 已换新名;相处天数不变、记忆数不变。
2. **#A 失败态**:persona 重建失败 → job=failed,不留半更新。
3. **#B**:注入瞬断/空 → combined_map/_fact_write 重试后成功;注入 402 → 立即 failed 不重试(可看重试次数/耗时)。
4. **#C**:三入口请求体带正确 mode;后端按显式 mode 分派(不依赖前缀)。

## 铁律
- persona_build / fact_write 等 prompt **不动**;#A 复用现有 persona_build。
- add_memory / update_identity **不碰相处天数**。
- 硬错误(402)不重试。动加密信封走真机 e2e。
