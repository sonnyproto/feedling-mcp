# Phase B：通知设施 + 读端点 + 场景字段 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建 `user_notices` 通知流（emit/resolve/快照读），暴露 `GET /v1/notices`，
补两处场景字段（provider `last_test_error` + onboarding step error），为 iOS 通知
中心提供后端面。

**Architecture:** 新包 `backend/notices/`（CONTRIBUTING 分层：`core.py` 纯逻辑无路由
依赖 + `routes_asgi.py` 薄传输）。通知是**明文** JSON 文档，存在既有 `user_logs`
表的新 stream `user_notices`（不建新表），复用 `db.log_append/log_read_all/
log_patch_item/log_trim`；`item_key=dedupe_key` 做 upsert 去重。emit/resolve
**绝不抛出**——观测性设施不得拖垮主流程。

**Tech Stack:** FastAPI/Starlette（`APIRouter` + `require_auth` + `threadpool.run_db`）、
既有 `backend/db.py` log 流原语、pytest（conftest `backend_env` + `db.*` 底层 +
`asgi_test_client.make_client` / `seed_user`）。

**Spec:** `docs/superpowers/specs/2026-07-07-unified-error-surfacing-design.md`（Phase B 节）
**对外契约:** `docs/FRONTEND_ERROR_CONTRACT.md` §四（通知中心 JSON）

## Global Constraints

- **绝不自行 `git add`/`git commit`/`git stash`**：任务完成停在 working tree，用户提交。
  （任务里没有 commit 步骤，这是刻意的。）
- **emit/resolve 绝不抛出**：全 `try/except Exception → log.warning + return`，
  测试强制锁（store 抛错不外溢）。这是本 Phase 的头号风险项。
- **明文存储**：通知内容是系统错误信息非用户内容，与 `gate_decisions` 同级，
  **不走加密信封**。直接 `db.log_append` 明文 doc。
- **不建新表**：复用 `user_logs`，stream 名固定 `user_notices`。
- **CONTRIBUTING 分层**：业务逻辑进 `notices/core.py`（无 FastAPI 依赖）；路由进
  `notices/routes_asgi.py`（薄）；注册进 `asgi_app.py`（assembly-only，只加一行）。
- **枚举固定**：`VALID_SOURCES=("genesis","history_import","memory","runner","chat")`、
  `VALID_BLAME=("user_provider","provider_transient","system")`、
  `VALID_SEVERITY=("error","warning")`。
- **doc 字段即 FRONTEND_ERROR_CONTRACT §四**：`notice_id/source/error_class/blame/
  severity/user_text/detail(≤300)/dedupe_key/occurrences/first_ts/last_ts/resolved/
  resolved_ts`，一个不多一个不少（读端点直接透传 doc）。
- **⚠️ Task 4 是门控任务**：B3（chat 扇出 + catalog + 一致性测试）依赖
  `record_runtime_error` / `POST /v1/model_api/runtime_error` / consumer 的
  `classify_agent_error` 分类器——这三者在本 worktree **不存在**，在
  `feat/upstream-error-surfacing` 特性分支上。**Task 1-3 零依赖，现在就能执行；
  Task 4 只有在该特性合入本分支后才开始。** 见 Task 4 顶部的前置检查。
- 测试解释器（worktree 无自己的 venv）：
  `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv/bin/python -m pytest <file> -q`
  在 worktree 根目录跑；conftest 需本地 throwaway Postgres（55432，已在跑）。
- 工作目录：`/Users/zhengzhihao/Projects/teleport/feedling-mcp-error-contract`。

---

### Task 1: `backend/notices/` core（emit / resolve / list_notices）

**Files:**
- Create: `backend/notices/__init__.py`（空文件）
- Create: `backend/notices/core.py`
- Test: `tests/test_notices_core.py`（新建）

**Interfaces:**
- Consumes: `db.log_append/log_read_all/log_patch_item/log_trim`（`backend/db.py`，
  签名见下文注释）；`store.user_id`（str）。
- Produces:
  - `notices.core.emit(store, *, source, error_class, blame, severity, user_text, detail="", dedupe_key) -> None`
  - `notices.core.resolve(store, dedupe_key_prefix: str) -> None`
  - `notices.core.list_notices(store, *, include_resolved: bool = True) -> tuple[dict, int]`
  - 常量 `NOTICES_STREAM/NOTICES_MAX/RESOLVED_WINDOW_SEC/VALID_SOURCES/VALID_BLAME/VALID_SEVERITY`。
  Task 2 依赖 `list_notices`；Task 4 依赖 `emit`/`resolve`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_notices_core.py`：

```python
"""notices.core：emit/resolve/list 的 upsert 去重 + 快照过滤 + never-raise
（spec Phase B / B1）。

Run:  python -m pytest tests/test_notices_core.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from notices import core  # noqa: E402


def _store(uid):
    # core 只碰 store.user_id——纯逻辑测试用轻量 shim，不必 get_store。
    return type("S", (), {"user_id": uid})()


def _uid():
    import uuid
    return "usr_" + uuid.uuid4().hex[:12]


def _emit(store, **over):
    kw = dict(source="genesis", error_class="genesis_failed", blame="system",
              severity="error", user_text="蒸馏失败", detail="boom",
              dedupe_key="genesis:job_ab12")
    kw.update(over)
    core.emit(store, **kw)


def test_emit_creates_notice():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s)
    rows = db.log_read_all(uid, core.NOTICES_STREAM)
    assert len(rows) == 1
    n = rows[0]
    assert n["source"] == "genesis" and n["dedupe_key"] == "genesis:job_ab12"
    assert n["occurrences"] == 1 and n["resolved"] is False and n["resolved_ts"] is None
    assert n["notice_id"].startswith("ntc_")
    assert n["first_ts"] == n["last_ts"]
    assert set(n.keys()) == {                      # doc 形状 == 契约 §四
        "notice_id", "source", "error_class", "blame", "severity", "user_text",
        "detail", "dedupe_key", "occurrences", "first_ts", "last_ts",
        "resolved", "resolved_ts"}


def test_emit_upsert_increments_occurrences():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s); _emit(s, detail="boom2", user_text="蒸馏失败(2)")
    rows = db.log_read_all(uid, core.NOTICES_STREAM)
    assert len(rows) == 1                            # 同 key 未 resolved → 合并
    assert rows[0]["occurrences"] == 2
    assert rows[0]["detail"] == "boom2" and rows[0]["user_text"] == "蒸馏失败(2)"
    assert rows[0]["last_ts"] >= rows[0]["first_ts"]


def test_emit_after_resolve_creates_new_notice():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s)
    core.resolve(s, "genesis:")
    _emit(s)                                         # 已 resolved → 新建
    rows = db.log_read_all(uid, core.NOTICES_STREAM)
    assert len(rows) == 2
    assert rows[0]["resolved"] is True
    assert rows[1]["resolved"] is False and rows[1]["occurrences"] == 1
    assert rows[0]["notice_id"] != rows[1]["notice_id"]


def test_resolve_prefix_marks_resolved():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s, dedupe_key="chat:quota_insufficient")
    _emit(s, dedupe_key="chat:rate_limited")
    _emit(s, dedupe_key="genesis:job_x")
    core.resolve(s, "chat:")
    rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, core.NOTICES_STREAM)}
    assert rows["chat:quota_insufficient"]["resolved"] is True
    assert rows["chat:rate_limited"]["resolved"] is True
    assert rows["genesis:job_x"]["resolved"] is False   # 前缀不匹配，不动


def test_emit_never_raises_on_store_error(monkeypatch):
    uid = _uid(); seed_user(uid); s = _store(uid)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(core.db, "log_read_all", boom)
    core.emit(s, dedupe_key="genesis:x")            # 不抛
    monkeypatch.setattr(core.db, "log_read_all", lambda *a, **k: [])
    monkeypatch.setattr(core.db, "log_append", boom)
    core.emit(s, dedupe_key="genesis:y")            # 不抛


def test_resolve_never_raises_on_store_error(monkeypatch):
    uid = _uid(); seed_user(uid); s = _store(uid)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(core.db, "log_read_all", boom)
    core.resolve(s, "chat:")                         # 不抛


def test_bad_enum_dropped():
    uid = _uid(); seed_user(uid); s = _store(uid)
    core.emit(s, source="not_a_source", dedupe_key="x:1")
    core.emit(s, blame="somebody", dedupe_key="x:2")
    core.emit(s, severity="loud", dedupe_key="x:3")
    assert db.log_read_all(uid, core.NOTICES_STREAM) == []


def test_detail_clipped_to_300():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s, detail="z" * 900)
    assert len(db.log_read_all(uid, core.NOTICES_STREAM)[0]["detail"]) == 300


def test_trim_caps_rows(monkeypatch):
    uid = _uid(); seed_user(uid); s = _store(uid)
    monkeypatch.setattr(core, "NOTICES_MAX", 3)
    for i in range(5):
        _emit(s, dedupe_key=f"genesis:job_{i}")
    assert len(db.log_read_all(uid, core.NOTICES_STREAM)) == 3   # 只留最新 3


def test_list_active_and_resolved_window():
    uid = _uid(); seed_user(uid); s = _store(uid)
    import time
    now = time.time()
    # 直接 log_append 精确控制 ts，避开时间 mock：
    db.log_append(uid, core.NOTICES_STREAM, _doc("chat:active", now, resolved=False),
                  ts=now, item_key="chat:active")
    db.log_append(uid, core.NOTICES_STREAM,
                  _doc("chat:recent", now - 10, resolved=True, resolved_ts=now - 10),
                  ts=now - 10, item_key="chat:recent")
    db.log_append(uid, core.NOTICES_STREAM,
                  _doc("chat:old", now - 30 * 86400, resolved=True,
                       resolved_ts=now - 30 * 86400),
                  ts=now - 30 * 86400, item_key="chat:old")
    body, status = core.list_notices(s, include_resolved=True)
    assert status == 200
    keys = [n["dedupe_key"] for n in body["notices"]]
    assert keys == ["chat:active", "chat:recent"]    # old 超 7d 窗口被滤；按 last_ts 倒序
    body2, _ = core.list_notices(s, include_resolved=False)
    assert [n["dedupe_key"] for n in body2["notices"]] == ["chat:active"]


def _doc(key, ts, *, resolved, resolved_ts=None):
    return {
        "notice_id": "ntc_" + key.replace(":", "_"), "source": "chat",
        "error_class": "quota_insufficient", "blame": "user_provider",
        "severity": "error", "user_text": "x", "detail": "", "dedupe_key": key,
        "occurrences": 1, "first_ts": ts, "last_ts": ts,
        "resolved": resolved, "resolved_ts": resolved_ts}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_notices_core.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'notices'`。

- [ ] **Step 3: 建包与实现**

`backend/notices/__init__.py`：空文件。

`backend/notices/core.py`：

```python
"""User notices stream：系统错误的可回溯通知面（spec 2026-07-07 Phase B / B1）。

明文存储：内容是系统错误信息非用户内容，与 gate_decisions 同级，不走加密信封。
emit/resolve 绝不抛出——观测性设施不得拖垮主流程。存于既有 user_logs 表的
`user_notices` stream（不建新表），item_key=dedupe_key 做 upsert 去重。

db log 原语（backend/db.py）：
- log_append(user_id, stream, doc, ts=None, item_key=None)  → INSERT 一行
- log_read_all(user_id, stream) -> list[dict]               → 按 seq 升序全量
- log_patch_item(user_id, stream, item_key, patch) -> dict|None  → patch 最新命中行
- log_trim(user_id, stream, max_rows)                       → 只留最新 max_rows 行
"""
from __future__ import annotations

import logging
import time
import uuid

import db

log = logging.getLogger("feedling.notices")

NOTICES_STREAM = "user_notices"
NOTICES_MAX = 200
RESOLVED_WINDOW_SEC = 7 * 86400

VALID_SOURCES = ("genesis", "history_import", "memory", "runner", "chat")
VALID_BLAME = ("user_provider", "provider_transient", "system")
VALID_SEVERITY = ("error", "warning")


def _now() -> float:
    return time.time()


def emit(store, *, source, error_class, blame, severity, user_text,
         detail="", dedupe_key) -> None:
    """Upsert 一条通知。dedupe_key 命中一条**未 resolved** 的现存通知 →
    occurrences+1、刷新 last_ts/detail/user_text/blame/severity/error_class；
    否则（不存在，或最新一条已 resolved）→ 新建（occurrences=1，新 notice_id）。
    绝不抛出。"""
    try:
        if (source not in VALID_SOURCES or blame not in VALID_BLAME
                or severity not in VALID_SEVERITY):
            log.warning("notices.emit dropped: bad enum source=%r blame=%r severity=%r",
                        source, blame, severity)
            return
        uid = store.user_id
        rows = db.log_read_all(uid, NOTICES_STREAM)
        existing = None
        for r in rows:
            if r.get("dedupe_key") == dedupe_key:
                existing = r          # rows 按 seq 升序，保留最新一条（= log_patch_item 会命中的那条）
        now = _now()
        clipped = str(detail or "")[:300]
        if existing is not None and not existing.get("resolved"):
            db.log_patch_item(uid, NOTICES_STREAM, dedupe_key, {
                "occurrences": int(existing.get("occurrences", 1)) + 1,
                "last_ts": now,
                "detail": clipped,
                "user_text": user_text,
                "blame": blame,
                "severity": severity,
                "error_class": error_class,
            })
            return
        doc = {
            "notice_id": "ntc_" + uuid.uuid4().hex[:12],
            "source": source,
            "error_class": error_class,
            "blame": blame,
            "severity": severity,
            "user_text": user_text,
            "detail": clipped,
            "dedupe_key": dedupe_key,
            "occurrences": 1,
            "first_ts": now,
            "last_ts": now,
            "resolved": False,
            "resolved_ts": None,
        }
        db.log_append(uid, NOTICES_STREAM, doc, ts=now, item_key=dedupe_key)
        db.log_trim(uid, NOTICES_STREAM, NOTICES_MAX)
    except Exception:
        log.warning("notices.emit failed (swallowed)", exc_info=True)


def resolve(store, dedupe_key_prefix: str) -> None:
    """把 dedupe_key 以 prefix 开头的所有**未 resolved** 通知标记 resolved +
    resolved_ts。前缀匹配支持 'chat:'、'runner:' 这类按域清空。绝不抛出。"""
    try:
        uid = store.user_id
        rows = db.log_read_all(uid, NOTICES_STREAM)
        now = _now()
        seen = set()
        for r in rows:
            key = r.get("dedupe_key", "")
            if (key and key.startswith(dedupe_key_prefix)
                    and not r.get("resolved") and key not in seen):
                seen.add(key)
                db.log_patch_item(uid, NOTICES_STREAM, key,
                                  {"resolved": True, "resolved_ts": now})
    except Exception:
        log.warning("notices.resolve failed (swallowed)", exc_info=True)


def list_notices(store, *, include_resolved: bool = True) -> tuple[dict, int]:
    """快照式读取：活跃通知全给；resolved 仅给近 7 天且 include_resolved 时。
    按 last_ts 倒序。返回 ({"notices": [...]}, 200)。读侧不抛出交给上层
    （核心读路径异常应让请求 500，而非静默空——与 emit/resolve 语义不同）。"""
    uid = store.user_id
    rows = db.log_read_all(uid, NOTICES_STREAM)
    cutoff = _now() - RESOLVED_WINDOW_SEC
    out = []
    for r in rows:
        if not r.get("resolved"):
            out.append(r)
        elif include_resolved and float(r.get("resolved_ts") or 0) >= cutoff:
            out.append(r)
    out.sort(key=lambda r: float(r.get("last_ts") or 0), reverse=True)
    return {"notices": out}, 200
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_notices_core.py -q`
Expected: 10 passed。

- [ ] **Step 5: 回归（确认没碰到别的流）**

Run: `python -m pytest tests/test_db.py -q`
Expected: 与基线一致全绿（本任务只新增 stream 名，不改 db.py）。

---

### Task 2: `GET /v1/notices` 读端点

**Files:**
- Create: `backend/notices/routes_asgi.py`
- Modify: `backend/asgi_app.py`（`_ASGI_PACKAGES` 元组加一行）
- Test: `tests/test_notices_route.py`（新建）

**Interfaces:**
- Consumes: Task 1 `notices.core.list_notices`；`asgi.deps.require_auth`、
  `asgi.threadpool.run_db`、`accounts.auth_core.AuthResult`（现有）。
- Produces: `GET /v1/notices?include_resolved=<bool 默认 true>` →
  `{"notices": [...]}`；`register_asgi(app)`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_notices_route.py`：

```python
"""GET /v1/notices：鉴权 + 快照过滤 + include_resolved + 排序（spec Phase B / B2）。

Run:  python -m pytest tests/test_notices_route.py -q
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from asgi_test_client import make_client  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _register():
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"})
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _doc(key, ts, *, resolved, resolved_ts=None):
    return {
        "notice_id": "ntc_" + key.replace(":", "_"), "source": "chat",
        "error_class": "quota_insufficient", "blame": "user_provider",
        "severity": "error", "user_text": "x", "detail": "", "dedupe_key": key,
        "occurrences": 1, "first_ts": ts, "last_ts": ts,
        "resolved": resolved, "resolved_ts": resolved_ts}


def _seed(uid):
    now = time.time()
    db.log_append(uid, "user_notices", _doc("chat:active", now, resolved=False),
                  ts=now, item_key="chat:active")
    db.log_append(uid, "user_notices", _doc("chat:newer", now + 5, resolved=False),
                  ts=now + 5, item_key="chat:newer")
    db.log_append(uid, "user_notices",
                  _doc("chat:recent", now - 10, resolved=True, resolved_ts=now - 10),
                  ts=now - 10, item_key="chat:recent")
    db.log_append(uid, "user_notices",
                  _doc("chat:old", now - 30 * 86400, resolved=True,
                       resolved_ts=now - 30 * 86400),
                  ts=now - 30 * 86400, item_key="chat:old")


def test_requires_auth(backend_env):
    res = make_client().get("/v1/notices")
    assert res.status_code == 401


def test_snapshot_filter_and_sort(backend_env):
    uid, key = _register()
    _seed(uid)
    res = make_client().get("/v1/notices", headers={"X-API-Key": key})
    assert res.status_code == 200
    keys = [n["dedupe_key"] for n in res.get_json()["notices"]]
    # 活跃全给 + recent resolved 在 7d 内给 + old 超窗被滤；按 last_ts 倒序
    assert keys == ["chat:newer", "chat:active", "chat:recent"]


def test_include_resolved_false_hides_resolved(backend_env):
    uid, key = _register()
    _seed(uid)
    res = make_client().get("/v1/notices?include_resolved=false",
                            headers={"X-API-Key": key})
    assert res.status_code == 200
    keys = [n["dedupe_key"] for n in res.get_json()["notices"]]
    assert keys == ["chat:newer", "chat:active"]


def test_notice_doc_shape(backend_env):
    uid, key = _register()
    _seed(uid)
    res = make_client().get("/v1/notices", headers={"X-API-Key": key})
    n = res.get_json()["notices"][0]
    assert set(n.keys()) == {
        "notice_id", "source", "error_class", "blame", "severity", "user_text",
        "detail", "dedupe_key", "occurrences", "first_ts", "last_ts",
        "resolved", "resolved_ts"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_notices_route.py -q`
Expected: FAIL——`/v1/notices` 未注册（404，非 401/200）。

- [ ] **Step 3: 实现路由**

新建 `backend/notices/routes_asgi.py`：

```python
"""GET /v1/notices — 快照式通知中心读端点（spec Phase B / B2）。

require_auth（无 scope，与其它用户面端点一致）；include_resolved 为字符串
query，在此解析成 bool 再转发给 core。"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth
from notices import core as notices_core

router = APIRouter()


@router.get("/v1/notices")
async def list_notices(request: Request, auth: AuthResult = Depends(require_auth)):
    raw = str(request.query_params.get("include_resolved", "true")).lower()
    include_resolved = raw not in {"0", "false", "no"}
    body, status = await threadpool.run_db(
        notices_core.list_notices, auth.store, include_resolved=include_resolved)
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
```

- [ ] **Step 4: 注册进 asgi_app.py**

`backend/asgi_app.py` 的 `_ASGI_PACKAGES` 元组末尾（`"hosted.onboarding_validation_asgi",`
一行之后）加一行：

```python
    "notices.routes_asgi",
```

（该元组随后被 `for _mod_name in _ASGI_PACKAGES: importlib.import_module(_mod_name).register_asgi(app)`
遍历——assembly-only，不加其它逻辑。）

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_notices_route.py -q`
Expected: 4 passed。

- [ ] **Step 6: 回归（路由表装配没崩）**

Run: `python -m pytest tests/test_asgi_healthz.py tests/test_asgi_hosted_setup.py -q`
Expected: 与基线一致全绿（新增 router 不影响既有路由）。

---

### Task 3: B4 场景字段（provider `last_test_error` + onboarding step error）

**Files:**
- Modify: `backend/provider_client.py::public_config`（约 :207-220）
- Modify: `backend/hosted/onboarding_validation.py`（model_api_test step，约 :340-348）
- Test: `tests/test_scene_fields.py`（新建）+ 受影响存量测试同步更新

**Interfaces:**
- Consumes: 无（纯字段暴露）。
- Produces: `public_config(config)` 输出多一个 `last_test_error` 键；onboarding
  的 model_api_test step dict 多一个 `last_test_error` 键。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_scene_fields.py`：

```python
"""B4 场景字段：provider public_config.last_test_error + onboarding step
（spec Phase B / B4）。

Run:  python -m pytest tests/test_scene_fields.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client  # noqa: E402


def test_public_config_exposes_last_test_error():
    out = provider_client.public_config({
        "provider": "anthropic", "model": "claude-3-5-sonnet-latest",
        "test_status": "failed", "last_test_error": "403 预扣费额度失败"})
    assert out["last_test_error"] == "403 预扣费额度失败"


def test_public_config_last_test_error_defaults_empty():
    out = provider_client.public_config({"provider": "anthropic", "model": "x"})
    assert out["last_test_error"] == ""
```

⚠️ 实现者：onboarding step 的测试需先 grep 出返回 steps 的**公开函数**及其调用方式
（`grep -n "model_api_test\|def .*step\|\"steps\"" backend/hosted/onboarding_validation.py`）。
若该函数能用 `seed_user` + 写 `model_api` blob（`last_test_error` 已在其中）直接驱动，
就加一个路由/函数级断言：model_api_test step 携带 `last_test_error`。若耦合过重
（需要完整 onboarding 上下文），退回为「读代码确认 step dict 字面量含该键」并在
报告里说明——测试意图是锁字段存在，不是锁整条 onboarding 流。把你的决定写进报告。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scene_fields.py -q`
Expected: FAIL——`public_config` 输出无 `last_test_error` 键。

- [ ] **Step 3: 实现**

`backend/provider_client.py::public_config` 的返回 dict 里，在 `"updated_at"` 一行
之后加一行（`last_test_error` 已由 `setup_core.model_api_test` 持久化进 `model_api`
blob，这里只是把它序列化出去）：

```python
        "last_test_error": str(config.get("last_test_error") or ""),
```

`backend/hosted/onboarding_validation.py` 的 model_api_test step dict（含
`"test_status"` 那个）里，紧邻 `"test_status"` 一行加：

```python
        "last_test_error": (config or {}).get("last_test_error", ""),
```

（保持既有 step 字段顺序与风格——`required` 恒在、空串表「无」，`last_test_error`
同理恒在、空串表「无已知失败原因」。）

- [ ] **Step 4: 修受影响的存量测试**

Run: `grep -rn "public_config\|last_test_at\|test_status" tests/ | grep -v test_scene_fields`
对断言了 `public_config` 输出**精确键集**（`set(out.keys()) == {...}` 或逐键清点）的
用例：把 `last_test_error` 补进期望集（**不许削弱**——只加键，不删既有断言）。对
onboarding steps 做精确形状断言的用例同理。逐个列在报告里；若无精确键集断言则报告
写明「无受影响存量断言」。

- [ ] **Step 5: 跑测试确认通过 + 回归**

Run: `python -m pytest tests/test_scene_fields.py -q`
Run: `python -m pytest tests/ -q -k "provider or onboarding or model_api" --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -5`
Expected: 全绿（含上一步同步更新过的存量测试）。

---

### Task 4 (⚠️ 门控): B3 chat 双写 + catalog + 一致性测试

> **前置检查——不满足则不要开始本任务，回报控制器**：
> 1. `grep -n "def record_runtime_error" backend/hosted/config_store.py` 有命中
>    （`feat/upstream-error-surfacing` 已合入本分支）。
> 2. `grep -rn "classify_agent_error\|_ERROR_CLASS" tools/chat_resident_consumer.py`
>    有命中（consumer 分类器已在本分支）。
>
> 两者任一为空 → 本任务**不可执行**，向控制器报 BLOCKED（依赖未合入），
> Task 1-3 的交付不受影响、可独立合并部署（通知流只写不读时零用户可见风险）。

**Files（前置满足后）:**
- Create: `backend/notices/catalog.py`
- Modify: `backend/hosted/config_store.py::record_runtime_error`（扇出 emit/resolve）
- Test: `tests/test_chat_notice_fanout.py`（新建）、`tests/test_catalog_consumer_parity.py`（新建）

**Interfaces:**
- Consumes: Task 1 `notices.core.emit/resolve`；consumer 侧的 error_class 全集
  （来源 = `tools/chat_resident_consumer.py` 的分类器，执行时读取以钉住枚举）。
- Produces: `catalog.blame_for(error_class) -> str`、
  `catalog.user_text_for(error_class, **ctx) -> str`、
  `catalog.ERROR_CLASSES`（frozenset，供一致性测试比对）。

- [ ] **Step 1: 钉住 error_class 全集（本任务第一步，不可跳过）**

读 `tools/chat_resident_consumer.py` 的分类器（`classify_agent_error` 及其
`error_class` 取值），把**全部** error_class 列出来，作为 catalog 的 key 全集。
spec 记录的基线 8 类 + 计划新增（provider_incompatible/context_overflow/
content_filtered/runner_degraded）——**以合入后代码里的实际枚举为准**，不照抄本行。
把清点结果写进报告。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_catalog_consumer_parity.py`（参照 `tests/test_api_errors_doc.py`
的「两来源各取 set 再 diff」写法）：

```python
"""catalog 覆盖 consumer 分类器的全部 error_class（spec Phase B / B3 一致性纪律）。

consumer 在 tools/ 不能 import backend，catalog 在 backend/——两处各自维护，
用本测试锁一致性：catalog.ERROR_CLASSES ⊇ consumer 的全部 error_class。

Run:  python -m pytest tests/test_catalog_consumer_parity.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent))          # 让 tools 可 import
from notices import catalog  # noqa: E402
# 实现者：按 consumer 暴露 error_class 全集的真实方式导入
# （常量集合 / 从分类规则表推导），下面这行按实际改：
from tools.chat_resident_consumer import CONSUMER_ERROR_CLASSES  # noqa: E402


def test_catalog_covers_all_consumer_error_classes():
    missing = set(CONSUMER_ERROR_CLASSES) - set(catalog.ERROR_CLASSES)
    assert not missing, f"catalog 缺 error_class: {sorted(missing)}"


def test_every_catalog_blame_is_valid():
    from notices import core
    for ec in catalog.ERROR_CLASSES:
        assert catalog.blame_for(ec) in core.VALID_BLAME
```

> 若 consumer 没有现成的 `CONSUMER_ERROR_CLASSES` 常量集合可 import，实现者需在
> consumer 里**补一个导出常量**（把分类器已有的 error_class 收成一个 frozenset，
> 不改分类逻辑），或从分类规则表 keys 推导——目标是让一致性测试有稳定的机读来源。
> 把选择写进报告。

新建 `tests/test_chat_notice_fanout.py`：

```python
"""record_runtime_error 扇出到 user_notices（spec Phase B / B3）。

Run:  python -m pytest tests/test_chat_notice_fanout.py -q
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import config_store  # noqa: E402
from notices import core as notices_core


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def _seed_model_api(uid):
    seed_user(uid)
    store = core_store.get_store(uid)
    config_store._save_model_api_config(
        store, {"provider": "anthropic", "model": "claude-3-5-sonnet-latest"})
    config_store._ensure_model_api_runtime_profile(store)
    return store


def test_error_fans_out_to_notice():
    uid = _uid(); store = _seed_model_api(uid)
    config_store.record_runtime_error(
        store, error="403 预扣费额度失败", error_class="quota_insufficient")
    rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
    assert "chat:quota_insufficient" in rows
    n = rows["chat:quota_insufficient"]
    assert n["source"] == "chat" and n["resolved"] is False
    assert n["detail"] == "403 预扣费额度失败"


def test_clear_resolves_chat_notices():
    uid = _uid(); store = _seed_model_api(uid)
    config_store.record_runtime_error(
        store, error="403 预扣费额度失败", error_class="quota_insufficient")
    config_store.record_runtime_error(store, error="", error_class="")   # 清空
    rows = db.log_read_all(uid, notices_core.NOTICES_STREAM)
    assert all(r["resolved"] for r in rows if r["dedupe_key"].startswith("chat:"))
```

- [ ] **Step 3: 实现 catalog**

新建 `backend/notices/catalog.py`：`ERROR_CLASSES`（frozenset，= Step 1 钉住的全集）
+ `_CATALOG = {error_class: (blame, user_text_template)}`（blame ∈ core.VALID_BLAME；
user_text 含动态占位由 `user_text_for(ec, **ctx)` 填充）+ `blame_for` / `user_text_for`。
**blame 三分类纪律**（照 FRONTEND_ERROR_CONTRACT §二）：`system` 类**绝不**能引导用户
充值/改 key/改配置。逐条从 consumer 分类器的既有话术搬（同一来源纪律）。

- [ ] **Step 4: 实现 record_runtime_error 扇出**

在 `backend/hosted/config_store.py::record_runtime_error` 内（写完
`last_runtime_error` 之后）加扇出（`from notices import core as notices_core`、
`from notices import catalog`）：

```python
    try:
        store_obj = ...  # record_runtime_error 已持有的 store
        if error:
            notices_core.emit(
                store_obj, source="chat", error_class=error_class,
                blame=catalog.blame_for(error_class), severity="error",
                user_text=catalog.user_text_for(error_class),
                detail=error, dedupe_key=f"chat:{error_class}")
        else:
            notices_core.resolve(store_obj, "chat:")
    except Exception:
        pass   # 扇出绝不影响 record_runtime_error 主职责（emit/resolve 本身已 never-raise，这是双保险）
```

（以合入后 `record_runtime_error` 的真实签名/局部变量名为准接线；consumer 零改动
——它继续 `POST /v1/model_api/runtime_error`，扇出在服务端。）

- [ ] **Step 5: 跑测试确认通过 + 回归**

Run: `python -m pytest tests/test_catalog_consumer_parity.py tests/test_chat_notice_fanout.py tests/test_notices_core.py tests/test_model_api_runtime_error_route.py -q`
Expected: 全绿。
Run: `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -5`
Expected: 通过数 = 基线 + 本 Phase 新增，failed 不多于基线 pre-existing。

---

## Self-Review 结论（已跑）

- **Spec 覆盖**：B1→Task 1，B2→Task 2，B3→Task 4（门控），B4→Task 3，B5 测试
  散入各任务（core/route/scene 各自带测，chat 扇出 + catalog 一致性在 Task 4）。✓
- **占位符**：Task 3 的 onboarding 测试、Task 4 的 error_class 钉住/consumer 导出
  常量是**刻意的现场决策点**（依赖合入后代码的真实形状），已写明两条路与判据；
  其余无 TBD。Task 4 顶部有硬前置检查，不满足即 BLOCKED。✓
- **类型一致**：`emit`/`resolve`/`list_notices` 签名在 Task 1 定义、Task 2/4 消费一致；
  doc 13 字段集在 Task 1 测试、Task 2 测试、契约 §四三处对齐；stream 名 `user_notices`
  常量单一来源。✓
- **已知取舍**：emit/resolve 是「读—改」两步非事务，并发同 dedupe_key 可能产生两条
  occurrences=1 而非一条 occurrences=2——观测性可接受（spec 明示非事务、never-raise
  优先），已在 core.py 注释记录；不引入 advisory-lock（写放大不值当）。
- **依赖边界**：Task 1-3 基于本 worktree 现状可完整执行、独立可部署（通知流只写不读
  或读空，零用户可见风险）；Task 4 门控在 `feat/upstream-error-surfacing` 合入之后。
