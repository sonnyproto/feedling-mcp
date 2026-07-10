# Model API 多配置 —— iOS 接口文档

对应 iOS PR [feedling-mcp-ios#76](https://github.com/teleport-computer/feedling-mcp-ios/pull/76)（`codex/model-api-profiles-debug`）。

后端已把「用户的 model API 配置」从单条 JSON blob 换成两张表，并暴露 7 条新端点。本文档描述**实际实现**的契约（逐字取自 `backend/hosted/setup_core.py` / `setup_routes_asgi.py` / `backend/db.py`），并给出 iOS 侧的映射与改动清单。

认证：所有端点走 `X-API-Key`（或 `Authorization: Bearer`，或旧版 `?key=`）。

---

## 概念模型（与 iOS 一一对应）

后端的两张表正是 iOS 已经建好的两层结构：

| 后端 | iOS |
|---|---|
| `model_api_credentials` 一行 | `ModelAPICredentialReference` |
| `model_api_routes` 一行 | `ModelAPIRouteProfile` |

- **credential** = 一把 provider API key（含 provider、label、base_url、密文信封、hint）
- **route** = 一个 (credential × model) 组合，带 `is_active` / `test_status` / `reasoning_effort` / `thinking_fallback`

**同一个 provider 可以存多把 key**（个人的、团队的），这正是 `credentialList` 那个「选已有凭据」UI 的用武之地。数据库层没有 `(user_id, provider, base_url)` 唯一索引。

**每个用户至多一条 active route**，由 Postgres 的 partial unique index 强制，不是靠代码自觉。

---

## 端点

### `GET /v1/model_api/routes` —— 列出全部 route

```json
{
  "active_route_id": "3f9c…",          // 无 active 时为 null
  "routes": [
    {
      "id": "3f9c…",
      "credential_id": "a71e…",
      "provider": "anthropic",
      "model": "claude-sonnet-4-5",
      "credential_label": "Anthropic Key A",
      "api_key_hint": "sk-a…451",
      "base_url": "",
      "supports_responses": false,
      "reasoning_effort": "high",       // "" 表示未设置
      "thinking_fallback": null,        // null / true / false 三态
      "is_active": true,
      "test_status": "ok",              // untested | ok | failed
      "last_test_at": "2026-07-10T08:12:03Z",   // "" 表示从未测过
      "last_test_error": "",
      "last_runtime_error": "",
      "last_runtime_error_class": "",
      "created_at": "2026-07-09T…",
      "updated_at": "2026-07-10T…"
    }
  ]
}
```

**响应中绝不含 `api_key_envelope`。** 服务端只以密文持有 provider key，且只有 TDX enclave 能解。`db.model_api_routes_list()` 在 SQL 层就不 SELECT 那一列，测试里有断言钉死。

### `POST /v1/model_api/routes` —— 新建 route

```jsonc
{
  "provider": "anthropic",
  "model": "claude-haiku-4-5",

  // ↓ api_key 与 credential_id 二选一，多给少给都是 400
  "api_key": "sk-ant-…",        // 新建一把凭据
  "credential_id": "a71e…",     // 复用已有凭据

  "base_url": "",               // 仅 openai_compatible 需要
  "label": "Anthropic Key A",   // 仅新建凭据时用；默认取 provider 名
  "reasoning_effort": "off",    // off | low | medium | high | 正整数字符串
  "thinking_fallback": true,    // true/false；缺省不持久化
  "activate": true              // 建完立刻激活（走同步测活）
}
```

- 给 `credential_id` 时，`provider` / `base_url` **以该凭据为准**，payload 里的会被忽略。
- 给 `api_key` 时**总是新建**一条 credential —— 同 provider 允许多把 key。
- 不带 `activate` → 返回 `{"route": {…}}`，新 route 处于 `untested` 且非 active。
- 带 `activate: true` → 等价于建完立刻调 activate（含同步测活），返回与 activate 相同。

失败：

| 情况 | 状态 | slug |
|---|---|---|
| 两者都给 / 都不给 | 400 | `api_key_or_credential_id_required` |
| `credential_id` 不存在 | 404 | `credential_not_found` |
| 无法建信封（缺 content pubkey / enclave 不可达） | 409 | `cannot_encrypt_provider_key` |
| `reasoning_effort` 非法 | 400 | `invalid_reasoning_effort` |
| `thinking_fallback` 非 true/false | 400 | `invalid_thinking_fallback` |

### `POST /v1/model_api/routes/{route_id}/activate` —— 切换生效

**这个端点会先同步测活，通过了才切换。**

```json
{ "active_route_id": "3f9c…", "route": { …同 GET /routes 的单条… } }
```

失败时**旧的 active route 纹丝不动**：

| 情况 | 状态 | body |
|---|---|---|
| route 不存在/不属于该用户 | 404 | `{"error": "route_not_found"}` |
| 上游测活失败 | 400 | `{"error": "provider_test_failed", "detail": "…", "status_code": 401}` |

> **为什么必须先测活**：agent-runner 的 roster 只收 `is_active AND test_status = 'ok'` 的用户。激活一条没测过的 route，用户会在下一个 15 秒 tick 从 roster 消失，supervisor 会杀掉他的 consumer 且不会自愈。所以测不过就不给切。
>
> 测活失败时该 route 会被标记 `test_status = "failed"`，UI 可以据此显示。

**切换会触发托管 agent 的 respawn**（provider/model/key 任一变化都会），最长 15 秒生效。正在处理中的那条消息不会丢，会被重新投递给新的 consumer。

### `POST /v1/model_api/routes/{route_id}/test` —— 单测一条 route

```json
{ "status": "ok", "route": { … } }
```

结果回写该 route 的 `test_status` / `last_test_at` / `last_test_error`。

> ⚠️ **当心**：如果测的是当前 **active** 的 route 且失败了，它会被标成 `failed` 但仍然是 `is_active` —— 于是用户掉出 roster、consumer 被杀，而且**不会自动接管**别的 ok route。App 侧建议在这种情况下提示用户，或者干脆不允许对 active route 手动测活。（后端是否该自动接管，待产品决策。）

失败：404 `route_not_found` / 400 `provider_test_failed`。

### `DELETE /v1/model_api/routes/{route_id}` —— 删除 route

```json
{ "status": "deleted", "active_route_id": "8b2d…" }
```

删的若是 active route，后端**自动接管** `updated_at` 最新的那条 `test_status = "ok"` 的 route，新 id 在 `active_route_id` 里返回。没有候选时返回 `null`（此时用户没有生效配置，托管 agent 会停）。

失败：404 `route_not_found`。

### `PATCH /v1/model_api/credentials/{credential_id}` —— 改名 / 换 key

```jsonc
{ "label": "Team Key", "api_key": "sk-ant-new…" }   // 两者至少给一个
```

- 只改 `label` → 不联系 provider，直接改。
- 换 `api_key` → **若该凭据拥有当前 active route，会先拿新 key 对那条 route 同步测活**。测不过就整体不落库（旧 key、旧 `test_status` 全部保留），返回 400 `provider_test_failed`。测通过才写入，并把该凭据下**非 active** 的 route 全部标回 `untested`。

成功：`{"status": "ok"}`

失败：404 `credential_not_found` / 400 `nothing_to_update` / 400 `provider_test_failed` / 409 `cannot_encrypt_provider_key` / 500 `model_api_credential_write_failed`

### `DELETE /v1/model_api/credentials/{credential_id}` —— 删除凭据

```json
{ "status": "deleted", "active_route_id": "8b2d…" }
```

级联删除该凭据派生的所有 route。若其中含 active route，按上面的规则自动接管。

失败：404 `credential_not_found`。

---

## 保持不变的端点（旧版 App 无感）

`POST /v1/model_api/setup` 的**路径、请求体、响应体全部不变**，语义改为幂等 upsert：

- 若当前 active route 的 credential 的 `(provider, base_url)` 与请求匹配 → 更新那把 key
- 否则 → 新建一条 credential
- 然后 upsert route、测活、激活

所以旧版 App 反复 setup 同一套配置**不会堆积 route**。`GET /v1/model_api/get` 继续返回 active route 的扁平投影：

```json
{ "config": { "configured": true, "provider": "anthropic", "model": "claude-sonnet-4-5",
              "base_url": "", "api_key_hint": "sk-a…451", "test_status": "ok",
              "last_test_at": "…", "last_test_error": "", "created_at": "…",
              "updated_at": "…", "privacy_mode": "tdx_cvm_backend_runtime_option_a",
              "reasoning_effort": "high", "thinking_fallback": true } }
```

`reasoning_effort` / `thinking_fallback` **仅在设置过时出现**。无 active route 时 `config` 是 `{"configured": false}`。

`POST /test`、`POST /driver`、`DELETE /delete`、`GET /runtime`、`GET /key_envelope` 契约同样不变。

---

## iOS 侧映射

`ModelAPIRouteProfile` ← `GET /routes` 的 `routes[]` 单条：

| iOS 字段 | 后端字段 | 备注 |
|---|---|---|
| `id` | `id` | UUID 字符串 |
| `credentialID` | `credential_id` | |
| `provider` | `provider` | 与 `ModelAPIProvider.rawValue` 一致 |
| `model` | `model` | |
| `credentialLabel` | `credential_label` | |
| `apiKeyHint` | `api_key_hint` | 已是 mask（`sk-a…451`） |
| `baseURL` | `base_url` | |
| `status` | `test_status` | `ok`→`.ready`、`failed`→`.failed`、`untested`→`.untested` |
| `issueText` | `last_runtime_error_class` ?? `last_runtime_error` ?? `last_test_error` | 与现有 `modelAPIConfigIssueText` 的优先级一致 |
| `source` | — | 恒为 `.server` |

`activeRouteID` ← 响应顶层的 `active_route_id`。

> **顺带修好一个 iOS 侧的死代码**：`ModelAPIConfig.lastRuntimeError` 此前恒为 `nil` —— 因为 `last_runtime_error` 只在 `GET /v1/model_api/runtime` 返回，从来不在 `GET /get` 里。所以 `modelAPIConfigIssueText` 的 runtime 分支永不命中。现在 `GET /routes` 直接带上了这个字段，`issueText` 能真正工作了。

## iOS 要改的三处

1. **`refresh()`** —— 改调 `GET /v1/model_api/routes`，直接得到 `routes[]` + `active_route_id`。不再需要把 `GET /get` 的单条 config 包装成 `serverRoute(from:)`，也不再需要 `ModelAPIRouteProfile.serverRouteID` / `serverCredentialID` 那两个占位 UUID。

2. **`select(_:)`** —— 那句 `// The release backend currently returns only one route, so there is nothing else to switch to` 可以删掉了。改调 `POST /v1/model_api/routes/{id}/activate`。

   注意这个调用**会走一次真实的上游测活**，可能耗时数秒（取决于 provider）。UI 要给 loading 态，并处理 400 `provider_test_failed`（把 `detail` / `status_code` 映射成用户可读文案）。成功后用响应里的 `active_route_id` 更新本地状态。

3. **`save(_:)`** —— 两个选择：
   - 继续用 `setupModelAPI(...)`（`POST /setup`）。它现在是幂等 upsert，行为正确，但**只能操作 active route 的那条 credential**，无法为同一 provider 新建第二把 key。
   - 改用 `POST /v1/model_api/routes`（带 `activate: true`），并在 `draft.credentialID` 命中已有凭据时传 `credential_id` 而非 `api_key`。这才能真正支撑 sheet 里「选已有凭据 / 新建凭据」那两条路径。

   `ModelAPIConfigurationDraft` 已经带了 `credentialID` / `activateAfterSave`，正好对上 `POST /routes` 的 `credential_id` / `activate`。

`ModelAPIDebugStore` 那套 `UserDefaults` 里的本地 route 可以整个删掉了 —— 后端现在是真的多 route。

---

## 错误 slug 速查

新增的都已登记在 `docs/API_ERRORS.md`。iOS 需要为这些补本地化文案：

| slug | HTTP | 何时 |
|---|---|---|
| `route_not_found` | 404 | route id 不存在或不属于该用户 |
| `credential_not_found` | 404 | credential id 同上 |
| `api_key_or_credential_id_required` | 400 | `POST /routes` 的 `api_key` / `credential_id` 必须且只能给一个 |
| `nothing_to_update` | 400 | `PATCH /credentials` 两个字段都没给 |
| `invalid_thinking_fallback` | 400 | `thinking_fallback` 不是 true/false |
| `model_api_credential_write_failed` | 500 | DB 写失败（罕见） |
| `model_api_route_write_failed` | 500 | DB 写失败，或 route 被并发删除 |

已有的 `provider_test_failed`（400，带 `status_code`）、`cannot_encrypt_provider_key`（409）、`invalid_reasoning_effort`（400）语义不变。

---

## 行为变化提醒

**`thinking_fallback` 从 per-user 变成了 per-route。**

它原本是一个全局开关（存在旧 blob 顶层），现在跟着 route 走。理由是「模型能不能产出原生 thinking」是 model 的属性 —— Claude Sonnet 有原生 thinking 不需要 fallback，gpt-4.1-mini 没有则需要。

所以用户切换 route 时，这个开关会跟着变。迁移把每个老用户的原值原样给了他那条唯一的 active route，**升级瞬间行为不变**。

App 侧若要暴露这个开关，应该放在「每条 route 的设置」里而不是全局设置里。目前 sheet 还没有这个字段，`POST /setup` 和 `POST /routes` 都接受它。
