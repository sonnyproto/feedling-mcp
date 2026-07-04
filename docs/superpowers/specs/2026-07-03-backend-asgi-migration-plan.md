# Backend 全量 asyncio / ASGI 化改造计划

> 起草 2026-07-03，同日修订至 v3。**决策已定：全量 FastAPI 化执行，且不保留
> 运行时 Flask 兜底**（用户拍板）——部署形态从第一天起就是纯 FastAPI app，
> 不做混合壳、不做逐路由灰度；回滚粒度是**整镜像**（同 schema，切回旧 image
> 即回到 Flask）。Flask 代码在**开发期**保留于仓库内，仅作 parity 测试的
> 对照 oracle（测试内 import，不部署），prod 切换稳定后删除。
>
> 独立部署的 async poll service 方案作废（用户明确不想单独起 poll server，
> 其设计文档已删除）；auth core / poll core / waiter registry / wake listener
> 的设计要点已并入本计划 §7、§9。
> 负载背景见 `2026-07-02-backend-longpoll-concurrency-investigation.md`。
>
> **无兜底的代价（明示）**：收益延后到切换日——混合壳路线下 poll async 的
> 收益可以提前单独上线，纯切换路线下所有收益在 prod cutover 那天一次兑现；
> 换来的是没有双栈运行时、没有 WSGI adapter、架构一步到位。prod 用户规模
> 极小 + 同 schema + 镜像级回滚，使这个交换是可接受的。
>
> §3 的代码事实已于 2026-07-03 逐条对照仓库核实；动手前若隔了几个 session，
> 先重核一遍 §3。

## 1. 目标

把当前 Flask/gunicorn sync backend 逐步迁移为 ASGI/async-first backend，使：

- 等待型 I/O 不占 OS thread（长轮询是靶心）。
- HTTP API、poll、realtime、外部 provider/enclave 调用能统一在 async runtime 下调度。
- 保留现有 E2E 加密边界、Postgres 数据模型、iOS/API contract。
- **一次性全量切换**：开发分阶段（PR 序列），部署不分阶段——test CVM 先全量
  切换 soak，prod 在全部路由完成 + parity 全绿后一次切换；回滚 = 切回旧镜像
  （同 schema，秒级）。

## 2. 非目标

- 不重写产品业务。
- 不改 v1 envelope 格式。
- 不把 enclave app 一起 async 化；enclave 可单独评估。
- 不在第一阶段改 provider SDK/CLI runner 行为。
- 不为了“纯 async”强行重写所有 sync 代码；threadpool 兼容层是一等公民，
  低频路由永久留在 threadpool 也可接受。
- **不保留运行时 Flask 兜底**：不做混合壳、不引 WSGI adapter、不做逐路由
  灰度开关。Flask 仅在开发期作为 parity oracle 留在仓库里。

## 3. 当前主 backend 的 async 化阻力

### 3.1 Framework 与 request context（2026-07-03 核对）

当前 backend 是 Flask app，具体事实：

- `backend/app.py` 909 行、assembly-only；**app.py 里没有直接
  `register_blueprint`**——是 ~19 个领域包各自暴露 `register(app)`（accounts /
  agent / push / proactive / identity / memory / worldbook / bootstrap /
  genesis / chat / tracking / admin / content / copytext / hosted / screen /
  perception / diagnostics / onboarding_archive），内部再挂 Blueprint。
  **迁移单位就是这些包**，路由所有者表（§6.1）按包生成。
- 路由里广泛使用 Flask `request/jsonify/Response`。
- 请求钩子：`app.py:371` `before_request`（`g._req_start`）+ `:376`
  `after_request` 结构化 `[req]` access log（`dur_ms`、`uid=g.user_id`、
  Content-Length/Encoding），且**对 `?key=` 查询参数做大小写不敏感 REDACT**
  （legacy auth 允许 URL 带 API key，日志绝不能泄露）——ASGI access-log
  middleware 必须逐条继承，这是安全 parity，不是格式 parity。
- 错误处理器：`app.py:850-861` `errorhandler(401/403/503)` 返回**固定 JSON
  body**（`{"error": "unauthorized"}` / `forbidden` / `service_unavailable`）。
  FastAPI exception handler 必须对齐这些 body。
- 响应压缩：`Compress(app)`（flask-compress，`app.py:411`）。ASGI 等价物要么
  `GZipMiddleware`（只有 gzip 无 brotli），要么压缩下放 ingress——需做一次
  parity 决策并测大 response 的 Content-Encoding。
- **COMPAT 符号回灌**：app.py 尾部把 hosted 模块顶层定义统一回灌进 app 命名
  空间，供老测试/工具 `from app import ...`。迁移全程 `app.py` 必须保持可
  import；退休时单独清理（§13）。

全量迁移到 FastAPI 必须处理以上每一项的 request/response 抽象替换。

### 3.2 DB 层是 sync psycopg pool

`backend/db.py` 使用 sync `psycopg_pool.ConnectionPool`。如果在 async route 里直接调用，
会阻塞 event loop。

迁移选择：

1. 过渡期：sync DB 放 threadpool。
2. 中期：新增 async DB gateway，使用 `psycopg_pool.AsyncConnectionPool`。
3. 长期：热点查询 async 化，低频 admin/maintenance 保持 threadpool 也可接受。

### 3.3 进程内状态、import 副作用与多 worker（2026-07-03 核对）

**import 时序（`import app` 即发生；运行时永不 import app.py 后，以下每一项
都必须在新启动链里显式重建，见 §8.1 核销表）**：

- `app.py:153` `db.init_schema()` —— **alembic `upgrade head`，当前每个 worker
  各跑一次**，靠幂等在扛。asgi_app 不 import app.py，这一步必须显式落到
  启动链里（§5.2 / §8.1，骨架 PR 第一天完成），否则**新镜像会跳过 migration
  直接服务旧 schema**。
- `app.py:355` `core_wake_bus.start_listener()` —— 每 worker 一条 LISTEN daemon
  线程 + `register_handler` 缓存驱逐（如 `"users"` → reload users）。
- `app.py:345` `core_leader.run_singleton("ws", screen_ws.start)` —— Postgres
  advisory lock 选主，胜者在 daemon 线程里 `asyncio.run(websockets.serve(...))`
  绑 `:9998`（`screen/ws.py:102,108`）。该机制与 web server 无关，uvicorn 下
  原样可用（无 gevent 式 monkeypatch 冲突）。
- `backend/requirements.txt` 里 "backend MUST stay single-worker" 的注释
  **已过时**（leader election 已解决，prod 实跑 `-w 4`），迁移时顺手修正。

**threading 分布（非测试代码，出现次数）**：`core/store.py` 15、
`agent_runtime/supervisor.py` 8（跑在 runner 容器）、`proactive/runtime_v2.py` 7、
`hosted/turn.py` 6、`db.py` 4、`core/wake_bus.py` 3、`screen/ws.py` 2、
chat/proactive 长轮询 waiters（`chat/routes.py:464-468`、
`proactive/routes.py:746-750`，**迁移靶心**）、其余 genesis/hosted/accounts 1-2。

进程内状态：`UserStore` cache、chat/proactive waiters、wake_bus listener、
WS leader election、hosted runtime guard / heartbeat 读取、provider/httpx
clients。async 化时要重新定义生命周期、startup/shutdown、跨 worker wake、
cache invalidation；后台线程第一轮**全部保留为线程**，由 lifespan 统一管理，
不做"为 async 而 async"的重写。

### 3.4 外部调用混合

当前外部调用包括：

- httpx sync client 调 enclave / provider / APNs 等。
- provider SDK 或 CLI path。
- object storage boto3 sync。
- image/Pillow 处理。

这些不能简单 `async def` 包起来，否则 event loop 仍会被阻塞。实测分布：
httpx(sync)/requests/boto3 散在 **20 个文件**（`core/enclave.py`、
`provider_client.py`、`hosted/*` 5 个、`push/apns.py`、`screen/*` 3 个、
`object_storage.py`、`genesis/worker.py`、`model_api_runtime/tools.py`、
`memory_readside_core.py`、`worldbook_readside_core.py` 等），迁移到哪个包就
按 §5.0 审计哪个包，不做一次性全局替换。

### 3.5 鉴权耦合（2026-07-03 核对）

`accounts/auth.py::require_user()` 内部直接 `flask.abort(401)`（4 处）——这是
framework 耦合最深的共享函数，auth core 抽离（§7.1）必须改成抛类型化
`AuthError`，否则任何非 Flask 调用方（ASGI route、poll waiter）都没法用。

### 3.6 测试基建现状（2026-07-03 核对）

- `tests/` 129 个测试文件；conftest 每 session 建一次性 Postgres 库（机制不变）。
- **35 个文件用 Flask `test_client`** —— 路由迁到 FastAPI 后对应测试改
  `httpx.ASGITransport` / `TestClient`，迁移期双栈 fixture 并存（§14.7）。
- **5 个文件把 backend 当 subprocess 启动**（`e2e_model_api_test.py`、
  `test_multi_tenant_isolation.py`、`test_litellm_gateway.py`、
  `test_bootstrap_gates.py`、`test_agent_runtime_spawners.py`）——启动命令
  要参数化，CI 在 Flask 退休前两种命令都要跑。
- 部分老测试依赖 §3.1 的 COMPAT 符号回灌，`app.py` 可 import 是硬前提。

## 4. 总体路线

**开发分阶段，部署一次切换。** 没有运行时兜底后，"阶段"指的是 PR/开发顺序
与 test 环境验证顺序，不是 prod 流量灰度：

1. **抽 shared core**（→ §7 / Phase 0-1）：auth、response contract、DB access
   boundary、observability（framework-neutral，Flask 与新 app 都能调，保证
   parity 可测）。
2. **新 app 骨架 + 启动链**（→ §8 / Phase 2）：`asgi_app.py`、
   lifespan/on_starting 把 §3.3 的 import 副作用**第一天全部显式重建**（无
   Flask import 可依赖）、access-log middleware、error mapping。
3. **poll 原生 async**（→ §9 / Phase 3）：waiter registry + wake 桥（收益
   主体，最早在 test CVM 并行实例验证）。
4. **全量路由重写**（→ §10-11 / Phase 4-5）：按 §6.2 风险分级，A→B→C→D
   逐包迁（19 个领域包 = 19 个可并行的工作单元），每包过 parity。DB async 化
   热点随包推进。
5. **realtime / 特殊 runtime 收尾**（→ §12 / Phase 6）：screen WS 归属、
   admin/diagnostics、genesis hooks。
6. **test CVM 全量切换 + soak**（→ §13/§15）：test 环境启动命令切纯 FastAPI，
   跑足观察期。
7. **prod 一次切换 → 稳定后删 Flask**（→ §13 / Phase 7）。

阶段 2-5 期间，新 app 可在 test CVM 以**并行实例**（`:5005`，不接 ingress
流量，见 §8）随时起来打内网验证——这不是运行时兜底，只是开发期的对照环境。
test 的**正式切换**（阶段 6）等全部路由完成；prod 必须等 url_map 清单 100%
核销 + test soak 通过。

## 5. 目标架构

目标形态是**单一 FastAPI 部署单元**：一个镜像、一个容器、一个端口（:5001），
普通 API、长轮询、realtime 全部在同一进程组内由 event loop + bounded
threadpool 调度；poll 过载靠 waiter caps（`FEEDLING_POLLER_MAX_ACTIVE` 等）兜
故障域。后台重任务不进 API 进程，沿用现有 supervisor / genesis worker loop
（agent-runner 本就是独立容器），见 §5.7。**没有 WSGI 兜底层**——所有路由
都是 FastAPI 原生实现。

```text
                 ┌───────────────────────────────┐
                 │ dstack-ingress / routing       │   （不变）
                 └──────────────┬────────────────┘
                                │ :5001
                     ┌──────────▼──────────┐          ┌─────────────────┐
                     │ FastAPI backend      │          │ enclave_app      │
                     │ (gunicorn+Uvicorn    │──────────│ gunicorn gthread │
                     │  worker × N)         │          └─────────────────┘
                     │  全部路由原生 async  │
                     │  poll / whoami /     │
                     │  chat send / admin/… │
                     └──────────┬──────────┘
              ┌─────────────────┴──────────────────────────────┐
              │ shared core                                     │
              │ auth_core / db_async+db_sync / store / wake_bus │
              │ envelope / provider clients / observability     │
              └─────────────────────────────────────────────────┘
   进程内长驻：wake_bus LISTEN 线程、leader("ws") WS 线程、proactive/genesis
   后台线程 —— 全部由 lifespan / on_starting 显式管理
```

开发期仓库内双栈（Flask 作 parity oracle，只进测试不进镜像启动命令），
运行时单栈。

> **决策记录（2026-07-03）**：曾评估把 poll 拆为独立 `fastapi-poll` 进程组
> （容量/故障域隔离）。**已否**——目标是全量切换到单一部署单元；当前规模
> （~98 常驻 consumer，距 waiter cap 5000 两个数量级）拆分无实际收益。若未来
> 容量曲线恶化（consumer 数上一到两个数量级、poll 过载开始影响主 API SLO），
> 再重新评估。

## 5.0 阻塞链路审计清单

每迁一个模块前，先 grep 并分类阻塞依赖：

```text
requests
time.sleep
httpx.Client
psycopg sync calls / db.py
boto3
synchronous Redis / cache clients
sync provider SDK
subprocess.run / Popen.wait
large file read/write
Pillow/image processing
large JSON serialize/deserialize
compression
crypto loops / batch envelope work
```

迁移规则：

| 阻塞项 | 目标处理 |
|---|---|
| HTTP calls | process-lifetime `httpx.AsyncClient` |
| sleep | `asyncio.sleep` |
| Postgres | `psycopg_pool.AsyncConnectionPool` 或 bounded threadpool 过渡 |
| boto3/R2 | 先 threadpool；后续评估 aiobotocore/独立 worker |
| provider SDK 不支持 async | threadpool 或独立 worker |
| subprocess/CLI | 独立 worker / supervisor，不跑 API event loop |
| CPU-heavy JSON/image/crypto | process pool / worker / explicit bounded threadpool |
| long background workflow | worker，不用 FastAPI `BackgroundTasks` |

红线：`async def` route 里不得直接执行上表阻塞项；必须显式走 async client、
`run_in_threadpool`/bounded limiter、process pool 或 worker。

## 5.1 目标代码目录

建议新增目录，避免把 FastAPI 代码塞回 `app.py` 单体：

```text
backend/
  asgi_app.py                 # FastAPI/Starlette app assembly + lifespan
  asgi/
    __init__.py
    settings.py               # ASGI-specific env knobs
    middleware.py             # request id / access log / exception mapping
    responses.py              # JSON response helpers shared by routers
    threadpool.py             # bounded sync bridge
    lifespan.py               # startup/shutdown hooks
  accounts/
    auth_core.py              # framework-neutral auth
    routes_asgi.py            # ASGI whoami/bootstrap auth-facing routes
  chat/
    poll_core.py              # framework-neutral pending check
    routes_asgi.py            # ASGI chat poll / later chat send
  proactive/
    poll_core.py
    routes_asgi.py
  runtime/
    wake_listener_asgi.py     # ASGI poll waiters 的专用 LISTEN/NOTIFY
    waiters.py                # asyncio Future registry + limits
  db_async.py                 # later phase: psycopg async pool
```

开发期 `backend/app.py` 保留为 parity oracle 的唯一 assembly（parity 测试
import 它起对照实例）；**运行时任何代码路径都不 import 它**。ASGI app 不注册
Flask blueprints、不挂 WSGI adapter——每个领域包新增 `routes_asgi.py`
（或 router 模块），由 `asgi_app.py` 统一 include。

## 5.2 启动形态（唯一形态：纯 FastAPI）

切换后 backend 容器的启动命令：

```bash
gunicorn \
  --chdir backend \
  --config backend/gunicorn_conf.py \
  -k uvicorn_worker.UvicornWorker \
  --timeout 120 \
  -b 0.0.0.0:5001 \
  asgi_app:app
```

**回滚 = 切回旧镜像/旧命令**（同 schema，秒级）：

```bash
gunicorn --chdir backend --config backend/gunicorn_conf.py --threads 32 \
  --timeout 120 -b 0.0.0.0:5001 app:app
```

要点：

- **保留 `--config backend/gunicorn_conf.py`**：`on_starting` 是 gunicorn master
  钩子，与 worker class 无关——换 UvicornWorker 后它照样在 fork 前跑
  `assert_hosting_ready()` + sys.path 注入，`workers = _worker_count()` 的
  `FEEDLING_BACKEND_WORKERS` 语义也原样保留（所以命令里不写 `-w`）。
- **启动链是第一天的硬任务，不是收尾工作**：`asgi_app:app` 完全不 import
  `backend/app.py`，§3.3 的全部 import 副作用必须在骨架 PR 里就显式重建，
  逐条核销：
  1. **`db.init_schema()`（alembic upgrade head）→ `on_starting` master 单点**
     （顺带替代现在"每 worker 各跑一次"的幂等竞态）；漏掉 = 新镜像跳过
     migration 服务旧 schema，这是本计划最高危的单点。
  2. `core_wake_bus.start_listener()` → lifespan（缓存驱逐 handler + poll
     waiter 唤醒桥，见 §9.3——纯 FastAPI 下**一条 LISTEN 连接两用**，不再
     需要第二条）。
  3. `core_leader.run_singleton("ws", screen_ws.start)` → lifespan。
  4. admin/observability sampler、其余后台线程 → lifespan。
- **anyio 默认全局 threadpool 只有 40 tokens**：`run_db` / sync 兼容层必须在
  lifespan 显式设 limiter（§7.2），否则重路由并发被静默限在 40（< 现有 128）。
- **gunicorn `--timeout 120` 对 UvicornWorker 是心跳级**，语义要拆开记：
  worker 的 heartbeat 跑在 event loop 上，所以 **event loop 整体卡死会被
  --timeout 收割**（保护仍在）；但 **threadpool 里卡死的 sync 代码不会**
  （loop 活着、心跳照发），只会悄悄耗尽 limiter tokens。用 §5.9 的 access-log
  在途慢请求日志 + limiter 饱和度指标兜底，监控项见 §15.1。
- **`limit_concurrency` 的传法**：这是 uvicorn 的参数，gunicorn CLI 没有对应
  flag——需要自定义 worker 子类：
  `class Worker(UvicornWorker): CONFIG_KWARGS = {"limit_concurrency": 2048}`，
  `-k` 指向该子类。数值 ≥ waiter cap + 普通并发余量，只作最后护栏。
- 开发期在 test CVM 起并行实例内网验证时，用同命令换端口（`:5005`），不接
  ingress 流量。

## 5.3 ASGI app assembly 草图

目标明确是 FastAPI，无 WSGI 兜底：

```python
# backend/asgi_app.py
from fastapi import FastAPI

from asgi.lifespan import lifespan

app = FastAPI(lifespan=lifespan)

# 与 app.py 的 pkg.register(app) 对称：每个领域包暴露 register_asgi(app)
# 或 router，由这里统一装配。app.py 的 assembly-only 纪律原样继承。
import accounts, agent_pkg, push_pkg, proactive_pkg, identity_pkg, memory_pkg
# ... 19 个领域包
for pkg in ALL_PACKAGES:
    pkg.register_asgi(app)
```

装配纪律（继承 CONTRIBUTING 对 app.py 的要求）：

- `asgi_app.py` assembly-only，不写业务逻辑。
- **绝不 import `backend/app.py`**（它是 parity oracle，import 会触发 §3.3 的
  全部副作用，等于把旧启动链偷渡回来）。CI 加一条守卫测试：
  `import asgi_app` 后断言 `sys.modules` 里没有 `app`。
- 未迁完期间，缺失的路由就是 404——这是并行实例（`:5005`）内网验证时的
  预期行为，也是 url_map 清单必须 100% 核销才能切换的原因。

## 5.4 必须新增的 env knobs

| Env | 默认 | 说明 |
|---|---:|---|
| `FEEDLING_BACKEND_WORKERS` | `4` | ASGI gunicorn worker 数；复用现有变量（经 `gunicorn_conf.py` 生效） |
| `FEEDLING_ASGI_DB_THREADS` | `64` | sync DB bridge threadpool |
| `FEEDLING_ASYNC_DB_POOL_SIZE` | `16` | 每 ASGI worker 的 async Postgres pool size |
| `FEEDLING_ASYNC_DB_POOL_TIMEOUT_SEC` | `5` | DB pool acquire timeout |
| `FEEDLING_DB_STATEMENT_TIMEOUT_MS` | `10000` | session/local statement timeout |
| `FEEDLING_HTTP_MAX_CONNECTIONS` | `200` | process-lifetime AsyncClient max connections |
| `FEEDLING_HTTP_MAX_KEEPALIVE` | `50` | process-lifetime AsyncClient keepalive |
| `FEEDLING_POLLER_MAX_ACTIVE` | `5000` | ASGI poll waiters 全局上限 |
| `FEEDLING_POLLER_MAX_PER_USER_CHAT` | `2` | 单用户 chat poll 上限 |
| `FEEDLING_POLLER_MAX_PER_USER_PROACTIVE` | `2` | 单用户 proactive poll 上限 |
| `FEEDLING_ASGI_ACCESS_LOG` | `1` | ASGI 层结构化 access log |

连接预算（prod `max_connections=402`，07-02 现场占用 47——迁移全程以此为
硬顶）：

- ASGI worker 数仍按 `FEEDLING_BACKEND_WORKERS` 算。
- 每 worker 同时有：
  - sync `db.py` pool（max 16，随 async 化推进逐步下调）；
  - wake_bus listener 1（lifespan 起，缓存驱逐 + poll waiter 唤醒**一条两用**，
    §9.3）；
  - leader election ≤1；
  - async pool（`FEEDLING_ASYNC_DB_POOL_SIZE`，默认 16）。
- 即 4 worker ≈ 4×(16+16+1+1)=**136** 峰值——在 402 内，但加上 enclave /
  agent-runner / alembic / 运维连接后余量要核 `pg_stat_activity`。
- **不要同时大幅提高 ASGI workers + async pool max_size**；async 化推进时
  同步下调 sync pool，总量守恒。

## 5.5 DB session 规则

async 并发上来后，DB 连接池是第一风险。必须遵守：

1. **等待事件时不得持有 DB connection/session。**
2. **长轮询只在“查 pending”和“claim/update”时短暂拿连接。**
3. **每个 query 有 acquire timeout + statement timeout。**
4. **任何 transaction 必须尽量短，不能包住 provider/enclave/LLM 调用。**

错误示例：

```python
async with db_async.session() as session:
    pending = await load_pending(session)
    if not pending:
        await wait_for_event_30s()   # bad: 持有 DB 连接等 30s
```

正确示例：

```python
pending = await load_pending_shortly()
if not pending:
    await wait_for_event_30s()       # no DB connection held
pending = await load_pending_shortly()
```

DB pool 初始建议（单一 backend 进程组，对齐 §5.4 预算）：

```text
backend: workers=4, sync pool 16 + async pool 16（Phase 4 起）
         + LISTEN×2（混合期）+ leader ≤1  → 峰值 ~140 conns
```

按 prod `max_connections=402` 核算（另有 enclave / agent-runner / alembic /
运维连接），每阶段核 `pg_stat_activity` 后再调大。

## 5.6 全局 AsyncClient 规则

不得每个请求创建 `httpx.AsyncClient()`。在 FastAPI lifespan 中创建 process-lifetime
clients：

```python
@asynccontextmanager
async def lifespan(app):
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
        ),
    )
    yield
    await app.state.http.aclose()
```

至少分两类 client：

- `internal_http`：backend ↔ enclave / service internal，较短 connect timeout。
- `provider_http`：LLM/provider，较长 read timeout，单独连接上限，避免 provider 慢拖死内网调用。

## 5.7 Background task 规则

FastAPI `BackgroundTasks` 只允许轻量、短任务，例如 fire-and-forget 日志/指标。以下必须
拆到 worker：

- genesis/history import。
- 大批量 distill / reducer。
- batch rewrap / repair。
- 长时间 provider/LLM workflow。
- 大文件/R2 backfill。
- 多用户 proactive batch。

worker 可选实现：

- 近期：沿用现有 supervisor/genesis worker loop + DB claim (`FOR UPDATE SKIP LOCKED`)。
- 中期：抽 `backend/worker_app.py`，统一跑 scheduled/background jobs。
- 远期：如果 job 类型继续增长，再评估 Celery/RQ/Dramatiq/Arq/Temporal。

## 5.8 CPU-heavy 规则

不要在 event loop 做：

- image/Pillow。
- 大 JSON parse/dump。
- gzip/压缩。
- batch crypto/envelope。
- pandas/numpy。

处理策略：

- 小 CPU work：bounded threadpool。
- 大 CPU work：process pool 或 worker。
- 可拆服务的：单独 worker/service。

验收必须看 event loop lag，而不是只看 request status。

## 5.9 Flask → FastAPI 迁移映射

| Flask 概念 | FastAPI/ASGI 目标 |
|---|---|
| `before_request` | middleware / dependency |
| `after_request` | middleware wrapping `call_next` |
| `teardown_request` | dependency `yield` cleanup / middleware finally |
| `g` | `request.state` |
| `request` context | explicit `Request` 参数 / dependency |
| `blueprint` | `APIRouter` |
| `errorhandler` | `exception_handler` |
| `jsonify` | `JSONResponse` / return dict |
| app import side effects | lifespan startup |
| Flask test client | `httpx.AsyncClient` / `TestClient` |

必须逐项迁：

- auth / permission。
- request id / trace id。
- debug_trace context。
- access log —— 三条硬要求：
  1. **`?key=` REDACT**（大小写不敏感）原样继承，legacy auth 的 URL API key
     绝不能落日志（§3.1）；
  2. **取消/超时的请求也要记一行**（`status=cancelled dur_ms=...`）——Flask
     `after_request` 只记录成功返回的请求，07-02 排查就是被这个幸存者偏差
     误导的，ASGI middleware 是修它的机会；
  3. 超阈值（如 10s）的在途请求周期性 dump，弥补 UvicornWorker 下没有
     per-request timeout 收割（§5.2）。
- error mapping（对齐 §3.1 errorhandler 的固定 JSON body）。
- CORS（如有）。
- compression（flask-compress 随 Flask 一起退役：`GZipMiddleware` 或下放
  ingress，二选一并测大 response 的 Content-Encoding，§3.1）。
- rate limit / capacity guard。

不允许“顺手在 route 里临时取全局状态”绕过 dependency/middleware。
`g.user_id` 的 ASGI 等价建议用 `contextvars.ContextVar`（auth 成功后设置、
middleware 读取），Flask wrapper 同步写 `g`，两栈单源。

## 5.10 Pydantic v2 注意

如果为 FastAPI route 引入 request/response models，默认按 Pydantic v2：

```text
dict()             → model_dump()
json()             → model_dump_json()
orm_mode           → from_attributes
validator          → field_validator
root_validator     → model_validator
```

迁移策略：

- 第一阶段不要给所有 legacy payload 强上 Pydantic model，避免 contract 细节被自动转换。
- 对外部 iOS/consumer 敏感 payload，先保持 dict response + parity tests。
- 新增 admin/internal API 可以用 Pydantic v2 models。

## 6. Phase 0 — 迁移前准备

目标：不改运行时行为，只建立迁移基线和可比较工具。

### 6.1 建路由清单

生成当前 Flask `url_map` 快照，作为迁移基线：

```bash
uv run python - <<'PY'
import sys
sys.path.insert(0, "backend")
import app
for r in sorted(app.app.url_map.iter_rules(), key=lambda x: str(x)):
    print(r, sorted(r.methods))
PY
```

输出保存到 `docs/generated/backend-url-map-YYYY-MM-DD.txt` 或测试 fixture。

同时生成一份“路由所有者表”：

```text
path, methods, blueprint/module, auth mode, reads DB, writes DB, calls enclave,
calls provider, has side effect, migration risk
```

这张表决定迁移顺序。没有 owner/risk 的路由不得核销。

**顺带做死路由清理**：全量重写是收缩迁移面的唯一机会——url_map 里确认已无
调用方的 legacy/debug 路由（对照 30min 端点流量统计 + git 历史），在 Flask 侧
先删掉再拍快照。prod 用户规模极小，砍错的代价远低于白迁一条路由的代价。

**快照要保鲜**：cutover 前必须用同命令重新生成一次并 diff——迁移窗口内 Flask
侧的任何路由增删（线上 bugfix 带出来的）都必须反映进清单，否则 100% 核销是
假的。

### 6.2 分类路由

按风险分层：

- **A 等级：低风险读路由**：healthz、bootstrap/status、whoami、config reads。
- **B 等级：普通写路由**：identity/actions、memory/actions、push tokens。
- **C 等级：高风险用户主路径**：model_api/chat/send、chat history、hosted turn。
- **D 等级：特殊 runtime**：screen WS、poll、admin observability、genesis worker hooks。

### 6.3 抽 shared response/error helpers

先把“业务结果 -> HTTP response”的规则从 Flask response 里抽出来：

- auth error。
- validation error。
- provider error。
- hosted runtime unavailable。
- envelope decrypt unavailable。

目标是 FastAPI/Starlette 与 Flask 能返回同样 JSON。

### 6.4 依赖补齐

`backend/requirements.txt` 增加：

```text
fastapi>=0.115
uvicorn[standard]>=0.30
uvicorn-worker>=0.3     # gunicorn worker class，见下注
anyio>=4
```

**worker class 选型注**：uvicorn ≥0.30 起 `uvicorn.workers` 模块已标记废弃，
官方拆到独立 `uvicorn-worker` 包——启动命令用
`-k uvicorn_worker.UvicornWorker`（不是 `uvicorn.workers.UvicornWorker`），
避免锁死在废弃路径上。

无 WSGI adapter 依赖（不留运行时兜底）。顺手修正 requirements.txt 里过时的
"backend MUST stay single-worker" 注释（§3.3）。flask / flask-compress 在
prod 切换稳定、oracle 退役后删除（§13）。

### 6.5 迁移前验收

- 当前 Flask 路由清单已固化（死路由已清、快照已拍）。
- 当前 prod/test 启动命令、worker/thread/env 已记录。
- 选定 3 条 low-risk route 做第一批 ASGI parity。
- CI 可跑新增 parity test harness。

回滚：无运行时改动。

### 6.6 迁移窗口治理（双写规则）

迁移期间 prod 跑的仍是 Flask，线上 bugfix 不会停——不定规则的话，Flask 侧
持续演化会让已完成的 native 路由悄悄失去 parity。规则：

1. **业务逻辑修复优先落 shared core / service 层**（framework-neutral 的部分
   两栈自动共享）——这也是 Phase 1 先抽 core 的第二个理由：迁移窗口越长，
   core 化的修复越多，双写面越小。
2. **路由层/契约变更 = 双写**：动了 Flask 路由的请求/响应形状，必须同 PR 更新
   对应 native 路由 + parity test；native 未实现的路由只改 Flask，并在
   url_map 清单标记"已变更，迁移时以新行为为准"。
3. **新增路由默认只写 native**（FastAPI），Flask 不再添新路由——除非该功能
   必须在 cutover 前上 prod，此时双写。
4. cutover 前重拍 url_map 快照做 diff（§6.1），双写遗漏在这里兜底。
5. 迁移窗口有明确的时间盒预期：窗口每拖一周，双写税加一周——这是压缩
   Phase 7..N（按包重写）周期、多人/多 agent 并行的动机。

## 7. Phase 1 — Shared Core 抽离

目标：拆出 FastAPI 和 Flask 都能调用的业务核心，不引入 ASGI 服务。

### 7.1 Auth core

新增 `accounts/auth_core.py`：

```python
class AuthError(Exception):
    status_code: int
    code: str

def resolve_user(headers: Mapping[str, str]) -> dict:
    ...
```

Flask `require_user()` 变成 wrapper；ASGI 直接调用 core。

细节要求：

- 输入只接受 plain headers mapping，不接受 Flask/FastAPI request object。
- 输出 user row 至少包含现有 `g.user_id` 所需字段。
- 错误用 `AuthError(code, status_code, detail)`，Flask/FastAPI 分别映射 response。
- 支持 `X-API-Key`、`Authorization: Bearer`、`X-Feedling-Runtime-Token`。
- runtime token scope 与现有 `accounts.auth` 一致，不能因为 ASGI 迁移扩大权限。

测试：

- API key 成功 / 不存在 / reset 后失效。
- runtime token 成功 / 过期 / 签名错 / scope 不足。
- Flask `require_user()` wrapper 与旧行为一致。

### 7.2 DB boundary

短期保留 `db.py` sync API。新增统一 threadpool adapter：

```python
async def run_db(fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs), limiter=db_limiter)
```

同时开始为热点 DB 函数设计 async 版本：

- `get_user_by_api_key`
- `get_store_snapshot`
- `append_chat`
- `claim chat reply`
- `list_agent_runtime_enabled_users`
- `list_supervisor_instance_heartbeats`

禁止规则：

- ASGI route 内不得直接 import 并调用 sync `db.py`，必须走 `run_db()` 或 async DB。
- 例外必须在 code review 里说明“CPU-only / no blocking I/O”。

### 7.3 Store core

当前 `UserStore` 很多逻辑混合 DB/cache/in-process wake。迁移前先确认哪些函数需要：

- pure compute；
- DB transaction；
- process-local cache；
- wake notify。

目标不是马上重写，而是把 ASGI 路由调用点变少。

### 7.4 Poll core

抽出 framework-neutral 的 poll core：

- `chat/poll_core.py`
- `proactive/poll_core.py`

Flask route 先改成调用 poll core 后继续用原 `threading.Event().wait()`。这样第一阶段
就能确认抽 core 没改变 payload/claim 语义。

### 7.5 Phase 1 验收

- Flask 现有测试全绿。
- `tests/test_auth_core.py` 覆盖 headers auth。
- `tests/test_chat_poll_core.py` 覆盖 pending payload 与 claim。
- `tests/test_proactive_poll_core.py` 覆盖 job poll payload。

回滚：revert shared core wrapper，启动命令不变。

## 8. Phase 2 — 新 app 骨架与启动链

目标：`asgi_app.py` + lifespan 骨架落地，§3.3 的 import 副作用全部显式重建
（这是第一天的硬任务，见 §5.2），首批路由跑通全链路。

首批 native routes（验证链路，不求覆盖）：

- `GET /healthz`
- `GET /v1/users/whoami`（读 DB + auth，验证 auth core + run_db + contextvar）
- `GET /v1/bootstrap/status`（如果依赖少）

开发期验证形态：compose 可选并行服务（不接 ingress 流量，纯内网对照）：

```yaml
backend-asgi:
  image: ghcr.io/teleport-computer/feedling:<same-sha>
  command: ["gunicorn", "--chdir", "backend", "--config", "backend/gunicorn_conf.py",
            "-k", "uvicorn_worker.UvicornWorker", "-b", "0.0.0.0:5005", "asgi_app:app"]
```

正式切换时删除该服务，主 backend command 直接换成 §5.2 的唯一形态。

### 8.1 Lifespan / on_starting 必做（对照 §3.3 逐条核销）

- `db.init_schema()` → `on_starting` master 单点（最高危，漏 = 跳过 migration）。
- `assert_hosting_ready()` —— 已由保留的 `gunicorn_conf.py:on_starting` 覆盖；
  缺 `FEEDLING_RUNTIME_TOKEN_SECRET` / `FEEDLING_HOST_ALL` /
  `FEEDLING_LITELLM_ENABLE` 必须仍 fail-fast。
- `core_wake_bus.start_listener()`（缓存驱逐 + poll waiter 唤醒桥，§9.3）。
- `core_leader.run_singleton("ws", screen_ws.start)`。
- threadpool limiter 显式设置（§5.2 的 40-token 陷阱）。
- process-lifetime `httpx.AsyncClient`（§5.6）。
- shutdown：唤醒全部 poll waiters → 停后台线程 → 关 async clients / DB pools。

### 8.2 Phase 2 验收

- ASGI whoami 与 Flask oracle response 语义等价（parity fixture，§14.1）。
- runtime token / api key auth 一致。
- DB pool/threadpool 不阻塞 event loop（healthz p99 canary）。
- 启动链核销表：§3.3 每一项都能在 lifespan/on_starting 代码里指到对应行。
- 并行实例（:5005）在 test CVM 起得来、`/healthz` 绿。

## 9. Phase 3 — 迁移等待型/高 I/O 路由

目标：先把真正需要 async 的等待型路由迁到 ASGI——这是全迁移的收益兑现点。
形态已定（§5）：**同一 FastAPI app 的 native router 承接 poll**，无独立 poller
容器；waiter/wake 设计见本节（§9.1-9.3 即完整设计，原独立文档已删除）。

### 9.1 Chat poll native route

实现：

- `GET /v1/chat/poll` FastAPI route。
- auth core。
- `chat_poll_payload_once()` 先查 pending。
- 无 pending 注册 asyncio waiter。
- Postgres notify wake 后再查 pending。
- per-user/global cap。
- request cancellation 时 `finally unregister`。

保持 contract：

- `messages`
- `runtime_v2`
- `client_release`
- `timed_out`
- `consumer_id`
- `claimed`

### 9.2 Proactive poll native route

同 chat poll，但注意：

- 每次 poll 前仍要 `_reclaim_stale_resident_jobs(store)`。
- `limit` clamp 语义保持。
- timeout 默认短，适合作第一条 prod native poll route。

### 9.3 Wake listener（一条 LISTEN 两用）

纯 FastAPI 下不需要第二条 LISTEN 连接：`core_wake_bus.start_listener()` 的
缓存驱逐 dispatch（`UserStore` cache 与框架无关，纯 FastAPI 下仍然需要）
**加一个 event-loop 桥**即可——listener 线程收到 `chat/proactive` notify 时，
除现有 handler 外再 `loop.call_soon_threadsafe(registry.wake, channel, user_id)`。

- 监听同一个 `feedling_wake` channel，每 worker 一条（lifespan 起）。
- 不过滤 origin worker（waiter 醒后总是重查 pending，虚假 wake 无害）。
- 断线重连沿用 wake_bus 现有 5s 策略；重连后 waiters 靠 timeout 自然兜底。

### 9.4 其他高 I/O 读路由

优先迁移：

- `/v1/users/whoami`
- `/v1/bootstrap/status`
- `/v1/identity/get` backend ciphertext read path
- `/v1/model_api/key_envelope`
- hosted supervisor live check read path

这些是高频、逻辑相对薄、对降低 backend sync pressure 有帮助的路由。

### 9.5 Phase 3 验收

- 1000 idle `/v1/chat/poll` 只占 asyncio futures，threadpool active ≈ 0。
- wake latency p95 在目标内（先定 500ms）。
- consumer 无 duplicate claim / duplicate reply。
- `/v1/users/whoami` latency 不随 idle poll 数线性增长。
- 整镜像回滚到旧 Flask 命令后，consumer 无感恢复（payload 兼容 §6 已保证）。

## 10. Phase 4 — DB async 化热点路径

当 ASGI shell 稳定后，逐步把热点 DB 函数改为 async。

### 10.1 新建 `db_async.py`

```python
from psycopg_pool import AsyncConnectionPool

async def init_pool():
    ...

async def get_user_by_api_key(...):
    async with pool.connection() as conn:
        ...
```

注意：

- sync `db.py` 与 async `db_async.py` 在迁移期共存。
- migration 仍由 sync Alembic path 处理，不必 async。
- 每个 ASGI worker 一个 async pool，连接预算要重新算。

### 10.2 迁移顺序

1. auth lookup。
2. whoami。
3. supervisor heartbeat read。
4. chat append / claim。
5. UserStore snapshot reads。

每迁一个函数，都要有 sync/async parity test。

### 10.3 Async DB 连接池生命周期

ASGI startup：

```python
await db_async.init_pool()
```

ASGI shutdown：

```python
await db_async.close_pool()
```

禁止模块 import 时创建 async pool；uvicorn/gunicorn worker fork 时会出问题。

### 10.4 事务边界

现有 `db.py` 很多函数内部自己开 connection/transaction。async 版本不要把 transaction
拆散到 route 层，除非明确需要跨函数原子性。规则：

- 单 SQL/单业务操作：函数内部自管 transaction。
- 多步骤写入：新增明确的 service function 管 transaction。
- wake notify 必须在 commit 后发，避免 poller 醒来读不到数据。

### 10.5 Phase 4 验收

- async auth lookup 与 sync lookup parity。
- async heartbeat read 与 sync read parity。
- async DB pool 连接数符合预算。
- 故意打慢查询不会卡 event loop；其他 async route 仍响应。

## 11. Phase 5 — 高风险业务路由迁移

### 11.1 `/v1/model_api/chat/send`

这是主路径，迁移前置条件：

- auth core 已稳定。
- envelope build helpers 可在线程池或 async 安全运行。
- append_chat / hosted guard / wait_for_reply 语义有 async 等价。
- debug trace 写入兼容。

改造方式：

- provider inline path 如果仍有 sync HTTP/SDK，先放 threadpool 或改 async httpx。
- hosted agent path 本身是 DB append + wait reply，可直接 async wait。
- 保持 202 contract 不变。

细化步骤：

1. 先迁 hosted path，不迁 legacy inline LLM path。
2. append user message 后，使用 async wait for reply（不占 thread）。
3. provider gateway/native 判定仍复用 `hosted.agent_runtime_cutover` core。
4. `check_supervisor_live` 先走 async DB 或 threadpool，保持 fail-open/fail-closed 语义。
5. reply ready/processing response 与现有 202 body parity。
6. legacy inline path（sync provider HTTP/SDK）整体放 threadpool 跑，行为不变，
   避免同一 PR 里连 provider client 一起 async 化。

### 11.2 memory / identity actions

这些写路径涉及 envelope build、store save、wake notify。

迁移原则：

- transaction boundary 先不变。
- 加 route-level parity tests。
- 任何 wake_bus notify 必须在 commit 后发，保持现有语义。

迁移顺序建议：

1. identity read routes。
2. identity actions。
3. memory read metadata routes。
4. memory actions。
5. worldbook routes。

这些路由涉及 envelope build/enclave pubkey fetch，先允许 threadpool；后续再改 async httpx。

### 11.3 admin / diagnostics

低风险但涉及大查询和文件/R2 I/O，可晚迁。

### 11.4 External HTTP async 化

逐步替换：

- `httpx.Client` → process-lifetime `httpx.AsyncClient`。
- provider calls 按 provider family 分批。
- enclave calls 可先保留 threadpool，等 chat/send 稳定后再 async。

不要在同一 PR 同时迁 route + provider client + DB async。每次只动一个轴。

### 11.5 Phase 5 验收

- 真机 chat send smoke。
- hosted codex/claude/openrouter/gemini matrix。
- provider 402 cooldown 仍生效。
- debug_trace 字段不丢。
- push/live activity 不回归。

## 12. Phase 6 — Realtime 收敛（screen WS 归属）

Phase 3 落地后，realtime 三块的状态：

- poll —— 已是同进程 native async 路由（§5 决策：不拆独立服务）。
- wake_bus listener —— 每 worker 一条 LISTEN 线程 + event-loop 桥，保持。
- `:9998` screen WS ingest —— 仍是 daemon 线程里独立 asyncio loop + leader
  election，**这是本 phase 唯一待决的问题**。

两个选项：

- **默认：保持现状线程**。它工作正常、与 uvicorn 无冲突、没有归并的紧迫理由。
- **可选：并入主 event loop**（lifespan task + leader election 保留），少一条
  线程和一个独立 loop，生命周期统一；代价是 WS 与 API 同 loop 的故障耦合。

如果选择并入，screen WS 迁移步骤：

1. 新增 FastAPI WebSocket route `/screen/ingest` 或保持 `:9998` 独立 port。
2. 复用 `screen/ws.py` 的 auth parser，但改为 ASGI WebSocket headers/path。
3. frame save 仍丢 threadpool，避免 event loop 做 image/R2/DB。
4. 保留 `core_leader.run_singleton` 或改为单独 service；多 ASGI worker 不能全部 bind
   `:9998`。
5. test 真机 broadcast extension。

## 13. Phase 7 — 切换与 Flask 删除

**切换条件（prod cutover 前置，一项不满足不切）**：

- url_map 清单（§6.1）**100% 核销**：每条路由都有 native 实现 + parity 通过。
- test CVM 全量切换 soak ≥ 1 周：iOS 真机 + consumer + enclave 回调全链路无回归。
- §8.1 启动链核销表复查（尤其 alembic on_starting）。
- 回滚演练做过一次：test 环境切回旧镜像再切回来，确认双向都是秒级无感。

**切换步骤**：

1. test CVM cutover → soak（阶段 6）。
2. prod cutover：镜像 + command 一次切换（上链流程照 DEPLOYMENTS）。
3. 旧镜像 tag 记录在 DEPLOYMENTS 作为回滚坐标，保留至少一个 release 周期。

**Flask 删除（prod 稳定 1-2 周后，独立 PR）**：

1. 删除 Flask 路由与 `threading.Event().wait` waiters。
2. **清理 app.py COMPAT 符号回灌**（§3.1）：依赖它的老测试/工具改为直接
   import 领域包，独立完成并跑全量测试后再删 assembly。
3. 删除 `backend/app.py` assembly + parity oracle fixtures。
4. 删 flask / flask-compress 依赖。
5. 更新 CONTRIBUTING 中 backend 入口不变量（app.py → asgi_app.py 的
   assembly-only 纪律）+ DEPLOYMENTS 启动命令。

删除后回滚只能回到"删除前的最后一个 FastAPI 镜像"，不再能回到 Flask——所以
删除必须等 prod 稳定期过完。

## 14. 测试策略

### 14.1 Parity tests

为每个迁移 route 建 parity test：

- 同输入 headers/body/query。
- Flask response 与 ASGI response status/body 等价。
- 关键副作用一致：DB row、wake notify、debug trace。

**框架级差异清单（不显式覆盖必翻车）**：

- JSON 序列化：Flask `jsonify` 默认 `sort_keys=True`，FastAPI `JSONResponse`
  不排序——按语义等价断言，不做字节比较；核 Content-Type charset。
- trailing slash：Flask `strict_slashes` vs Starlette `redirect_slashes`
  （307 重定向对非浏览器 consumer 是行为变化）。
- 405 vs 404 语义、HEAD 自动处理、未知 query 参数容忍度。
- 压缩：flask-compress vs GZipMiddleware 的 Content-Encoding / Vary 差异。
- 未捕获异常的 500 body 形状。

### 14.2 Concurrency tests

- 1000 concurrent whoami/read requests，不阻塞 event loop。
- chat send 并发 append/claim 无重复。
- wake notify 与 async waiters 不丢 wake。

### 14.3 Failure tests

- DB down。
- enclave timeout。
- provider timeout。
- runtime token expired。
- request cancelled midway。

ASGI 需要特别测试 cancellation：客户端断开时，不能留下 DB transaction / waiter / lock。

### 14.4 Route migration matrix

每迁一个 route，都在矩阵里补一行：

| Route | Auth | DB | Side effects | Flask test | ASGI parity | Load/cancel | Owner |
|---|---|---|---|---|---|---|---|
| `/healthz` | none | no | no | existing | required | basic | backend |
| `/v1/users/whoami` | api/runtime | read | no | existing | required | 1k concurrent | accounts |
| `/v1/chat/poll` | api/runtime | read/CAS claim | waiters | existing | required | 1k idle + wake | chat |
| `/v1/proactive/jobs/poll` | api/runtime | read/update stale | waiters | existing | required | 1k idle + wake | proactive |
| `/v1/model_api/chat/send` | api/runtime | write | chat append/wake | existing | required | concurrent send/cancel | hosted |

迁移 PR 没补矩阵视为未完成。

### 14.5 Event loop blocking guard

新增测试/工具：

```python
async def assert_loop_not_blocked(duration=1.0, max_lag_ms=50):
    ...
```

在 ASGI load tests 中并发跑 heartbeat coroutine，捕获 sync DB / boto3 / Pillow 误入 event
loop 的问题。

### 14.6 Cancellation tests

必须覆盖：

- poll request 断开：waiter unregister。
- chat send 断开：已经 append 的 user message 不回滚；后续 agent reply 仍可投递。
- DB transaction 中断：连接回 pool 前没有 open transaction。
- external provider call 取消：client close / timeout 正确释放。

语义提醒：经 `to_thread.run_sync` 的 sync 函数**不会被取消杀死**——线程跑完、
只有 await 点抛 CancelledError。数据不会写一半，但"副作用已发生、响应没送达"
成为常态，所以每条迁移的写路由都要确认 consumer/iOS 重试是幂等的（现有
claim CAS / append 去重已满足，逐路由核对）。原生 async 写事务用
`CancelScope(shield=True)` 包住 commit + commit 后的 wake notify。

### 14.7 测试基建迁移（对照 §3.6）

- 35 个 `test_client` 文件**跟路由走**：某路由的 native 实现落地后，其测试改打
  ASGI app（`httpx.ASGITransport` / `TestClient`）；Flask oracle 侧由 parity
  fixture 覆盖。开发期双栈 conftest fixture（`flask_client` / `asgi_client`）
  并存，Flask 删除时 `flask_client` 一起退役。
- 5 个 subprocess 测试的启动命令参数化（旧 gunicorn sync ↔ 新 `asgi_app` 命令），
  CI 两种都跑直到 prod 切换完成，之后只跑新命令。
- conftest 的一次性 PG provisioning 机制不变。
- 老测试对 app.py COMPAT 符号回灌的依赖（§3.1）在 Flask 删除前不清理。

## 15. 部署策略

**开发期不动 prod；prod 只有一次切换。** 节奏：

1. 开发期：test CVM 起并行实例（`:5005`，不接流量）随时内网对照。
2. 全部路由完成 + parity 全绿 → **test CVM 正式 cutover** → soak ≥ 1 周
   （iOS 真机 + consumer + enclave 回调全链路）。
3. test 回滚演练一次（切旧镜像再切回，双向验证）。
4. **prod cutover**：镜像 + command 一次切换。
5. prod 稳定 1-2 周 → 删 Flask（§13）。

运行时开关只保留容量类，不做路由级开关：

- `FEEDLING_ASGI_DB_THREADS` / threadpool limiter。
- `FEEDLING_ASYNC_DB_POOL_SIZE`。
- `FEEDLING_POLLER_MAX_ACTIVE` + per-user caps。

回滚 = **整镜像/命令回滚**（同 schema，秒级，无 DB rollback）。没有路由级
回滚——这是不留兜底的代价，靠 parity 100% 核销 + test soak 前置吸收。

### 15.1 Cutover 观察项

切换后（test 与 prod 各自）至少盯：

- 5xx rate、404 rate（**404 尤其重要**：漏迁路由在无兜底下表现为 404，
  cutover 后 24h 内任何非预期 404 都当 P1 查）。
- ASGI event loop lag。
- threadpool active/pending（`run_db` limiter 饱和度）。
- DB connections（对照 §5.4 预算）。
- poll active waiters；agent-runner `poll error: timed out`（**收益验证**：
  应从 ~80-110/min 掉到接近零）。
- enclave `backend_error: timed out`（下游恢复验证）。
- async DB pool active/waiting、HTTP client pool acquired/waiting。
- access log 的 cancelled / 慢请求行（§5.9）。

### 15.2 Rollback playbook

无路由级回滚，只有两级：

1. **整体回滚**：backend command/镜像切回旧 gunicorn Flask（同 schema，秒级）。
   触发条件预先写死：chat send 主路径 5xx > 阈值、poll 投递中断 > 5min、
   任何数据完整性疑点——不现场争论要不要回滚。
2. schema/DB 问题：本计划原则上不引入 schema 变更；若某阶段引入，必须带独立
   migration rollback plan，且该变更不得与 cutover 同一个 release。

回滚后修复 → 重新走 test soak → 再切。不做"回滚后紧急修一把直接再上"。

### 15.3 压测矩阵

test cutover 前与 prod cutover 前各跑一遍：

| 场景 | 指标 |
|---|---|
| 普通 API 100 rps | p95/p99、5xx、event loop lag |
| 400 idle chat polls | active waiters、API p95 是否受影响 |
| 1000 idle chat polls | poll memory、wake p95、DB conns |
| poll + whoami 并发 | whoami p95 不随 poll 线性增长 |
| poll + chat send | duplicate claim/reply = 0 |
| provider slow | provider_http pool 不拖 internal_http |
| DB slow query | pool waiting 可见，event loop 不被阻塞 |
| request cancel storm | waiter leak = 0，DB tx leak = 0 |
| worker long job | API event loop lag 不受影响 |

## 16. 风险表

| 风险 | 说明 | 缓解 |
|---|---|---|
| event loop 被 sync DB 阻塞 | async route 里直接调 sync DB 会卡全进程 | 强制 threadpool adapter；lint/review 禁直接调用 |
| request cancellation 破坏状态 | async client 断开会取消 task | DB write 用 shield/transaction boundary；waiter finally unregister |
| auth 行为分叉 | Flask/ASGI 两套 request context | shared auth core |
| wake_bus 语义改变 | cache invalidation / waiters 依赖进程内状态 | 保留 wake_bus listener，只加 event-loop 桥不改 dispatch（§9.3 一条两用） |
| DB 连接暴涨 | ASGI workers + sync pools + async pools 共存 | 连接预算表（§5.4）；cutover 前压测 |
| route contract 细微变化 | iOS/consumer 对字段敏感 | parity tests + real device smoke |
| **漏迁路由 = 404** | 无兜底下，url_map 漏一条就是用户可见故障 | §6.1 清单 100% 核销为切换硬条件；cutover 后 404 监控当 P1（§15.1） |
| **收益延后到切换日** | 无灰度，poll async 收益不能提前上线，期间 prod 继续承受长轮询压力 | 方案 F（runner 外迁 + workers 扩容）先行顶住；控制开发周期 |
| migration 时间长 | 迁移窗口内 Flask 侧持续演化，双写税随窗口线性增长 | §6.6 双写规则 + 按包并行压缩窗口 + cutover 前重拍 url_map diff |
| gunicorn_conf hook 丢失 | 托管前置不 fail-fast | 保留 `--config gunicorn_conf.py`（§5.2）+ 启动链核销表（§8.1） |
| 运行时误 import app.py | 旧启动链副作用被偷渡回来（双 listener/双 WS 竞争） | CI 守卫测试：`import asgi_app` 后断言 `app` 不在 `sys.modules`（§5.3） |
| async DB 与 sync DB 双池连接过多 | RDS max_connections 被吃满 | 每阶段连接预算 + max_size 限制 |
| long polling 与普通 API 同进程 | poll 过载拖主 API | waiter caps（global/per-user）+ 429 backoff + `--limit-concurrency` 护栏；容量曲线恶化时再重评进程组拆分（§5 决策记录） |
| 每请求创建 AsyncClient | 连接暴涨、TLS/FD 浪费 | lifespan 全局 client |
| 长任务放 FastAPI BackgroundTasks | API worker 被 job 拖慢 | 禁用于长任务；沿用现有 supervisor/genesis loop（§5.7） |
| DB session 包住 wait/provider 调用 | async 并发下耗尽 DB 连接 | session 规则 + tests |
| CPU-heavy 进 event loop | 单 worker 卡死 | event loop lag guard + process/worker |
| **漏掉 alembic** | asgi_app 不 import app.py，`db.init_schema()` 随之消失，新镜像服务旧 schema | 骨架 PR 第一天就把 init_schema 落 `on_starting` master 单点（§5.2/§8.1）；启动链核销表为切换硬条件 |
| anyio 默认 threadpool 仅 40 | `run_db`/sync 兼容层静默限流在 40 并发（< 现有 128） | lifespan 显式设 limiter（§5.2/§7.2） |
| 卡死请求无人收割 | UvicornWorker 下 gunicorn --timeout 不按请求生效 | 在途慢请求日志（§5.9）+ limit-concurrency 护栏 |
| 老测试/工具 import 断裂 | app.py COMPAT 符号回灌被过早删除 | app.py 保持可 import 到 Flask 删除；删除前专项清理（§13） |
| pydantic 校验漂移 | FastAPI model 拒掉现在容忍的宽松 payload（旧 iOS/consumer） | §5.10：legacy payload 不上 model，`request.json()` 裸解析 + parity |

## 17. 和 Async Poll Service 的关系（历史记录）

**独立部署的 async poll service 已否**（用户决策：不单独起 poll server，且
全量 FastAPI 已拍板），**其设计文档已从仓库删除**。它的 auth core / poll
core / waiter registry / wake listener / caps / 观测字段 / 测试要点已并入
本计划 §7.1、§7.4、§9——§9 即现行完整设计，不存在另一份要交叉参照的文档。

## 18. 决策记录与执行红线

**2026-07-03 决策（同日三次演进，以此为准）**：

1. 全量 FastAPI 化执行（取代初稿"近期不做"的结论）。
2. 不拆独立 poll service；`fastapi-poll` 进程组拆分已否——单一部署单元。
3. **不保留运行时 Flask 兜底**：不做混合壳、不做逐路由灰度；Flask 仅作开发期
   parity oracle，prod 切换稳定后删除。回滚粒度 = 整镜像。

相关既定事实：

- 收益主要来源明确：07-02 排查证实 ~98 常驻长轮询 ≈ 98 个占死的 gthread
  线程；其余路由不是瓶颈（CPU ~20%、DB 空闲）。无灰度路线下，**全部收益在
  prod cutover 日一次兑现**（poll 收益不可提前单独上线——已明示并接受）。
- 后台重任务不进 API 进程，沿用现有 supervisor/genesis loop（§5.7）。
- runner 外迁 + workers 扩容（方案 F）仍是独立运维动作，且在切换日之前是
  prod 长轮询压力的唯一缓解，应先行；内存墙不由本计划解决。

执行坚持三条红线：

1. **url_map 100% 核销才切 prod**：无兜底下漏一条路由就是用户可见 404；
   test CVM 全量 soak + 回滚演练是 prod cutover 硬前置（§13/§15）。
2. **先 poll/whoami，后 chat send**：开发与 test 验证顺序仍按风险分级，
   不得一上来写最高风险路径。
3. **每个 route 有 parity**：没有 parity test 的 route 不算完成，不计入
   url_map 核销。

## 19. 二次审阅补强项（2026-07-03）

以下补强不改变“全量 FastAPI、无 Flask 运行时兜底”的目标；它们是该目标下
必须显式落进执行计划的上线闸门。

### 19.1 schema rollback 契约

§4/§18 里把回滚粒度定为“整镜像回滚到旧 Flask image，使用同一套 schema”。
这个策略成立的前提是：cutover 窗口内所有 schema 变化都必须向后兼容旧 Flask。

执行规则：

- prod cutover 前只允许 expand-only migration：新增表、新增 nullable column、新增
  index、新增兼容字段。
- 禁止在回滚窗口内做 drop/rename、字段语义改变、非空约束收紧、枚举收窄、默认值
  语义改变。
- `db.init_schema()` 迁到 gunicorn `on_starting` 后，schema upgrade 会先于 worker
  serving 发生；因此每个 migration PR 必须标注“旧 Flask image 是否仍可启动和服务”。
- destructive migration 只能在 FastAPI prod 稳定 1-2 周、确认不再需要 Flask image
  回滚后单独执行。

硬门禁：

- cutover 前跑一次“新 schema + 旧 Flask image”启动 smoke；失败则不能使用整镜像回滚
  作为兜底。

### 19.2 wake_bus self-origin 唤醒缺口

当前 `core/wake_bus.py` 明确跳过 self-origin notify：

```python
if data.get("o") == WORKER_ID:
    return  # our own write — the local fast path already handled it
```

旧 Flask 语义依赖本地 fast path：写路径调用 `store.notify_chat_waiters()` /
`store.notify_proactive_job_waiters()` 唤醒同 worker 的 `threading.Event`，再用
`wake_bus.notify()` 唤醒其他 worker。

FastAPI async waiter registry 不能只接 `wake_bus` LISTEN 桥，否则会漏掉：

```text
同一个 worker 上有 async poll waiter
同一个 worker 上发生 chat/proactive 写入
notify 被 self-origin 过滤
waiter 只能等到 30s timeout
```

实现必须三选一，并用测试锁住：

- 写路径在持久化成功后直接调用 `async_waiters.wake(channel, user_id)`，再
  `wake_bus.notify()` 跨 worker。
- 或把 wake_bus 拆成“本地 async bridge 可接 self-origin、store cache evict 仍跳过
  self-origin”的双 dispatch。
- 或新建 poll 专用 LISTEN path，不复用 `_dispatch()` 的 self-origin 过滤。

硬门禁：

- 加集成测试：同 worker 内一个 task 发起 poll wait，另一个 task 写入 chat/proactive，
  poll 必须在 <500ms 返回，不能等 timeout。
- 加跨 worker/跨进程测试：worker A 写入，worker B 的 waiter 被 LISTEN 唤醒。

### 19.3 385 长轮询规模下的隔离阈值

§5 当前按“约 98 常驻 consumer、cap 5000”否掉 `fastapi-poll` 独立进程组。最新
口径是长轮询客户可能约 385 人，这仍在 ASGI idle socket 能承受的范围内，但已经
不适合只写“规模很小所以不拆”。

保留单进程组决策，但必须定义重新评估阈值：

- `active_poll_waiters > 1000` 持续 15 分钟；
- `event_loop_lag_p99 > 100ms` 且与 poll waiters 数量正相关；
- 普通 API p95/p99 在 poll 高峰显著恶化；
- poll 429/backoff 比例持续上升；
- 单 worker FD、内存、DB pool wait 任一接近告警阈值。

触发任一条件时，不回滚 FastAPI；优先把同一代码仓库按 command/env 拆成：

```text
fastapi-api   普通 HTTP API
fastapi-poll  /v1/chat/poll + /v1/proactive/jobs/poll
```

拆分前置条件也要现在就做：poll route 使用独立 limiter、独立 metrics label、
独立 access-log category，避免未来拆进程时再补观测。

### 19.4 no-fallback 下的 parity 覆盖口径

“url_map 100% 核销”需要明确不只是路径存在。每条 route 的 parity 至少覆盖：

- method/path/query/body parsing；
- status code 与错误 envelope；
- auth failure/permission failure；
- response headers、CORS、compression、cache-control；
- legacy client 宽松 payload 行为；
- cancellation/timeout 行为；
- side effect：DB 写入、wake notify、后台任务 enqueue、外部 provider 调用。

切换前要保留一份机器可读 route matrix：

```text
route, owner, risk, flask_test, asgi_test, parity_status, notes
```

没有 `asgi_test` 和 `parity_status=pass` 的 route，不得从 matrix 勾掉。

### 19.5 app.py import 断裂的工具清单

`asgi_app` 运行时不得 import `backend/app.py` 是正确红线，但仓库里仍有测试、
脚本、debug 工具可能 import `app` 或依赖 Flask test client。§13 删除 Flask 前，
需要单独核销：

- CI tests；
- `tools/*` 和 `scripts/*`；
- deploy health checks；
- 手工 debug 命令；
- README/CONTRIBUTING/DEPLOYMENTS 里的启动命令。

硬门禁：

- `rg "from app import|import app|test_client\\(" backend tests tools scripts` 清零，
  或每个命中都有迁移说明。

### 19.6 启动链实现校验

文档已要求把 `db.init_schema()` 放进 gunicorn master `on_starting`，但当前
`gunicorn_conf.py` 只负责 hosting ready / worker 数等逻辑。PR 4 必须同时交付：

- `on_starting` 里单点执行 `db.init_schema()`，失败即 master 启动失败；
- `on_starting` import path 在 `--chdir backend` 和本地测试两种路径下都可用；
- `UvicornWorker` 下 `on_starting` 仍被执行的测试或 smoke；
- 多 worker 启动时 migration 只执行一次，不在每个 worker lifespan 重复执行；
- startup 日志明确输出 hosting ready、schema init、wake listener、screen WS leader、
  threadpool limiter、AsyncClient 初始化结果。

## 20. PR 切片建议

### PR 1 — Dependencies + route inventory

- 加 FastAPI/uvicorn/anyio 依赖。
- 生成 URL map fixture。
- 加 route migration matrix 文档。
- 无运行时行为变化。

### PR 2 — auth_core + response helpers

- 新增 `accounts/auth_core.py`。
- Flask `require_user()` 改 wrapper。
- 单测 auth parity。

### PR 3 — poll_core extraction

- 新增 chat/proactive poll core。
- Flask route 复用 core，但仍 sync wait。
- 单测 payload/claim parity。

### PR 4 — asgi_app 骨架 + 启动链

- 新增 `asgi_app.py`、lifespan（§8.1 核销表全部落地：alembic on_starting、
  wake_bus、leader("ws")、threadpool limiter、AsyncClient）。
- native `/healthz`。
- access-log middleware + error mapping（§5.9）。
- CI 守卫：`import asgi_app` 不得引入 `app`。
- compose 并行实例（`:5005`）command，test 内网验证用。

### PR 5 — native whoami/bootstrap

- ASGI accounts routes。
- parity tests。
- load test whoami。

### PR 6 — native async poll

- waiters registry。
- wake_bus event-loop 桥（§9.3 一条 LISTEN 两用）。
- native chat/proactive poll。
- cancellation + load tests（1k idle poll 压在 `:5005` 并行实例上）。

### PR 7..N — 按包全量重写路由

按 §6.2 风险分级 A→B→C→D，一个领域包一个（组）PR，19 个包可多人/多 agent
并行：

- 每包：`routes_asgi.py` + parity tests + url_map 清单核销打钩。
- DB async 化热点随包推进（`db_async.py` 首次引入时独立 PR）。
- hosted chat send（C 级）压轴：native hosted path、provider inline path 先
  threadpool、real provider smoke matrix。
- 后台任务归属确认随 hosted 包 PR 一起：genesis/history import/batch jobs
  不落 API worker 的 loop/threadpool（§5.7）。

### PR N+1 — test CVM cutover

- test backend command 切 §5.2 唯一形态；删并行实例服务。
- 压测矩阵（§15.3）全跑；soak ≥ 1 周；回滚演练。

### PR N+2 — prod cutover

- prod 镜像 + command 一次切换（DEPLOYMENTS 记录回滚坐标）。
- §15.1 观察项盯 24h/1 周。

### PR N+3 — 删除 Flask

- §13 删除清单：路由/waiters → COMPAT 回灌清理 → app.py → flask 依赖 →
  CONTRIBUTING/DEPLOYMENTS 更新。
- prod 稳定 1-2 周后才合。
