# provider_smoke — Provider 适配冒烟测试

对每个 LLM provider/中转站跑完整托管 onboarding + 多轮对话全链路，在手动手机
测试前自动暴露 adapter/中转站适配问题。打线上 **test CVM**。

每个账号走的全流程（复刻真实用户的 onboarding，缺一步聊天回复就会被 bootstrap
gate 409 掉）：

```
register → model_api/setup → identity/init(写身份卡, 离开 needs_identity 阶段)
  → model_api/driver(启用托管) → chat/verify_loop(passing=true, 打开
  needs_live_connection 门) → chat/send(第1轮) → 轮询解密 → chat/send(第2轮) → 轮询解密
```

> 注意顺序：`verify_loop` 必须在第一条真实消息**之前**跑通。门关着时发的消息，
> 它的回复会被 409 且**不会重试**，于是这条消息永远收不到回复。

## 前置
- Python 依赖：`cryptography`（仓库已装）。
- 根 `.env` 里有对应 provider 的 key：`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
  `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` / `KIMI_API_KEY`。
  缺哪个，对应 provider 自动 SKIP。

## 跑
```bash
# 全部(有 key 的)
python -m tools.provider_smoke.run_smoke
# 指定子集
python -m tools.provider_smoke.run_smoke deepseek gemini
# 单账号切配置, 顺带测 driver 重新派生 + consumer 原地 respawn
python -m tools.provider_smoke.run_smoke --reuse
# 覆写环境 / 超时
python -m tools.provider_smoke.run_smoke --base-url https://test-api.feedling.app --timeout 180
```

## 判据
- 第 1 轮口令回声：回复须含唯一 token，且非 fallback 话术。
- 第 2 轮上下文：须复述上一轮 token，证明会话连续。
- 失败按阶段归类：`register / setup / identity / verify / not-hosted / no-reply / wrong-token / fallback / context-miss / network`。
  - `identity`：写身份卡失败（常见 `enclave_info_unavailable` = enclave 暂时不可达）。
  - `verify`：`verify_loop` 没在重试内 passing（consumer 没接上/冷启）。
  - `network`：连接级故障重试耗尽（CVM 网关 TLS EOF / 超时 — 多为 test CVM 不稳定）。
- 退出码：全 PASS/SKIP → 0；有 FAIL → 非 0。

## 注意
- 默认每 provider 新建账号（label `provider-smoke-<provider>`），会在 test DB 留账号；
  `--reuse` 用单账号、零新增。
- model 字符串集中在 `matrix.py`，provider 改版时改这一处。
- openai_compatible 默认用 Kimi/Moonshot 端点（`matrix.py` 里的 `base_url`/`models`），按需改。
