# Resident(VPS)Onboarding 问题清单 + 测试要求

> 来源:2026-06-22 一次真实 VPS onboarding 实测(Hermes/OpenClaw 同机,连 test 环境
> `test-api.feedling.app`)。本文给工程师:① 今天遇到的问题与修复状态 ② 测试要求
> ③ 一个待解的开放问题(回复慢)。
>
> 状态图例:✅ 已修并验证 · 🟡 已修但**未真机/未重跑 onboarding 验证** · ❌ 未修 · ❓开放问题

---

## 1. 路由调用 + 通讯问题

### 1a. Hermes 被要求去接 agent,却误接到了 OpenClaw 🟡
- 现象:用户在跟 **Hermes**(身份"小哆啦")对话,把 onboarding 指令发给 Hermes;但 Hermes
  把 resident consumer 接到了**同机的 OpenClaw**,还改了 OpenClaw 的 `IDENTITY.md`/`BOOTSTRAP.md`。
- agent 的理由:Hermes CLI 直跑自报"Hermes"、且找不到绑定到该对话的 Hermes session,于是
  绕去 OpenClaw(可注入身份)。本质是把"agent_name 不能叫 Hermes"误套到"用哪个 runtime 当传输"。
- 已做:`io-onboarding/skill-resident-agent.md` 加了硬规则——**consumer 必须接到"收到 onboarding
  指令的那个 runtime 本身"**,多 runtime 同机不许改接"更顺手"的;runtime 自报名字是 `agent_name`
  的事,不要为此换 runtime 或改人格文件。(commit io-onboarding `ac63a70`)
- 🟡 **未验证**:该规则是 skill 约束,需用一次 **fresh onboarding**(新 agent 会话、重新 fetch skill)
  验证 agent 是否真的遵守;**且 skill 不能 100% 强制 agent**,需重点回归。

### 1b. OpenClaw 通讯不通(回消息收不到回复)✅ 已修并验证
- 现象:onboarding 显示"成功 + 发了问候",但用户回消息后一直 loading、无回复。
- 根因(两个叠加):
  1. **consumer 解析不出 OpenClaw 输出**:OpenClaw `agent --json` 把回复放在
     `result.payloads[].text`,consumer 的解析器不认 → 判 "no usable reply" → 加上
     `SEND_FALLBACK_ON_AGENT_ERROR=false` → 什么都不发。
  2. **verify_loop 假阳性**:consumer 见 verify ping 走"罐头回复"短路、**根本没调真 agent**,
     掩盖了上面的解析失败,让 onboarding 误判通过。
- 已做(`feedling-mcp` test 分支,commit `11279e9`):
  - 加显式 OpenClaw extractor,支持 `result.payloads[].text`(含多气泡);
  - verify ping 改成**有界真 agent 探活**:慢(>20s,可配 `VERIFY_PROBE_TIMEOUT_SEC`)→ 回退罐头
    ack(不冤枉健康慢 agent);完成但无可用回复 → 不 ack,让 verify 正确失败。
- ✅ **已端到端验证**:VPS 切 test 分支 consumer + 重启,`/v1/chat/verify_loop` 返回
  `passing=true`;真实消息往返成功(用户确认 IO App 能正常收到回复)。

### 1c. (1b 修复时连带发现)consumer 硬依赖 psycopg ✅ 已修并验证
- test 版 consumer 顶层 import `proactive.adapters_v2`/`runtime_v2` → `observability_v2` → `db`
  → **psycopg**;但 resident 是纯 HTTP 客户端、无 DB,用户机器 venv 没 psycopg → 切 test 分支后
  import 直接崩。
- 已做:把这两个 import 改**惰性**(只在 proactive-job 路用),聊天回复路 import 即 psycopg-free。
  (commit `2fb2b60`)
- ✅ 已在 VPS venv(无 psycopg)验证 import 通过。
- ❌ **遗留**:proactive-job 路仍需 psycopg。根治应把 `merge_wakes_v2` 从带 `db` 的模块(runtime_v2
  / observability_v2)拆出来,让 resident 完全不碰后端 DB 层。**这是给工程师的一个后端清理项。**

### 1d. iOS 发的连接信息曾是死的 MCP(背景,已修但未真机)🟡
- 旧 iOS `residentConsumerConfig` 还发 `FEEDLING_MCP_URL/KEY`(MCP 已下线 → 404),且不发
  `FEEDLING_ENCLAVE_URL`(现唯一解密源)。已改为发 `FEEDLING_ENCLAVE_URL`、去掉死 MCP。
  (commit feedling-mcp-ios `794be85`)
- 🟡 **未真机验证**:需 iOS 出新 build;本次测试用的是手动拼的连接信息,不是 App 复制的。

---

## 2. 测试要求(交给工程师)

### 2a. 把上面 1a–1d 全部测通
- 1b/1c 已端到端通过,但请在工程师环境**独立复测**(别只信本次单点验证)。
- 1a / 1d 是 🟡,**必须**补验证:
  - 1a:fresh onboarding,确认 agent 接到的是"收到指令的那个 runtime",不再绕去兄弟 runtime、
    不再改人格文件。
  - 1d:iOS 新 build,确认从 App 复制的连接信息是 `FEEDLING_ENCLAVE_URL`、能直接跑通。

### 2b. 对 onboarding 的**各种状态**做测试
不要只测"happy path"。至少覆盖:
- 全新关系 vs 已有长历史(关系天数分档);
- 记忆/身份门未达标时的拦截;
- 实时连接验证(verify_loop):agent 正常 / agent 回不出可用内容 / agent 很慢——三种都要测,
  确认 verify 该过的过、该失败的失败(不再假阳性);
- consumer 没起 / 起了但解密源不通 / 解密源 OK;
- 中途切路由、重装、换 key 后的状态。

### 2c. 对**基础 agent** 做全面测试(每种都要能接通 + 能正常回消息)
当前 consumer 对不同 agent 的输出格式支持不一(OpenClaw 就是这次踩的坑)。请逐一验证:
- **Claude Code**(`claude --print --output-format json`)
- **Hermes Agent**(`hermes chat ...`)
- **OpenClaw**(`openclaw agent --json`,输出在 `result.payloads[].text`)
- **Codex**
- (以及 HTTP 模式 OpenAI-兼容 endpoint)

每种 agent 都要验:① 输出能被 consumer 正确解析成回复 ② verify_loop 真调它能过
③ 多气泡 / 带图 / thinking 等形态不丢。**目标:新增一个 agent 入口时有据可依、不再"碰运气"。**

---

## 3. 开放问题:回复慢 ❓(请给方案,不限于我们的猜测)

- 现象:每条消息回复 ~16–19s。
- 实测拆解(本次 OpenClaw 为例):
  - OpenClaw agent 调用 **~13–16s**(大头);
  - 其中 OpenClaw 自报模型调用 ~5s,但整个子进程 ~14s → **~9s 是每条消息冷启动一个
    `openclaw agent` 子进程的 re-init**(载 workspace / 注入大量 skills+tools / resume session);
    模型侧 prompt 约 **25k tokens**(注入了一堆与聊天无关的 skill/tool)。
  - consumer 回写 ~3s(whoami + 加密 + POST);poll 延迟很小(服务端收到即唤醒长轮询)。
- **结论**:慢的主体在 agent 侧(冷启动 re-init + 重 prompt + 模型),feedling 的
  poll/解密/回写已经很快。这也是 resident 路的固有特性:回复速度 ≈ 用户自己 agent 的速度。
- **请工程师评估更好的方案**(开放,不预设答案):
  - 一个想法是"长驻 agent 服务 + consumer 走 HTTP",避免每条消息冷启动——**但不确定这是不是
    最优解**(依赖各 agent 能否常驻 + resume 同一 session)。
  - 也可考虑:产品层"秒回 ack → 后台补发深内容"的异步体验;精简注入给 agent 的 skill/tool;
    consumer 侧是否有可省的来回。
  - 想听工程师从 agent runtime / consumer / 产品体验三个角度给的判断与取舍。

---

## 附:本次涉及的改动(供工程师定位)
| 修复 | 仓库 / 分支 | commit | 文件 |
|---|---|---|---|
| OpenClaw 解析 + verify 真探活 | feedling-mcp / test | `11279e9` | `tools/chat_resident_consumer.py`(+回归测试) |
| consumer psycopg 惰性导入 | feedling-mcp / test | `2fb2b60` | `tools/chat_resident_consumer.py` |
| skill 路由硬规则 | io-onboarding / main | `ac63a70`(+`4f5d258`) | `skill-resident-agent.md` |
| iOS 发 enclave URL(去死 MCP) | feedling-mcp-ios / main | `794be85` | `CVMEndpoints.swift`、`FeedlingAPI.swift` |
| CHANGELOG | feedling-mcp / test | `92750f4` 等 | `docs/CHANGELOG.md`(2026-06-21/22 条) |
