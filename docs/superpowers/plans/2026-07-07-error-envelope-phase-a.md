# Phase A：同步 HTTP 错误信封 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 全后端非 2xx 响应统一信封（error slug + 可选 blame/detail/request_id），补兜底
500 处理器与校验错误统一形状，建立 slug 契约文档。

**Architecture:** 中央 `api_error()` helper + 请求级 request_id contextvar（access-log
中间件生成、异常处理器消费、响应头回带）+ 两个新异常处理器（Exception 兜底、
RequestValidationError 重塑）。存量 117 处 `{"error": ...}` 不迁移，只收敛 6 处
HTTP 面自由文本错误。

**Tech Stack:** FastAPI/Starlette 异常处理器、contextvars、pytest（conftest
`backend_env` + `asgi_test_client.make_client` / httpx ASGITransport 子应用模式）。

**Spec:** `docs/superpowers/specs/2026-07-07-unified-error-surfacing-design.md`（Phase A 节）
**对外契约:** `docs/FRONTEND_ERROR_CONTRACT.md` §三

## Global Constraints

- **绝不自行 `git add`/`git commit`/`git stash`**：任务完成停在 working tree，用户提交。
  （任务里没有 commit 步骤，这是刻意的。）
- 存量 `{"error": ...}` 返回的 slug 与状态码**一字不动**（现有测试断言不得破坏）。
- 新字段（blame/detail/request_id）全部增量可选——不出现在存量响应里。
- enclave 服务（backend/enclave/*）是另一个 surface，**本 Phase 不碰**。
- blame 枚举固定：`user_provider | provider_transient | system`。
- 测试在仓库根目录跑：`python -m pytest tests/<file> -q`（conftest 需本地 throwaway
  Postgres，见 tests/conftest.py 头部说明）。
- 工作目录：`/Users/zhengzhihao/Projects/teleport/feedling-mcp-error-contract`。

---

### Task 1: `api_error()` 中央 helper

**Files:**
- Modify: `backend/asgi/responses.py`
- Test: `tests/test_api_error_envelope.py`（新建）

**Interfaces:**
- Produces: `responses.api_error(status: int, slug: str, *, blame: str = "",
  detail=None, request_id: str = "") -> JSONResponse`。Task 3 依赖此签名。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_api_error_envelope.py`：

```python
"""api_error(): 统一错误信封的中央 helper（spec Phase A / A1）。

Run:  python -m pytest tests/test_api_error_envelope.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi import responses  # noqa: E402


def _body(resp):
    return json.loads(resp.body)


def test_minimal_envelope_only_error_field():
    resp = responses.api_error(404, "not_found")
    assert resp.status_code == 404
    assert _body(resp) == {"error": "not_found"}   # 可选字段缺省不出现


def test_full_envelope():
    resp = responses.api_error(
        500, "internal_error",
        blame="system", detail={"hint": "x"}, request_id="req_a1b2c3d4")
    assert _body(resp) == {
        "error": "internal_error",
        "blame": "system",
        "detail": {"hint": "x"},
        "request_id": "req_a1b2c3d4",
    }


def test_invalid_blame_rejected():
    # blame 是三值枚举（FRONTEND_ERROR_CONTRACT.md §二），传错是编程错误——立刻炸
    import pytest
    with pytest.raises(ValueError):
        responses.api_error(400, "x", blame="somebody_else")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_api_error_envelope.py -q`
Expected: FAIL，`AttributeError: module ... has no attribute 'api_error'`

- [ ] **Step 3: 实现**

`backend/asgi/responses.py` 末尾追加：

```python
VALID_BLAME = ("user_provider", "provider_transient", "system")


def api_error(status: int, slug: str, *, blame: str = "", detail=None,
              request_id: str = "") -> JSONResponse:
    """统一错误信封（spec 2026-07-07-unified-error-surfacing Phase A）。

    ``slug`` 是稳定契约面（docs/API_ERRORS.md）；blame/detail/request_id 为
    增量可选字段，缺省不出现在 body——老客户端零感知。新代码与被触碰的路由
    渐进采用；存量 ``{"error": ...}`` 返回不强制迁移。"""
    if blame and blame not in VALID_BLAME:
        raise ValueError(f"invalid blame: {blame!r}")
    body: dict = {"error": slug}
    if blame:
        body["blame"] = blame
    if detail is not None:
        body["detail"] = detail
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_api_error_envelope.py -q`
Expected: 3 passed。

---

### Task 2: request_id contextvar + access-log 集成 + 响应头

**Files:**
- Modify: `backend/asgi/context.py`（新 contextvar）
- Modify: `backend/asgi/middleware.py`（AccessLogMiddleware 生成/回带/记日志）
- Test: `tests/test_request_id.py`（新建）

**Interfaces:**
- Consumes: 无。
- Produces: `context.current_request_id: ContextVar[str]`（default `""`）；
  `context.new_request_id() -> str`（生成 `req_` + 8 hex）。Task 3 依赖两者。
  响应头 `X-Request-Id` 对**所有**经过 AccessLogMiddleware 的请求回带。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_request_id.py`：

```python
"""request_id：access-log 中间件生成 → contextvar → 响应头（spec Phase A / A2）。

Run:  python -m pytest tests/test_request_id.py -q
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi import context as asgi_context  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi.settings import settings  # noqa: E402
from fastapi import FastAPI  # noqa: E402


def _build_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/echo-rid")
    async def echo_rid():
        return {"rid": asgi_context.current_request_id.get()}

    return middleware.AccessLogMiddleware(app)


def _get(app, path):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get(path)
    return asyncio.run(go())


def test_request_id_format():
    rid = asgi_context.new_request_id()
    assert re.fullmatch(r"req_[0-9a-f]{8}", rid)
    assert asgi_context.new_request_id() != rid


def test_header_and_contextvar_agree(monkeypatch):
    monkeypatch.setattr(settings, "access_log", True)
    resp = _get(_build_app(), "/echo-rid")
    rid_header = resp.headers.get("x-request-id", "")
    assert re.fullmatch(r"req_[0-9a-f]{8}", rid_header)
    assert resp.json()["rid"] == rid_header   # handler 里读到的和头上回带的是同一个


def test_two_requests_get_distinct_ids(monkeypatch):
    monkeypatch.setattr(settings, "access_log", True)
    app = _build_app()
    a = _get(app, "/echo-rid").headers["x-request-id"]
    b = _get(app, "/echo-rid").headers["x-request-id"]
    assert a != b
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_request_id.py -q`
Expected: FAIL，`AttributeError: ... no attribute 'current_request_id'`

- [ ] **Step 3: 实现 contextvar**

`backend/asgi/context.py` 末尾追加（照 `current_user_id` 的既有模式）：

```python
# 请求级 request id：AccessLogMiddleware 在请求入口生成并放这里 + 回带
# X-Request-Id 响应头；错误处理器（asgi/middleware.py 的 500 兜底）从这里取，
# 让「用户报障给的 id」「错误响应体」「访问日志行」三者天然同 id 对账
# （spec 2026-07-07-unified-error-surfacing A2）。
current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_request_id", default=""
)


def new_request_id() -> str:
    import uuid
    return "req_" + uuid.uuid4().hex[:8]
```

- [ ] **Step 4: 实现中间件集成**

`backend/asgi/middleware.py` 的 `AccessLogMiddleware.__call__`：

(a) 方法开头（`if scope["type"] != "http" ...` 判定之后、`start = time.monotonic()`
附近）生成并设置：

```python
        req_id = asgi_context.new_request_id()
        asgi_context.current_request_id.set(req_id)
```

（文件头 import 区补 `from asgi import context as asgi_context`，如已有别名对齐即可。）

(b) `send_wrapper` 内 `http.response.start` 分支追加响应头：

```python
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [
                    (b"x-request-id", req_id.encode("ascii")),
                ]
```

注意：现有代码在同一分支里遍历 `message.get("headers", [])` 读 content-length——
追加动作放在遍历**之后**，避免改动读逻辑。

(c) 访问日志行尾追加 ` rid=%s` 字段（找到该文件里格式化 `[req]` 行的地方，
在行末补 `req_id`；保持既有字段顺序不变，只在末尾加）。

(d) ⚠️ `access_log` 关闭或 `/healthz` 的早退分支不经过这段——request_id 在该
配置下缺失是可接受的（错误处理器有兜底生成，见 Task 3）。**不要**为此把生成
逻辑挪出中间件。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_request_id.py -q`
Expected: 3 passed。

- [ ] **Step 6: 回归**

Run: `python -m pytest tests/test_asgi_chat_remaining.py tests/test_asgi_hosted_setup.py -q`
Expected: 与基线一致全绿（中间件改动不破坏既有请求路径）。

---

### Task 3: 兜底 Exception 处理器 + RequestValidationError 重塑

**Files:**
- Modify: `backend/asgi/middleware.py::register_exception_handlers`（约 :103 起）
- Test: `tests/test_exception_handlers.py`（新建）

**Interfaces:**
- Consumes: Task 1 `responses.api_error(...)`；Task 2
  `context.current_request_id` / `context.new_request_id()`。
- Produces: 未捕获异常 → `500 {"error":"internal_error","request_id":"req_..."}`；
  校验失败 → `400 {"error":"invalid_payload","detail":[...]}`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_exception_handlers.py`：

```python
"""兜底 500 信封 + RequestValidationError 重塑（spec Phase A / A2）。

用独立子应用测（照 tests/test_asgi_hosted_setup.py 的 _build_asgi_app 模式）：
注册 register_exception_handlers + 两条会出错的路由。
Run:  python -m pytest tests/test_exception_handlers.py -q
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi import middleware  # noqa: E402
from fastapi import FastAPI  # noqa: E402


def _build_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom secret detail")

    @app.get("/typed/{n}")
    async def typed(n: int):
        return {"n": n}

    return app


def _get(path):
    async def go():
        # raise_app_exceptions=False：让 500 走 handler 而不是直接抛给测试
        transport = httpx.ASGITransport(app=_build_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get(path)
    return asyncio.run(go())


def test_uncaught_exception_becomes_internal_error_envelope():
    resp = _get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "internal_error"
    assert re.fullmatch(r"req_[0-9a-f]{8}", body["request_id"])
    # 不泄漏异常内容给客户端——detail 只进服务端日志
    assert "kaboom" not in resp.text


def test_validation_error_reshaped_to_invalid_payload():
    resp = _get("/typed/not-a-number")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_payload"
    assert isinstance(body["detail"], list) and body["detail"]
    # detail 条目精简为 {loc, msg} 两键
    assert set(body["detail"][0].keys()) == {"loc", "msg"}


def test_uncaught_exception_logged_with_same_request_id(caplog):
    import logging
    with caplog.at_level(logging.ERROR):
        resp = _get("/boom")
    rid = resp.json()["request_id"]
    assert any(rid in r.message for r in caplog.records)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_exception_handlers.py -q`
Expected: `/boom` 现状无 handler → 3 个测试 FAIL（500 默认体/422 形状/无日志 id）。

- [ ] **Step 3: 实现**

`backend/asgi/middleware.py::register_exception_handlers` 内追加（放在现有
`_http_exception` 之后；import 区补
`from asgi import context as asgi_context`、`from asgi import responses`、
`from fastapi.exceptions import RequestValidationError`、`import logging` 并取
该文件既有 logger，如无则 `log = logging.getLogger("feedling.asgi")`）：

```python
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request, exc: RequestValidationError):
        # FastAPI 默认 422 {"detail":[...]}——重塑进统一信封，消灭双形状
        # （FRONTEND_ERROR_CONTRACT.md §三：invalid_payload）。
        detail = [
            {"loc": ".".join(str(p) for p in e.get("loc", ())),
             "msg": str(e.get("msg", ""))}
            for e in exc.errors()[:10]
        ]
        return responses.api_error(400, "invalid_payload", detail=detail)

    @app.exception_handler(Exception)
    async def _unhandled(request, exc: Exception):
        # 兜底 500：统一信封 + request_id，traceback 只进服务端日志（同 id 对账）。
        # AccessLogMiddleware 未启用（access_log=False / healthz）时现场补生成。
        rid = asgi_context.current_request_id.get() or asgi_context.new_request_id()
        log.exception("[%s] unhandled exception on %s %s",
                      rid, request.method, request.url.path)
        return responses.api_error(500, "internal_error", request_id=rid)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_exception_handlers.py -q`
Expected: 3 passed。

- [ ] **Step 5: 回归**

Run: `python -m pytest tests/test_asgi_chat_remaining.py tests/test_asgi_hosted_setup.py tests/test_asgi_admin.py -q`
Expected: 与基线一致全绿。特别关注：既有测试若有故意触发 422 的用例，形状断言
需同步更新为 `invalid_payload`（如有改动，逐个列在报告里）。

---

### Task 4: HTTP 面自由文本错误收敛（6 处）

**Files:**
- Modify: `backend/chat/chat_core.py:299,383,398`
- Modify: `backend/memory/memory_core.py:292,418`
- Modify: `backend/memory/actions.py:160`
- Test: `tests/test_error_slug_convergence.py`（新建）+ 受影响存量测试同步更新

**Interfaces:**
- Consumes: 无（返回 dict 的 core 函数不经过 api_error helper，保持二元组模式）。
- Produces: 稳定 slug `envelope_missing_fields` / `anchor_required`（进 Task 5 的
  API_ERRORS.md）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_error_slug_convergence.py`：

```python
"""自由文本错误 → slug + detail 收敛（spec Phase A / A3）。

Run:  python -m pytest tests/test_error_slug_convergence.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi_test_client import make_client  # noqa: E402
import base64  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _register():
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201
    body = res.get_json()
    return body["user_id"], body["api_key"]


def test_chat_message_missing_envelope_fields_is_slug(backend_env):
    uid, key = _register()
    res = make_client().post(
        "/v1/chat/message",
        headers={"X-API-Key": key},
        json={"envelope": {"v": 1, "id": "m1"}},   # 缺 body_ct/nonce/K_user…
    )
    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "envelope_missing_fields"        # slug，非自由文本
    assert isinstance(body["detail"], list) and "body_ct" in body["detail"]


def test_memory_missing_envelope_fields_is_slug(backend_env):
    uid, key = _register()
    res = make_client().post(
        "/v1/memory/cards",
        headers={"X-API-Key": key},
        json={"envelope": {"v": 1, "id": "c1"}},
    )
    # 端点路径以实际 memory 写卡路由为准（grep memory_core 的调用方确认；
    # 若该 core 函数只被非 HTTP 调用方使用，则直接单测 core 函数返回值）
    if res.status_code == 404:
        import memory.memory_core as mc
        body, status = mc_write_result = None, None
        # 回退：直接驱动 core（实现者按 memory_core.py:292 所在函数真实签名调用）
        raise AssertionError("route not found — test the core function directly, see plan note")
    assert res.status_code == 400
    assert res.get_json()["error"] == "envelope_missing_fields"
```

⚠️ 实现者注意：第二个测试的路由路径需先 grep `memory_core.py:292` 所在函数的
HTTP 调用方确认；若无直达路由，改为直接调用 core 函数断言返回二元组
`({"error": "envelope_missing_fields", "detail": [...]}, 400)`——测试意图是锁
slug 形状，不是锁路由。`actions.py:160` 与 `memory_core.py:418` 的
`{mem_type}_requires_anchor` 同理：改为固定 slug `anchor_required` + detail 带
`mem_type`，用 core 级单测锁。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_error_slug_convergence.py -q`
Expected: FAIL——现状返回 `envelope missing fields: [...]` 自由文本。

- [ ] **Step 3: 实现收敛**

六处统一改法（以 chat_core.py:299 为例，其余同型）：

```python
    missing = [f for f in _ENVELOPE_REQUIRED if not envelope.get(f)]
    if missing:
        return {"error": "envelope_missing_fields", "detail": missing}, 400
```

`thinking_envelope`（chat_core.py:398）：

```python
        return {"error": "thinking_envelope_missing_fields", "detail": missing}, 400
```

anchor 两处（memory_core.py:418、actions.py:160）：

```python
                    "error": "anchor_required",
                    "detail": {"mem_type": new_type},   # actions.py 处为 mem_type
```

（各处保留原状态码与包裹结构，只动 error 值 + 加 detail。）

- [ ] **Step 4: 修受影响的存量测试**

Run: `grep -rn "envelope missing fields\|requires_anchor" tests/ | grep -v test_error_slug_convergence`
对命中的每个断言：更新为新 slug 形状（**不许削弱**——原断言若检查了缺失字段名，
改为断言 `detail` 含该字段名）。逐个列在报告里。

- [ ] **Step 5: 跑测试确认通过 + 回归**

Run: `python -m pytest tests/test_error_slug_convergence.py tests/test_asgi_chat_remaining.py tests/test_chat_system_notice_role.py tests/test_chat_poll_core.py -q`
（memory 相关：`python -m pytest tests/ -q -k "memory" 2>&1 | tail -3`）
Expected: 全绿。

---

### Task 5: API_ERRORS.md 契约表 + CONTRIBUTING 纪律

**Files:**
- Create: `docs/API_ERRORS.md`
- Modify: `CONTRIBUTING.md`（错误返回纪律一段）
- Test: `tests/test_api_errors_doc.py`（新建——文档与代码不脱钩的守卫）

**Interfaces:**
- Consumes: Task 3/4 新增的 slug（internal_error/invalid_payload/
  envelope_missing_fields/thinking_envelope_missing_fields/anchor_required）。
- Produces: slug 契约文档（iOS 本地化表的输入源）。

- [ ] **Step 1: 写失败测试（文档守卫）**

新建 `tests/test_api_errors_doc.py`：

```python
"""docs/API_ERRORS.md 与代码不脱钩的守卫（spec Phase A / A3）。

不追求全量反向核对（部分 slug 是动态拼接），只锁两个方向：
1. 本计划引入/触碰的关键 slug 必须在文档里；
2. 文档里的每个 slug 行格式合法（可被 iOS 侧脚本解析成本地化表）。
Run:  python -m pytest tests/test_api_errors_doc.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

DOC = Path(__file__).parent.parent / "docs" / "API_ERRORS.md"

MUST_HAVE = {
    "internal_error", "invalid_payload", "envelope_missing_fields",
    "thinking_envelope_missing_fields", "anchor_required",
    "unauthorized", "forbidden", "service_busy", "not_found",
    "model_api_key_decrypt_failed", "already_answered",
}


def _doc_slugs():
    text = DOC.read_text(encoding="utf-8")
    # 契约表行格式：| `slug` | 状态码 | ... |
    return set(re.findall(r"^\| `([a-z][a-z0-9_]+)` \|", text, re.M))


def test_doc_exists_and_has_required_slugs():
    slugs = _doc_slugs()
    missing = MUST_HAVE - slugs
    assert not missing, f"API_ERRORS.md 缺 slug: {sorted(missing)}"


def test_doc_rows_have_status_code_column():
    text = DOC.read_text(encoding="utf-8")
    rows = [l for l in text.splitlines() if re.match(r"^\| `[a-z]", l)]
    assert rows, "契约表为空"
    for l in rows:
        cols = [c.strip() for c in l.split("|")]
        # | `slug` | <code> | <blame> | <说明> | → split 后至少 6 段
        assert len(cols) >= 6, f"行格式不对: {l}"
        assert re.match(r"^\d{3}(/\d{3})*$|^—$", cols[2]), f"状态码列不合法: {l}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_api_errors_doc.py -q`
Expected: FAIL（文档不存在）。

- [ ] **Step 3: 写 API_ERRORS.md**

创建 `docs/API_ERRORS.md`，结构：

```markdown
# API 错误 slug 契约表

> `{"error": "<slug>"}` 的 slug 是稳定 API 面：一经写入本表即冻结，废弃走
> 「新增新 slug、旧 slug 保留」。新增错误返回必须先登记到本表（CONTRIBUTING
> 有此纪律）。iOS 本地化表以本表为输入；「需本地化」为空的 slug 走通用文案。
> 对外渲染规则见 docs/FRONTEND_ERROR_CONTRACT.md。

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `unauthorized` | 401 | — | 未认证/凭证失效 | ✅ |
| `forbidden` | 403 | — | 无权限 | ✅ |
| `internal_error` | 500 | system | 未捕获异常兜底（必带 request_id） | ✅ |
| `invalid_payload` | 400 | — | body/参数校验失败（detail 带字段错误列表） | ✅ |
| `envelope_missing_fields` | 400 | — | 加密信封缺字段（detail 带缺失字段名） | |
| `thinking_envelope_missing_fields` | 400 | — | 同上，thinking 信封 | |
| `anchor_required` | 400 | — | 记忆动作缺 anchor（detail.mem_type） | |
| ... |
```

全量行从现状 slug 盘入——实现者跑：
`grep -rh '{"error":' backend/ --include="*.py" | grep -o '"error": "[a-z_0-9]*"' | sort -u`
逐个入表（57 个左右）。每行：状态码从代码返回处读；blame 只给能明确判定的
（判不了留 `—`）；「需本地化」按 FRONTEND_ERROR_CONTRACT.md §三的目录勾选，
其余留空。enclave/* 的 slug 单独一节标注「enclave surface，iOS 不直连」。

- [ ] **Step 4: CONTRIBUTING.md 补纪律**

在 CONTRIBUTING.md 的适当章节（错误处理/代码规范附近，读文件后就近插入）加：

```markdown
## 错误返回纪律

- 路由/core 的错误返回必须用稳定 slug：`{"error": "<snake_case_slug>", ...}`，
  禁止自由文本（如 f-string 拼接的句子）——动态内容放 `detail` 字段。
- 新增 slug 必须同 PR 登记进 `docs/API_ERRORS.md`（有测试守卫锁关键 slug）。
- slug 一经发布即冻结；语义变更走新增新 slug。
- 用户可见的话术不在后端维护（iOS 按 slug 本地映射）；`blame` 枚举见
  `backend/asgi/responses.py::VALID_BLAME`。
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_api_errors_doc.py -q`
Expected: 2 passed。

- [ ] **Step 6: 全量基线**

Run: `python -m pytest tests/ -q 2>&1 | tail -3`
Expected: 与执行前基线一致（改动前先跑一次记录；pre-existing 失败不算回归）。

---

## Self-Review 结论（已跑）

- **Spec 覆盖**：A1→Task 1，A2→Task 2+3，A3→Task 4+5，A4 测试散入各任务。✓
- **占位符**：Task 4 第二个测试留了「路由待确认否则测 core」的显式指引——这是
  刻意的现场决策点（memory 路由面待 grep），非占位符；其余无 TBD。✓
- **类型一致**：`api_error` 签名、`current_request_id`/`new_request_id` 命名在
  Task 1/2/3 间一致；slug 命名在 Task 4/5 间一致。✓
- 已知取舍：`{"error": ...}` 二元组模式的 core 函数不强制走 api_error helper
  （它们不产 JSONResponse）——helper 服务于异常处理器与新路由；存量渐进迁移。
```
