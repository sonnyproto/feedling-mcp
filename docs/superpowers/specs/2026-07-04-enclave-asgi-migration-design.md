# Enclave 迁移 FastAPI + asyncio 设计（混合并发模型）

日期：2026-07-04
状态：已评审通过，待写实现计划
前置：主 backend 的 Flask→FastAPI/ASGI 迁移已合入 test 分支（PR #44）；
`backend/enclave_app.py` 是全仓最后一个 flask 使用方。

## 1. 目标与非目标

### 目标

1. `backend/enclave_app.py`（2333 行 Flask 单文件）迁移为
   `backend/enclave/` 模块化包 + FastAPI/asyncio。
2. 全仓删除 flask / flask-compress 依赖（enclave 是最后一个使用方），
   缩小被证明（attested）镜像的依赖面。
3. 解除并发硬上限（现为 2 进程 × 32 gthread 线程）：慢 I/O（backend
   回环、VLM caption 最长 45s）不再占用线程槽；CPU 密集的解密批处理
   不阻塞事件循环。

### 非目标

- 不改任何路由路径、响应形状、错误码语义 —— 对 iOS、agent、consumer
  完全透明。
- 不改 envelope 加密方案（box_seal HKDF+ChaCha、AEAD AAD 绑定）。
- 不改 compose 入口命令 `python -u backend/enclave_app.py`。
- 不做新旧 enclave A/B 并行跑（enclave 有 TLS 指纹钉扎进 REPORT_DATA，
  双实例意义不大；验证节奏见 §8）。
- 不在本次改 REPORT_DATA 布局、attestation 语义、密钥派生路径。

## 2. 现状要点（迁移必须保住的行为）

- 11 条路由：`/healthz`、`/attestation`、`/v1/envelope/decrypt`、
  `/v1/memory/index|fetch|list`、`/v1/worldbook/match`、
  `/v1/chat/history`、`/v1/identity/get`、
  `/v1/screen/frames/<id>/decrypt|caption|image`。
- 服务方式：内嵌 gunicorn（BaseApplication）gthread worker，
  `FEEDLING_ENCLAVE_WORKERS`（prod=2）× 32 线程；enclave 内 TLS 终结，
  bootstrap 派生的 ECDSA P-256 证书经 tmpfs PEM 交给 gunicorn，
  iOS 将 sha256(cert.DER) 与 REPORT_DATA 中指纹比对（钉扎）。
- whoami 解析：runtime-token 本地 HMAC 校验（免回环）→ 30s TTL 缓存
  + per-key singleflight → 兜底 backend `/v1/users/whoami` 回环。
  `/v1/envelope/decrypt` 例外：绝不走缓存，每次实时解析（安全语义）。
- flask 专属能力：flask-compress gzip（decrypt-with-image ~470KB JSON
  受益）；`send_file(conditional=True)` 给 `/image` 提供 HTTP
  Range/ETag —— dstack-gateway 每 TCP 连接 ~1Mbps 限速下并行分块拉图
  的关键能力。
- 错误码约定（客户端已依赖，逐字保留）：401 unauthorized / 缺凭证；
  502 backend_error / backend_unreachable / decrypt_failed（读侧帧路
  由）；503 not_ready / key_derivation_unavailable /
  screen_caption_unconfigured；403 decrypt_failed（仅
  /v1/envelope/decrypt）；400 bad frame id / moments must be a list 等；
  404 frame not found。错误体形如 `{"error": "..."}`。
- **错误字符串也要逐字保留，不只保留 status code**：历史上已有两套
  缺凭证/用户解析失败拼法，`/v1/envelope/decrypt` 返回
  `missing_api_key` / `cannot_resolve_user_id`，多数 decrypt-and-serve
  读路由返回 `missing api_key` / `cannot resolve user_id`。迁移时不要
  顺手统一命名，避免 consumer/iOS/脚本按字符串分支的路径漂移。

## 3. 模块划分

`enclave_app.py` 保留为薄入口（约 30 行）：`bootstrap()` → 材料化
TLS → 起服务器。compose 命令不变。实现全部搬进新包：

```
backend/enclave/
    __init__.py
    config.py         # 环境变量、RELEASE、APP_AUTH、端口/TLS 开关
    state.py          # _state 字典 + bootstrap()
    keys.py           # KMS/dev-seed 密钥派生、content_sk 进程级缓存
    attestation.py    # REPORT_DATA 构造、quote 获取、dev_attestation
    envelope.py       # DecryptFailure、box_seal_open_hkdf、
                      # decrypt_envelope、AAD —— 纯同步无 I/O
    auth.py           # api_key/runtime_token 提取、本地 HMAC 校验、
                      # async whoami 缓存 + singleflight
    backend_client.py # 进程级 httpx.AsyncClient 池、backend GET 封装
    readside.py       # memory readside 适配器、index/fetch builder（纯函数）
    visual.py         # 帧 plaintext 解析、raw image MIME 探测
    routes/
        __init__.py   # build_app()：FastAPI 组装 + GZipMiddleware
        health.py     # /healthz /attestation
        envelope.py   # /v1/envelope/decrypt
        memory.py     # /v1/memory/index /fetch /list
        worldbook.py  # /v1/worldbook/match
        chat.py       # /v1/chat/history
        identity.py   # /v1/identity/get
        frames.py     # /v1/screen/frames/*（decrypt/caption/image + Range）
    asgi_worker.py    # enclave 专用 UvicornWorker：并发上限/TLS 行为收口
    serving.py        # gunicorn 内嵌组装、TLS PEM 材料化、worker 配置
```

依赖方向：`routes → auth / backend_client / envelope / readside /
visual → keys / state / config`。加密核（envelope.py）与 readside
是纯函数层，不 import FastAPI、不 import httpx，可独立单测。

## 4. 并发模型（核心设计）

- **所有路由 `async def`**。enclave→backend 回环换进程级
  `httpx.AsyncClient`（连接池参数照抄现有 Limits：keepalive 20 /
  max 100 / expiry 90s / timeout 15s），在 app lifespan 中创建与关闭。
- **解密批处理下放线程池**：每个请求把整批 envelope 的解密循环打包
  为一个 `anyio.to_thread.run_sync` 调用（按请求一次下放，不按
  envelope 逐条下放，避免线程抖动）。libsodium（cffi）与
  cryptography 的 C 调用释放 GIL，线程池内真并行；事件循环全程不被
  CPU 工作阻塞 —— 重用户导入历史时 `/healthz` 与其他用户请求照常
  响应。
- **whoami 缓存 async 化**：TTL 30s 不变；singleflight 从
  `threading.Lock` 改为 per-key `asyncio.Future`（等待者 await 同一
  in-flight 结果，完成后按现有语义退休）。runtime-token 本地 HMAC
  校验是纯计算，留在事件循环内联执行。
  `/v1/envelope/decrypt` 保持"每次实时解析、绝不走缓存"。
- **认证上下文显式传参**：现在的 `_flask_get()` 依赖 Flask
  `request` proxy 在内部读取 `X-Feedling-Runtime-Token`，所以
  api-key 为空的 runtime-token 路径也能转发给 backend。迁移后不能把
  token 藏在 request 全局里；每条路由先解析出
  `AuthContext(api_key, runtime_token, forward_headers)`，后续
  whoami、chat/history、memory/list、identity、frame envelope 拉取都显式
  传 `forward_headers`。这是 runtime-token-only 调用不回退成
  unauthenticated backend 请求的硬约束。
- **content_sk**：bootstrap 时派生并缓存；防御性的运行时重派生路径
  （dstack socket 回环）包进 `to_thread` + async 锁，保持"单次派生"
  性质。
- **VLM caption**：`backend/provider_client.py` 新增 async 版
  `chat_completion`（共享 `httpx.AsyncClient`，有 app lifespan 关闭钩子）；
  现有同步版保留给其他调用方。45s 长等待只挂协程不占线程。不要在
  async route 里直接调用当前同步 `provider_client.chat_completion()`，也不
  要让同步版和异步版混用同一个 `httpx.Client` 实例。
- **caption 配置保持运行时读取**：`/v1/screen/frames/<id>/caption` 现在
  每次请求重新读取 `FEEDLING_SCREEN_VLM_API_KEY`，并且刻意不 fallback 到
  import-time `SCREEN_VLM_API_KEY` 常量；unset 必须 fail-closed 为
  `screen_caption_unconfigured`。迁移成 lifespan/client 池时，只能缓存
  HTTP client，不能把 key/model/base_url 固化到启动时，否则 secret
  rotation 与单测语义会变。
- `FEEDLING_ENCLAVE_WORKERS`（prod=2）语义保留：多进程继续提供跨核
  冗余与故障隔离。`FEEDLING_ENCLAVE_THREADS` 的含义变为解密线程池
  容量（anyio CapacityLimiter），默认值沿用 32，环境变量名不变以免
  动 compose。
- app lifespan 中必须把 anyio 默认 thread limiter 调到
  `FEEDLING_ENCLAVE_THREADS`。主 backend 的 `FEEDLING_ASGI_DB_THREADS`
  不能复用到 enclave；这里的 limiter 保护的是解密批处理/少量 dstack
  阻塞调用，不是数据库线程。

## 5. 服务器与 TLS（最高风险项）

- 与主 backend 一致：内嵌 gunicorn `BaseApplication` +
  enclave 专用 `uvicorn_worker.UvicornWorker` 子类。入口仍是
  `python -u backend/enclave_app.py`（compose_hash 故事不变，
  CONTRIBUTING §7）。
- TLS：`serving.py` 照搬现有逻辑 —— bootstrap 派生的 PEM 材料化到
  tmpfs（0600、atexit 清理、master-pid 守卫防 worker 回收误删），
  certfile/keyfile 交给 gunicorn → uvicorn。
- **出示的 leaf cert 与现在是同一份 PEM，sha256(DER) 不变**，钉扎
  理论上不破。但 uvicorn 构建 SSLContext 的方式与现有自定义
  `ssl_context` hook（裸 PROTOCOL_TLS_SERVER、TLS1.2+、无 ALPN）不
  同，协商细节可能有出入。硬验收项（见 §8 test CVM 阶段）：
  1. `openssl s_client` 取实际 served cert，sha256(DER) ==
     attestation 指纹；
  2. iOS 审计卡实测通过；
  3. 最低 TLS 版本 ≥1.2。
  若 uvicorn/gunicorn 默认 context 行为有出入：优先在 enclave 专用
  worker 层集中注入自定义 SSLContext（复用当前
  `_enclave_ssl_context` 的 TLS1.2+、无 ALPN 语义），不要把 TLS 行为散在
  路由或启动脚本里。

## 6. Flask 能力替代

| 现在 | 迁移后 |
|---|---|
| flask-compress gzip | Starlette `GZipMiddleware`，压缩阈值对齐现有 flask-compress 生效阈值（默认 500 字节） |
| `send_file(conditional=True)` 的 Range/ETag | `/image` 路由手写单区间 Range：`Accept-Ranges: bytes`、206 + `Content-Range`、非法区间回 416、`ETag` + `If-None-Match` → 304。约 40 行 + 专项测试 |
| `request.headers/args/get_json(silent=True)` | FastAPI `Request` 显式传参；JSON 解析失败按现状容忍为空 dict，不让框架回 422 |
| `jsonify(...), status` | `JSONResponse(content, status_code)`，错误体字段逐字保留 |
| Flask 自动 HEAD/OPTIONS | FastAPI 路由表显式验收 GET 路由的 HEAD/OPTIONS/405 行为；至少 `/healthz`、`/attestation`、`/image` 不应因为框架差异改变探活/调试工具表现 |

**422 陷阱（显式防）**：FastAPI 类型化参数校验失败默认回 422，而
现有客户端期望 400/401（frame_id 正则、`moments must be a list`、
since/limit 解析等）。所有输入校验保持手工解析 + 显式
`JSONResponse`，路由签名不使用会触发自动校验的类型化
Query/Path/Body 声明。

## 7. 测试

- 当前 10 个测试文件引用 `enclave_app` 模块，需要迁到新模块路径：
  直接 import 的 8 个 —— `test_enclave_dev_seed.py`、
  `test_enclave_route_errors.py`、`test_enclave_routeb_readside.py`、
  `test_enclave_runtime_token.py`、`test_enclave_server_perf.py`、
  `test_memory_readside.py`、`test_memory_v1_readside.py`、
  `test_memory_v1_schema.py`；用
  `importlib.import_module("enclave_app")` 动态导入的 2 个 ——
  `test_enclave_visual_plaintext.py`、`test_enclave_frame_caption.py`
  （grep 时注意这种模式不带 `import enclave_app` 字样）。HTTP 层复用主迁移留下的
  `backend/asgi_test_client.py` 或 `httpx.ASGITransport`；monkeypatch 目标
  从 `enclave_app._state` 等改为 `enclave.state._state` 等新路径。
  `tools/e2e_encryption_test.py` 与 `tests/e2e_model_api_test.py` 仍通过
  `python backend/enclave_app.py` 启服务，入口兼容性要保住。
- 新增：
  - `/image` Range/ETag 专项测试（单区间 206、并行多个单区间请求可
    拼出完整文件、multipart range 请求按整文件 200 回退、416、304）；
  - gzip 生效测试（大 JSON 响应带 `Content-Encoding: gzip`）;
  - whoami async singleflight 并发测试（N 并发冷缓存 miss 收敛为
    1 次回环）；
  - runtime-token-only 路径回归测试：api_key 为空时，所有
    decrypt-and-serve 路由拉 backend ciphertext 仍转发
    `X-Feedling-Runtime-Token`，而不是发空 auth；
  - "解密批处理不阻塞事件循环"冒烟（改造现有
    `test_enclave_server_perf.py`：大批量解密进行中 `/healthz`
    仍在时限内响应）。
- 全量测试基线不回退（主 backend 迁移后基线 2037 green）。

## 8. 上线与验证节奏

1. **本地**：单测全绿；dev-seed 模式起服务过一遍全部 11 条路由；
   `docker-compose.memory-sandbox.yaml` 冒烟。
2. **test CVM**：部署后验证 ——
   - `/attestation` 正常、quote/measurements 无回归；
   - TLS 钉扎实测（§5 三条硬验收）；
   - chat history / memory / identity / frames 解密 e2e；
   - Range 并行分块拉图实测（多流下载完整性 + 提速）；
   - runtime-token 路径与 api-key 路径各过一遍。
3. **prod**：随下一次常规上链部署进 prod；
   `FEEDLING_ENCLAVE_WORKERS` 保持 2。此次上线顺带把 prod 镜像
   （b1e72a6）落后的 runtime-token 修复批带上去。
4. **收尾（同一 PR 内）**：从 `backend/requirements.txt` 删除
   flask / flask-compress 并重新生成 `requirements.lock`；确认全仓
   `grep -r "import flask"` 为零。

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| TLS 钉扎破（iOS 审计卡红） | 同一份 PEM，DER 哈希不变；test CVM 三条硬验收（§5）先于 prod；兜底自定义 SSLContext 注入 |
| 错误码语义漂移（422 等） | §6 的手工解析约定；测试断言逐条覆盖现有错误码 |
| 解密洪峰下事件循环被意外阻塞（漏改的同步调用） | review 清单：async 路由内禁止同步 httpx / 禁止内联重 CPU 循环；perf 冒烟测试把关 |
| gzip 行为差异 | 阈值对齐 + 大响应压缩测试 |
| whoami 缓存改写引入竞态 | singleflight 并发测试；`/v1/envelope/decrypt` 不走缓存的性质由专门测试锁定 |
| 模块化大 diff 的 review 成本 | 纯函数层（envelope/readside/visual）搬迁为逐字移动；行为变化集中在 auth/backend_client/routes/serving 四处 |

## 10. 预期收益与明确不解决的事

**收益**：慢 I/O 不占线程槽（并发公平性）；CPU 解密不堵事件循环
（一个重用户不再拖垮全体，502 风暴触发条件大幅收窄）；主
backend（已 ASGI 多 worker）与 enclave 互等抽干对方线程池的拥塞
在结构上不再可能；加密核纯函数化提升可测性；镜像依赖面缩小。

**不解决**：单条解密延迟（密码学计算量不变）；CPU 总吞吐上限
（prod 8 vCPU）；dstack-gateway 每连接 ~1Mbps 限速与 CVM 出口慢
（网络层，Range 并行分块因此必须保住）；VLM caption 本身的延迟。
