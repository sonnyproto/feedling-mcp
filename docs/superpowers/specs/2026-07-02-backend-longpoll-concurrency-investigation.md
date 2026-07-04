# Prod 负载排查 + 长轮询并发瓶颈方案空间(2026-07-02)

状态：**排查完成，已按当前代码复核一次（2026-07-03）**。本文的现场数据仍是
2026-07-02 截面；“待做/已建好”部分已按当前仓库代码修正，避免拿过期文档当事实。

## 0. 起因

用户提问：看 prod 环境负载（backend / enclave / agent-runner）；怀疑「consumer
轮询导致 backend 和 enclave 排队超时」。两类用户：VPS 用户（consumer 跑在自己
服务器）+ agent 托管用户（consumer 跑在 agent-runner 里）。

取证通道：prod RDS 只读查询（`PROD_DATABASE_URL`）+ `phala ssh feedling-enclave-v2`
（CVM `0711c9a4-afdc-40c6-ba49-d8cb95f7e850`，app_id `9798850e096d770293c67305c6cfdceed68c1d28`）。

## 1. 结论一句话

用户的假设**成立**：确有持续的排队超时。但根因不是「enclave 被轮询打满」，而是
**backend 的 gthread 模型下、~98 个常驻 consumer 各挂一个 30s HTTP 长轮询、每个
长轮询占死一个线程**，加上**内存只剩 ~1.2GB 卡死了纵向扩容**。enclave 的 decrypt
超时是 backend 饱和的**下游**后果。

2026-07-03 代码复核后，结构性判断仍成立：`/v1/chat/poll` 和
`/v1/proactive/jobs/poll` 都在 Flask/gunicorn 请求线程里用
`threading.Event().wait(timeout)` 阻塞等待；wake_bus 只负责跨 worker 唤醒/刷新缓存，
不改变“每个长轮询占一个 gthread thread”的并发模型。旧文档里若干“待做”已被实现：
consumer whoami TTL 缓存、provider 402 cooldown、独立 runner compose、supervisor
renew 线程都已在代码里存在。

> 排查中一度误判为「环境健康」——那是被**幸存者偏差**误导：backend 访问日志
> `[req]` 行在 `after_request` 才打印，**超时/未返回的请求根本不落日志**，所以只看
> 状态码会看到「全 200」。真相在 agent-runner（客户端侧）日志里。

## 2. 分层证据

| 层 | 现场数据 | 判断 |
|---|---|---|
| DB 连接 | 47 / 402，1 个 active，0 idle-in-transaction，0 锁等待 | 不是瓶颈 |
| CVM 宿主 | 8 vCPU，load 1.6/1.7/1.8（~20%） | CPU 不是瓶颈 |
| CVM 内存 | 15GB 用 13.4GB，剩 ~1.2GB，**0 swap** | **绑定约束** |
| 容器 CPU/内存 | agent-runner 86%CPU/**6.6GB**；backend 29%/2.4GB；enclave 2.36%/**516MB**；ingress 12%/46MB | 内存大头=98 consumer |
| 托管规模 | `agent_runtime_instances` 98 行全 running（64 codex+34 claude），租约/心跳全新鲜，**全部属单一 owner `f1c572b75705:1`**（prod 无独立 runner CVM） | 单进程扛全部 |
| 用户规模 | `users` 385；`chat_messages` distinct uid 126；`memory_moments` 106；即 ~259 空壳/孤儿 | 与孤儿账号 bug 一致 |
| backend 请求（30min） | 30208×200，**0×5xx**，1027×401（≈全 `uid=-`），聚合 ~17–20 req/s | 访问日志幸存者偏差 |
| 端点量（30min） | identity/changes 4847、proactive/jobs/poll 3985、users/whoami 3826、bootstrap/status 3697、chat/poll 2846… | — |
| **agent-runner（3h）** | **`poll error: timed out` 15795 次**（稳态 ~80–110/min，非突发）；`backend_error: timed out` 140；`all decrypt sources failed`（用户消息真被跳过）186 | **真·排队超时** |
| 现场并发 | backend :5001 established **150–168**（> 容量 128）；enclave :5003 **10**（排在 gthread 后面） | **超并发上限** |
| OpenRouter 402 | 某用户 `usr_a23f5afcb56808bc` 余额不足，proactive 每轮重试狂刷 ERROR（30min 207 条） | 独立小问题 |

## 3. 根因链

- **Backend**：`gunicorn --config gunicorn_conf.py(workers=4) --threads 32 --timeout 120 app:app`
  → `--threads>1` ⇒ gthread，容量 = **4×32 = 128 线程/请求**。
- 长轮询实现 `backend/chat/routes.py:468` = `ev.wait(timeout=30)`，**阻塞占一个 gthread
  线程整 30s**。98 个 consumer **持续**长轮询（一返回立刻再 poll）⇒ **任意时刻 ~98
  线程被占死，与超时时长无关**。剩 ~30 线程应付 proactive/identity/whoami/decrypt/iOS
  突发 + **enclave reentrant 回调 backend 的 whoami**（再吃线程）。峰值一超 128 ⇒
  socket backlog ⇒ 超过客户端 40s/11s 超时 ⇒ `poll error: timed out`。
- **Proactive poll 同类问题**：`backend/proactive/routes.py:746-750` 也是
  `threading.Event().wait(timeout)`，虽然 consumer 端默认 `PROACTIVE_POLL_TIMEOUT=1`
  比 chat poll 短，但它同样占用 backend gthread thread。
- **Enclave**：`python enclave_app.py` 内部起 **gunicorn gthread（`FEEDLING_ENCLAVE_WORKERS`
  compose 默认 2，代码默认 1；每 worker 32 线程）**，**不是单线程**。但
  `/v1/chat/history` 每次都 `_flask_get` 向 backend 拉密文信封 ⇒ backend 饱和时该
  fetch 超时 ⇒ `backend_error: timed out` ⇒ decrypt 丢周期。**所以 decrypt 丢周期是
  backend 饱和的下游，不是 enclave 并发问题。**
- **内存墙**：backend 4 worker≈2.4GB，想加到 8≈4.8GB 但只剩 1.2GB ⇒ **纵向扩容被内存
  否决**。6.6GB 的 98 consumer 是内存大头。

## 4. 当前代码事实（2026-07-03 复核，避免重复造）

- **reentrant whoami 已优化大半**：`enclave_app.py:_local_user_id_from_token`
  在 enclave 拿到 `FEEDLING_RUNTIME_TOKEN_SECRET` 时**本地 HMAC 验 runtime token 直接
  取 user_id，跳过回调 backend**；覆盖 `_whoami_cached` 和 `/v1/envelope/decrypt`。
  托管 consumer 通过 token 文件把 `X-Feedling-Runtime-Token` 写进 `_HEADERS`。⇒ 快路径在
  代码层存在，实际是否生效取决于 backend / enclave / runner 的 secret 是否一致。
- **consumer whoami 缓存已实现**：`tools/chat_resident_consumer.py` 默认
  `WHOAMI_REFRESH_TTL_SEC=300`，`_refresh_whoami_for_encrypted_reply()` 在 full keys 新鲜时
  不再每个 reply 都打 `/v1/users/whoami`。
- **provider 402 熔断已实现一部分**：consumer 端 proactive 路径有
  `PROVIDER_PAYMENT_COOLDOWN_SEC=600`，命中 402 / payment required / requires more credits
  后跳过后续 proactive agent call。注意：这是**进程内** cooldown，重启会丢；用户主动聊天不被 gate。
- **多节点/独立 agent-runner 已有代码和 compose**：
  `deploy/docker-compose.phala.runner.yaml` 定义独立 runner CVM，两 runner 容器、每个默认
  `AGENT_MAX_CHILDREN=8`；supervisor 纯 Postgres lease 协调，且已有独立 heartbeat /
  renew 线程、spawn 限速、provider-key envelope cache。主 compose 仍包含 `agent-runner`
  服务，真实拓扑需要看实际部署。
- **残余 whoami（3826/30min）待现场定位**：
  - (a) enclave 上没设 `FEEDLING_RUNTIME_TOKEN_SECRET` ⇒ 快路径全 None ⇒ 每次 reentrant（**配置问题**）；
  - (b) whoami 是 consumer 启动 / 401 / TTL 过期 / keys 不完整时直接打的，非 enclave reentrant；
  - (c) VPS 用 api_key 打 enclave（少量）。

## 5. 方案空间

绑定约束是**内存**；纵向加 worker/线程本可解 poll 超时，只被内存卡住。

| 方案 | 做法 | 杠杆 | 风险/代价 |
|---|---|---|---|
| **F. 卸载 consumer + 扩 backend（推荐）** | 98 consumer 挪独立 Form-B runner CVM（代码已支持）→ 主 CVM 腾 ~6.6GB → backend 4→8 worker / 32→64 线程，容量 128→256+ 吞长轮询+突发 | 高：直解内存墙 | **近零代码**（infra+config）；多一台付费 CVM + consumer 走跨 CVM ingress 多一跳 |
| A. backend 换 gevent | 长轮询变 greenlet（`ev.wait` 让渡） | 高，治本对所有客户端 | 中高：见下「A 的两个硬约束」 |
| C/E. consumer 源头改 in-CVM wake | 同-CVM 98 consumer 直接吃 wake_bus（DB LISTEN/NOTIFY）取代 HTTP 长轮询；VPS 仍 HTTP | 高，从源头砍 98 长轮询，长期最干净 | 中：改 consumer/supervisor 架构 |
| B. 独立 async 轮询微服务 | 只把 chat/proactive 长轮询挪 uvicorn/asyncio，共享 wake_bus | 高 | 中：多一套 async 基建 |
| ~~只 bump worker/线程~~ | — | — | 被内存否决 |
| ~~方案3 enclave 并发~~ | — | — | 排除：enclave 已 gthread×32，丢周期是下游 |
| ~~缩短长轮询窗口~~ | — | — | 排除：N 持续长轮询=N 占线程，与窗口无关 |

### A（gevent）的两个硬约束
1. **psycopg 是 `psycopg[binary]`（现场 `pq impl: binary`）**：psycopg3 sync 本可借
   gevent（用 `selectors` 等待），但 binary/C 加速走 C 层 epoll ⇒ **一条查询阻塞整个
   gevent hub**。须强制走 selector 等待（纯 Python wait），**有性能取舍，需隔离压测**。
2. **:9998 WS ingest 是 `asyncio.run(websockets.serve(...))`**（`backend/screen/ws.py`）：
   asyncio 与 gevent 两套事件循环，monkeypatch 后冲突 ⇒ **须把 WS 拆成独立进程**（不 patch）。

## 6. 推荐

- **救急/最低风险**：**F**——用已就绪多节点能力解开真正约束（内存），让最简单扩容生效，
  几乎不碰代码，顺带整体容量翻倍。
- **长期最干净**：~~C/E~~（**已排除**，见决策记录）。
- A 优雅但带 psycopg-binary + asyncio-WS 双风险；B 绕开两者、风险更低。

## 6.1 决策记录（2026-07-02 会话）

- **C/E 排除**：用户计划把 agent-runner 拆到**独立容器**，co-located 前提消失，in-CVM
  wake 不成立。
- **拆分拓扑 = 独立 CVM（Form B）**（用户确认）：主 CVM 腾出 ~6.6GB → `FEEDLING_BACKEND_WORKERS`
  4→8 的"买线程余量"过渡杠杆**可用**。**这个拆分本身就是方案 F 的基础设施动作。**
- **结构性推论**：consumer 一旦拆到独立 CVM 就只能 HTTP 长轮询回 backend（跨 CVM），
  "每长轮询占一线程"是**永久结构**；加 worker 只是买余量且内存线性，随增长复发。
  C/E 已排除后，**durable 根治只剩 A（gevent）或 B（独立 async 轮询微服务）**。
- **结构性方案 A/B 未定**（用户未选，留待 F 落地后按增长复发情况再决；倾向 B，风险低）。
- **whoami 链路**：① enclave reentrant(runtime-token) 已解决并部署(07-01，本地 HMAC 验)；
  ② consumer 直打已有 `WHOAMI_REFRESH_TTL_SEC=300` 缓存，残余流量需按启动/401/TTL 过期/
  keys 不完整分类；③ api_key 前台路径 → 迁 runtime token 才能本地验（未来）。whoami
  非线程杀手，主要是被长轮询饱和拖慢的受害者，F/A/B 修好背压即恢复。

## 6.2 可执行路径（已定部分）

| 阶段 | 动作 | 状态 |
|---|---|---|
| 现在 | 核线上实际 env：`FEEDLING_BACKEND_WORKERS`、`FEEDLING_ENCLAVE_WORKERS`、backend/enclave/runner 的 `FEEDLING_RUNTIME_TOKEN_SECRET` 是否一致；确认主 CVM 是否仍跑 agent-runner，或已切独立 runner compose | 待现场确认 |
| 近线 | 给 backend access log 增加 poll wait/duration、caller/auth 分类；区分 whoami 来源（consumer direct vs enclave reentrant vs iOS/API key） | 小 patch，便于避免再次被幸存者偏差误导 |
| 过渡 | agent-runner 拆独立 CVM（= F 基建）→ 主 CVM 腾内存 → `FEEDLING_BACKEND_WORKERS` 4→8（repo var，不上链；先核 `SHOW max_connections`） | 代码/compose 已具备，真实部署待确认 |
| 根治 | A 或 B（推荐 B），让 N 长轮询 ≠ N 线程 | **未定，deferred** |

## 7. 待办 / 下一步诊断

1. 查线上容器 env，而不是读 compose：backend/enclave/runner 的 worker 数和
   `FEEDLING_RUNTIME_TOKEN_SECRET` 是否实际生效。
2. 查真实部署拓扑：主 CVM 是否仍有 `feedling-enclave-agent-runner-1`；独立 runner CVM 是否
   已承接 host-all；如果两边同时 host-all，要看 `AGENT_MAX_CHILDREN` / leases 是否避免重复。
3. backend 日志区分 whoami 的 enclave-reentrant vs consumer-direct 占比。
4. whoami 401（3899/2h 全 `uid=-`）分类：runtime-token 续期竞态 vs 死号僵尸（需临时加
   auth-fail reason 日志才能定性；非负载来源，属清理卫生）。
5. 若 poll timeout 仍高，优先设计 B（独立 async poll 服务）：当前代码已证明 Flask/gthread
   poll 的结构性瓶颈仍在，worker 扩容只是买时间。

## 附：关键命令

```
# DB
psql "$PROD_DATABASE_URL" -c "select state,count(*) from pg_stat_activity group by state;"
# CVM
phala ssh feedling-enclave-v2 -t 90 -- sh -s <<'EOF'
docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}'
docker logs -t --since 90m feedling-enclave-agent-runner-1 2>&1 | grep 'poll error: timed out' | cut -c1-16 | uniq -c
EOF
```
