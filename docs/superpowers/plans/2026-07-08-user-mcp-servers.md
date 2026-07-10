# 用户 MCP 服务器（user_mcp）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户在 iOS 配置远程 HTTP MCP server（URL+自定义请求头，信封加密），经 poll 下发，consumer 物化成 claude/codex 原生 MCP 配置（托管保证可用）与通用 user-mcp.json（自跑送达），仅聊天回合生效。

**Architecture:** 配置分发模型（spec v2）：后端存信封化配置并在 poll_context 广告 fingerprint；consumer 检测变化 → 拉 `/v1/mcp/envelopes` → enclave 解密 → 物化文件；CLI 模板加 `{mcp}` 占位符按回合注入。后端唯一出站是 `/test` 连通性探测。无后端 MCP 代理层。

**Tech Stack:** FastAPI/httpx（后端）、纯 Python 物化模块（consumer 侧）、无新第三方依赖（probe 手写 JSON-RPC，不引 `mcp` SDK）。

**Spec:** `docs/superpowers/specs/2026-07-08-user-mcp-servers-design.md`（必读）

## Global Constraints

- **目标分支 test**：所有实现基于 `origin/test` 拉特性分支 `feat/user-mcp-servers`，在独立 worktree 进行；**不要**在 pre 工作树改代码。
- **绝不自行 `git add` / `git commit`**（用户硬规则）。每个任务结束时改动留在工作树、报告完成；提交由用户显式决定。计划里没有 commit 步骤，这是有意的。
- 遵循 `CONTRIBUTING.md`：新端点 = 领域包 `routes_asgi.py`（APIRouter + `register_asgi`，注册进 `asgi_app._ASGI_PACKAGES`）；路由体委托同包 `*_core.py`；阻塞调用走 `await threadpool.run_db(...)`。
- 限额（spec §3.1，逐字）：每用户最多 **10** 个 server；每 server headers 最多 **20** 个、总大小 ≤ **8KB**；URL 强制 `https://`；`name` 匹配 `^[a-z0-9_-]{1,32}$`。
- blob kind：`user_mcp`；信封 purpose 标签：`mcp_server_config`（enclave 端无 purpose 白名单，纯 trace 标签，无需改 enclave）。
- 日志永不落 header 值（只落 header 名）。
- 测试命令模板：`cd <worktree>/backend && PYTHONPATH=. python -m pytest ../tests/<file> -v`（venv 见仓库 README；全量回归跑 `python -m pytest ../tests -x -q`）。

---

### Task 0: 工作区准备

**Files:**
- 无代码改动；建 worktree + 复制 spec/plan 文档。

**Steps:**

- [ ] **Step 1: 从 origin/test 建特性 worktree**

```bash
cd /Users/zhengzhihao/Projects/teleport/feedling-mcp
git fetch origin test
git worktree add ../feedling-mcp-user-mcp -b feat/user-mcp-servers origin/test
```

- [ ] **Step 2: 复制 spec 与本计划到新 worktree**

```bash
cp docs/superpowers/specs/2026-07-08-user-mcp-servers-design.md ../feedling-mcp-user-mcp/docs/superpowers/specs/
cp docs/superpowers/plans/2026-07-08-user-mcp-servers.md ../feedling-mcp-user-mcp/docs/superpowers/plans/
```

- [ ] **Step 3: 基线测试**

Run: `cd ../feedling-mcp-user-mcp/backend && PYTHONPATH=. python -m pytest ../tests -x -q`
Expected: 全绿（若有 pre-existing 失败，记录清单，后续任务以此为基线）。

后续所有任务的路径均相对 `../feedling-mcp-user-mcp/`（下称 worktree 根）。

---

### Task 1: mcp_core — 存储、fingerprint、CRUD 核心

**Files:**
- Create: `backend/hosted/mcp_core.py`
- Test: `tests/test_user_mcp_core.py`

**Interfaces:**
- Consumes: `db.get_blob/set_blob`、`core.envelope._build_shared_envelope_for_store(store, raw: bytes, item_id: str) -> tuple[dict|None, str|None]`（范例 `backend/hosted/setup_core.py:53-58`）、`core.util._now_iso()`。
- Produces（后续任务依赖的确切签名）：
  - `USER_MCP_BLOB = "user_mcp"`
  - `fingerprint_for_store(store) -> str`（无配置返回 `""`；Task 4 poll_core 用）
  - `list_servers(store) -> tuple[dict, int]`
  - `upsert_server(store, payload: dict) -> tuple[dict, int]`
  - `set_enabled(store, name: str, payload: dict) -> tuple[dict, int]`
  - `delete_server(store, name: str) -> tuple[dict, int]`
  - `envelopes_payload(store) -> tuple[dict, int]`（Task 3 路由 + Task 6 consumer 用；形状 `{"fingerprint": str, "servers": [{"name","enabled","config_envelope"}]}`）
  - `validate_url_syntax(url: str) -> str|None`（返回错误码或 None；https 强制在此，私网校验在 Task 2 的 probe 模块，此处 import 调用）

- [ ] **Step 1: 写失败测试**（节选，完整覆盖：空列表 / upsert 后列表含 hint 不含明文 / 同名覆盖 / 超限 / http 拒绝 / 坏 name / PATCH 只动 enabled / delete / fingerprint 随每次变更改变、无配置为空串 / envelopes_payload 形状）

```python
# tests/test_user_mcp_core.py
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from accounts import registry  # noqa: E402
from hosted import mcp_core  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    user = registry.register_user(public_key="A" * 43 + "=", archive_language="en")
    return core_store.get_store(user["user_id"])


def _fake_envelope(monkeypatch):
    # 信封构建依赖 enclave 在线，单测替换为确定性 stub
    from core import envelope as core_envelope
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store",
        lambda store, raw, item_id: ({"v": 1, "id": item_id, "ct": raw.hex()}, None),
    )


def test_list_empty(store):
    body, status = mcp_core.list_servers(store)
    assert status == 200
    assert body == {"servers": []}
    assert mcp_core.fingerprint_for_store(store) == ""


def test_upsert_and_list_masks_secrets(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "jira",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer secret-token"},
    })
    assert status == 200, body
    body, _ = mcp_core.list_servers(store)
    (srv,) = body["servers"]
    assert srv["name"] == "jira"
    assert srv["url_hint"] == "mcp.example.com"
    assert srv["header_names"] == ["Authorization"]
    assert srv["enabled"] is True
    assert "secret-token" not in str(body)
    assert "config_envelope" not in srv


def test_http_url_rejected(store, monkeypatch):
    _fake_envelope(monkeypatch)
    body, status = mcp_core.upsert_server(store, {
        "name": "x", "url": "http://mcp.example.com", "headers": {}})
    assert status == 400
    assert body["error"]["kind"] == "https_required"


def test_limits(store, monkeypatch):
    _fake_envelope(monkeypatch)
    for i in range(10):
        _, s = mcp_core.upsert_server(store, {
            "name": f"s{i}", "url": "https://a.example.com", "headers": {}})
        assert s == 200
    body, status = mcp_core.upsert_server(store, {
        "name": "s10", "url": "https://a.example.com", "headers": {}})
    assert status == 400 and body["error"]["kind"] == "too_many_servers"


def test_patch_enabled_keeps_envelope(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    before = mcp_core.envelopes_payload(store)[0]["servers"][0]["config_envelope"]
    fp_before = mcp_core.fingerprint_for_store(store)
    body, status = mcp_core.set_enabled(store, "jira", {"enabled": False})
    assert status == 200 and body["enabled"] is False
    after = mcp_core.envelopes_payload(store)[0]["servers"][0]
    assert after["config_envelope"] == before and after["enabled"] is False
    assert mcp_core.fingerprint_for_store(store) != fp_before


def test_envelopes_payload_shape(store, monkeypatch):
    _fake_envelope(monkeypatch)
    mcp_core.upsert_server(store, {"name": "jira", "url": "https://a.example.com", "headers": {}})
    body, status = mcp_core.envelopes_payload(store)
    assert status == 200
    assert body["fingerprint"] == mcp_core.fingerprint_for_store(store)
    (srv,) = body["servers"]
    assert set(srv) == {"name", "enabled", "config_envelope"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_core.py -v`
Expected: FAIL（`No module named 'hosted.mcp_core'`）

- [ ] **Step 3: 实现 `backend/hosted/mcp_core.py`**

```python
"""User-configured remote HTTP MCP servers (spec: 2026-07-08-user-mcp-servers-design).

Storage: one per-user blob (kind ``user_mcp``). Secrets (url+headers) live ONLY
inside a shared X25519 envelope (purpose label ``mcp_server_config``); plaintext
metadata is what the iOS list screen shows. ``fingerprint`` is advertised on
every ``/v1/chat/poll`` so the resident consumer knows when to re-materialize.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from urllib.parse import urlparse

import db
from core import envelope as core_envelope
from core import util as core_util
from core.store import UserStore

USER_MCP_BLOB = "user_mcp"
MAX_SERVERS = 10
MAX_HEADERS = 20
MAX_HEADERS_BYTES = 8192
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# Host header is forged by the client stack; MCP session headers are owned by it.
_FORBIDDEN_HEADERS = {"host"}


def _err(kind: str, detail: str = "") -> dict:
    return {"error": {"kind": kind, "detail": detail}}


def _load(store: UserStore) -> dict:
    data = db.get_blob(store.user_id, USER_MCP_BLOB)
    if not isinstance(data, dict):
        return {"fingerprint": "", "servers": []}
    data.setdefault("fingerprint", "")
    data.setdefault("servers", [])
    return data


def compute_fingerprint(servers: list[dict]) -> str:
    if not servers:
        return ""
    basis = [
        {"name": s["name"], "enabled": bool(s.get("enabled")),
         "envelope_id": (s.get("config_envelope") or {}).get("id", "")}
        for s in sorted(servers, key=lambda s: s["name"])
    ]
    return "sha256:" + hashlib.sha256(
        json.dumps(basis, sort_keys=True).encode()).hexdigest()


def _save(store: UserStore, servers: list[dict]) -> dict:
    data = {"fingerprint": compute_fingerprint(servers), "servers": servers,
            "updated_at": core_util._now_iso()}
    db.set_blob(store.user_id, USER_MCP_BLOB, data)
    return data


def fingerprint_for_store(store: UserStore) -> str:
    return str(_load(store).get("fingerprint") or "")


def validate_url_syntax(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return "invalid_url"
    if parsed.scheme != "https":
        return "https_required"
    if not parsed.hostname:
        return "invalid_url"
    return None


def _validate_payload(name: str, url: str, headers: dict) -> dict | None:
    if not _NAME_RE.match(name or ""):
        return _err("invalid_name", "name must match ^[a-z0-9_-]{1,32}$")
    kind = validate_url_syntax(url)
    if kind:
        return _err(kind, url and urlparse(url).scheme or "")
    if not isinstance(headers, dict):
        return _err("invalid_headers", "headers must be an object")
    if len(headers) > MAX_HEADERS:
        return _err("too_many_headers", f"max {MAX_HEADERS}")
    total = sum(len(str(k)) + len(str(v)) for k, v in headers.items())
    if total > MAX_HEADERS_BYTES:
        return _err("headers_too_large", f"max {MAX_HEADERS_BYTES} bytes")
    for k in headers:
        if str(k).strip().lower() in _FORBIDDEN_HEADERS:
            return _err("forbidden_header", str(k))
    return None


def _public(srv: dict) -> dict:
    return {k: srv[k] for k in
            ("id", "name", "enabled", "url_hint", "header_names",
             "created_at", "updated_at")}


def list_servers(store: UserStore) -> tuple[dict, int]:
    servers = _load(store)["servers"]
    return {"servers": [_public(s) for s in servers]}, 200


def upsert_server(store: UserStore, payload: dict) -> tuple[dict, int]:
    name = str(payload.get("name") or "").strip()
    url = str(payload.get("url") or "").strip()
    headers = payload.get("headers") or {}
    err = _validate_payload(name, url, headers)
    if err:
        return err, 400
    # deep SSRF pre-check (DNS resolve) — friendly early error; the probe
    # re-checks at connect time anyway.
    from hosted import mcp_probe
    kind = mcp_probe.blocked_url_kind(url)
    if kind:
        return _err(kind, urlparse(url).hostname or ""), 400
    servers = _load(store)["servers"]
    existing = next((s for s in servers if s["name"] == name), None)
    if existing is None and len(servers) >= MAX_SERVERS:
        return _err("too_many_servers", f"max {MAX_SERVERS}"), 400
    secret = json.dumps({"url": url, "headers": {str(k): str(v) for k, v in headers.items()}})
    envelope, enc_err = core_envelope._build_shared_envelope_for_store(
        store, secret.encode("utf-8"), item_id=f"user_mcp_{uuid.uuid4().hex}")
    if envelope is None:
        return _err("cannot_encrypt", str(enc_err or "")), 409
    now = core_util._now_iso()
    record = {
        "id": existing["id"] if existing else f"srv_{uuid.uuid4().hex[:8]}",
        "name": name,
        "enabled": bool(payload.get("enabled", True)),
        "config_envelope": envelope,
        "url_hint": urlparse(url).hostname or "",
        "header_names": sorted(str(k) for k in headers),
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    servers = [s for s in servers if s["name"] != name] + [record]
    _save(store, servers)
    return _public(record), 200


def set_enabled(store: UserStore, name: str, payload: dict) -> tuple[dict, int]:
    servers = _load(store)["servers"]
    srv = next((s for s in servers if s["name"] == name), None)
    if srv is None:
        return _err("not_found", name), 404
    srv["enabled"] = bool(payload.get("enabled"))
    srv["updated_at"] = core_util._now_iso()
    _save(store, servers)
    return _public(srv), 200


def delete_server(store: UserStore, name: str) -> tuple[dict, int]:
    servers = _load(store)["servers"]
    if not any(s["name"] == name for s in servers):
        return _err("not_found", name), 404
    _save(store, [s for s in servers if s["name"] != name])
    return {"deleted": name}, 200


def envelopes_payload(store: UserStore) -> tuple[dict, int]:
    data = _load(store)
    return {
        "fingerprint": data["fingerprint"],
        "servers": [
            {"name": s["name"], "enabled": bool(s.get("enabled")),
             "config_envelope": s["config_envelope"]}
            for s in data["servers"]
        ],
    }, 200
```

注意：`from hosted import mcp_probe` 在函数内 lazy import（Task 2 才创建该模块；本任务测试先给它一个 stub —— 在测试文件顶部 `monkeypatch` 前置或先创建仅含 `blocked_url_kind = lambda url: None` 的占位模块。推荐直接把 Task 2 的 `blocked_url_kind` 先实现为纯语法版返回 `None`，Task 2 再补 DNS 逻辑）。

- [ ] **Step 4: 创建占位 `backend/hosted/mcp_probe.py`**（Task 2 完整实现）

```python
"""Connectivity probe + SSRF guard for user MCP servers (filled in by Task 2)."""


def blocked_url_kind(url: str) -> str | None:
    return None
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_core.py -v`
Expected: PASS（全部）

- [ ] **Step 6: 改动留工作树，报告完成（不 commit）**

---

### Task 2: mcp_probe — SSRF 防护 + 连通性探测

**Files:**
- Modify: `backend/hosted/mcp_probe.py`（替换 Task 1 占位）
- Test: `tests/test_user_mcp_probe.py`

**Interfaces:**
- Produces:
  - `blocked_url_kind(url: str) -> str|None`（`"blocked_url"` / `"dns"` / None；Task 1 upsert 已调用）
  - `probe(url: str, headers: dict, *, transport=None) -> dict`：成功返回 `{"ok": True, "tool_count": int, "tool_names": [str]}`；失败 raise `ProbeError`
  - `class ProbeError(Exception)`：属性 `kind`（`dns|timeout|tls|http_401|http_403|http_404|http_4xx|http_5xx|protocol|blocked_url`）、`detail: str`
  - `transport` 参数：传给 `httpx.Client(transport=...)`，测试注入 `httpx.ASGITransport` 用
- Consumes: 无（httpx、socket、ipaddress 标准栈）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_user_mcp_probe.py
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from hosted import mcp_probe  # noqa: E402


def _fake_mcp_app(require_auth: str | None = None):
    """进程内 fake streamable-HTTP MCP server（JSON 响应模式）。"""
    async def app(scope, receive, send):
        assert scope["type"] == "http"
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body"):
                break
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        if require_auth and headers.get("authorization") != require_auth:
            await _respond(send, 401, {"error": "unauthorized"})
            return
        req = json.loads(body) if body else {}
        method = req.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "search", "description": "d", "inputSchema": {}},
                                {"name": "fetch", "description": "d", "inputSchema": {}}]}
        elif method == "notifications/initialized":
            await _respond(send, 202, None)
            return
        else:
            await _respond(send, 400, {"error": "bad method"})
            return
        await _respond(send, 200, {"jsonrpc": "2.0", "id": req.get("id"), "result": result})

    async def _respond(send, status, payload):
        data = json.dumps(payload).encode() if payload is not None else b""
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"mcp-session-id", b"sess-1")]})
        await send({"type": "http.response.body", "body": data})

    return app


def test_probe_happy_path():
    transport = httpx.ASGITransport(app=_fake_mcp_app())
    out = mcp_probe.probe("https://mcp.example.com/mcp", {}, transport=transport)
    assert out == {"ok": True, "tool_count": 2, "tool_names": ["search", "fetch"]}


def test_probe_forwards_headers():
    transport = httpx.ASGITransport(app=_fake_mcp_app(require_auth="Bearer tok"))
    with pytest.raises(mcp_probe.ProbeError) as e:
        mcp_probe.probe("https://mcp.example.com/mcp", {}, transport=transport)
    assert e.value.kind == "http_401"
    out = mcp_probe.probe("https://mcp.example.com/mcp",
                          {"Authorization": "Bearer tok"}, transport=transport)
    assert out["ok"] is True


@pytest.mark.parametrize("url,kind", [
    ("https://127.0.0.1/mcp", "blocked_url"),
    ("https://10.1.2.3/mcp", "blocked_url"),
    ("https://192.168.1.1/mcp", "blocked_url"),
    ("https://169.254.169.254/latest", "blocked_url"),
    ("https://[::1]/mcp", "blocked_url"),
])
def test_blocked_urls(url, kind):
    assert mcp_probe.blocked_url_kind(url) == kind
    with pytest.raises(mcp_probe.ProbeError) as e:
        mcp_probe.probe(url, {})
    assert e.value.kind == "blocked_url"


def test_blocked_url_kind_public_ok(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips", lambda host: ["93.184.216.34"])
    assert mcp_probe.blocked_url_kind("https://mcp.example.com/x") is None


def test_dns_failure(monkeypatch):
    monkeypatch.setattr(mcp_probe, "_resolve_ips",
                        lambda host: (_ for _ in ()).throw(OSError("nx")))
    assert mcp_probe.blocked_url_kind("https://no-such.example.invalid/") == "dns"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_probe.py -v`
Expected: FAIL（占位模块无 `probe`/`ProbeError`）

- [ ] **Step 3: 完整实现 `backend/hosted/mcp_probe.py`**

```python
"""Connectivity probe + SSRF guard for user MCP servers.

The ONLY backend-originated outbound call in the user_mcp feature (spec §6).
Hand-rolled single-shot JSON-RPC over streamable HTTP — initialize →
notifications/initialized → tools/list — deliberately NOT the `mcp` SDK
(one endpoint doesn't justify the dependency + requirements.lock churn).

SSRF guard: the URL host must resolve to global addresses only. Checked
immediately before connecting (small TOCTOU/DNS-rebinding window is a
documented residual risk — spec §6); redirects are disabled outright.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlparse

import httpx

_CONNECT_TIMEOUT = 10.0
_TOTAL_TIMEOUT = 30.0
_PROTOCOL_VERSION = "2025-03-26"


class ProbeError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _resolve_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def blocked_url_kind(url: str) -> str | None:
    """"blocked_url" when the host resolves to any non-global address,
    "dns" when it doesn't resolve, None when clean."""
    host = urlparse(url).hostname or ""
    if not host:
        return "blocked_url"
    try:
        ip = ipaddress.ip_address(host)
        return None if ip.is_global else "blocked_url"
    except ValueError:
        pass  # hostname, not a literal IP
    try:
        ips = _resolve_ips(host)
    except OSError:
        return "dns"
    for raw in ips:
        if not ipaddress.ip_address(raw).is_global:
            return "blocked_url"
    return None


def _classify_http(status: int) -> str:
    if status in (401, 403, 404):
        return f"http_{status}"
    if 400 <= status < 500:
        return "http_4xx"
    return "http_5xx"


def _parse_rpc_response(resp: httpx.Response) -> dict:
    """Streamable HTTP servers answer either application/json or a one-shot
    SSE stream; take the first `data:` event in the latter case."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise ProbeError("protocol", "empty SSE stream")
    try:
        return resp.json()
    except json.JSONDecodeError:
        raise ProbeError("protocol", f"non-JSON response ({ctype})")


def probe(url: str, headers: dict, *, transport=None) -> dict:
    kind = blocked_url_kind(url)
    if kind in ("blocked_url", "dns"):
        raise ProbeError(kind, urlparse(url).hostname or "")

    send_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    send_headers.setdefault("Accept", "application/json, text/event-stream")
    send_headers["Content-Type"] = "application/json"

    def _post(client: httpx.Client, payload: dict, extra: dict) -> httpx.Response:
        try:
            return client.post(url, json=payload, headers={**send_headers, **extra})
        except httpx.ConnectTimeout:
            raise ProbeError("timeout", "connect timeout")
        except httpx.TimeoutException:
            raise ProbeError("timeout", "read timeout")
        except httpx.ConnectError as e:
            detail = str(e)[:160]
            raise ProbeError("tls" if "ssl" in detail.lower() else "dns", detail)

    timeout = httpx.Timeout(_TOTAL_TIMEOUT, connect=_CONNECT_TIMEOUT)
    with httpx.Client(timeout=timeout, follow_redirects=False,
                      transport=transport) as client:
        resp = _post(client, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "feedling-probe", "version": "1.0"}},
        }, {})
        if resp.status_code >= 400:
            raise ProbeError(_classify_http(resp.status_code), resp.text[:160])
        if resp.status_code in (301, 302, 307, 308):
            raise ProbeError("protocol", "redirects not allowed")
        _parse_rpc_response(resp)  # validates the handshake succeeded
        session = {}
        sid = resp.headers.get("mcp-session-id")
        if sid:
            session["Mcp-Session-Id"] = sid
        # spec-required before further requests; tolerate servers that 4xx it
        _post(client, {"jsonrpc": "2.0",
                       "method": "notifications/initialized"}, session)
        resp = _post(client, {"jsonrpc": "2.0", "id": 2,
                              "method": "tools/list"}, session)
        if resp.status_code >= 400:
            raise ProbeError(_classify_http(resp.status_code), resp.text[:160])
        body = _parse_rpc_response(resp)
        if "error" in body:
            raise ProbeError("protocol", json.dumps(body["error"])[:160])
        tools = (body.get("result") or {}).get("tools") or []
        names = [str(t.get("name") or "") for t in tools]
        return {"ok": True, "tool_count": len(names), "tool_names": names}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_probe.py ../tests/test_user_mcp_core.py -v`
Expected: PASS（Task 1 的测试因 `blocked_url_kind` 变严也要仍绿——其 fixture 用 `monkeypatch.setattr(mcp_probe, "_resolve_ips", ...)` 或用可解析的公网 IP 字面量；若 Task 1 测试因 DNS 失败，把测试 URL 换成 `https://93.184.216.34/mcp` 或在 fixture 里 stub `_resolve_ips`）

- [ ] **Step 5: 改动留工作树，报告完成（不 commit）**

---

### Task 3: 路由 — 管理端点 + envelopes + /test

**Files:**
- Create: `backend/hosted/mcp_routes_asgi.py`
- Modify: `backend/asgi_app.py:33-57`（`_ASGI_PACKAGES` 追加 `"hosted.mcp_routes_asgi",`）
- Modify: `backend/hosted/mcp_core.py`（追加 `test_server`）
- Test: `tests/test_user_mcp_routes.py`

**Interfaces:**
- Consumes: Task 1/2 全部；`asgi.deps.require_auth`、`asgi.threadpool.run_db`、`accounts.auth_core.extract_api_key`（模式照抄 `backend/hosted/setup_routes_asgi.py:42-52`）；`core.enclave._decrypt_envelope_via_enclave(envelope, api_key, purpose=..., runtime_token=...) -> bytes`（`backend/core/enclave.py:133`）。
- Produces（iOS/consumer 契约，spec §4）：
  - `GET /v1/mcp/servers`、`POST /v1/mcp/servers`、`PATCH /v1/mcp/servers/{name}`、`DELETE /v1/mcp/servers/{name}`、`POST /v1/mcp/servers/{name}/test`、`GET /v1/mcp/envelopes`
  - `require_auth` 天然接受 api_key 与 runtime-token（`accounts/auth_core.py:152` 先试 runtime_token），envelopes 端点无需额外分支。

- [ ] **Step 1: 在 `mcp_core.py` 追加 `test_server`**

```python
def test_server(store: UserStore, name: str, caller_api_key: str | None) -> tuple[dict, int]:
    from core import enclave as core_enclave
    from hosted import mcp_probe
    servers = _load(store)["servers"]
    srv = next((s for s in servers if s["name"] == name), None)
    if srv is None:
        return _err("not_found", name), 404
    try:
        secret = json.loads(core_enclave._decrypt_envelope_via_enclave(
            srv["config_envelope"], caller_api_key,
            purpose="mcp_server_config").decode("utf-8"))
    except Exception as e:
        return _err("decrypt_failed", str(e)[:160]), 400
    try:
        out = mcp_probe.probe(secret["url"], secret.get("headers") or {})
    except mcp_probe.ProbeError as e:
        return _err(e.kind, e.detail), 400
    return out, 200
```

- [ ] **Step 2: 写失败的路由测试**（fixture 照抄 `tests/test_diagnostics_routes.py:24-50` 的 `client`+`_register` 模式；信封与解密均 monkeypatch）

```python
# tests/test_user_mcp_routes.py  （节选——完整覆盖 6 端点 + 401 无认证 + /test 成功/probe失败）
def test_crud_roundtrip(client, monkeypatch):
    _fake_envelope(monkeypatch)   # 同 Task 1
    _, key = _register(client)
    h = {"X-API-Key": key}
    r = client.post("/v1/mcp/servers", headers=h, json={
        "name": "jira", "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer tok"}})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = client.get("/v1/mcp/servers", headers=h)
    assert r.get_json()["servers"][0]["url_hint"] == "mcp.example.com"
    r = client.open("/v1/mcp/servers/jira", method="PATCH", headers=h,
                    json={"enabled": False})
    assert r.status_code == 200 and r.get_json()["enabled"] is False
    r = client.get("/v1/mcp/envelopes", headers=h)
    body = r.get_json()
    assert body["fingerprint"].startswith("sha256:")
    assert body["servers"][0]["config_envelope"]
    r = client.delete("/v1/mcp/servers/jira", headers=h)
    assert r.status_code == 200
    assert client.get("/v1/mcp/servers", headers=h).get_json() == {"servers": []}


def test_test_endpoint_decrypts_and_probes(client, monkeypatch):
    _fake_envelope(monkeypatch)
    from core import enclave as core_enclave
    from hosted import mcp_core, mcp_probe
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave",
        lambda env, key, purpose, runtime_token="": json.dumps(
            {"url": "https://mcp.example.com/mcp", "headers": {}}).encode())
    monkeypatch.setattr(mcp_probe, "probe",
        lambda url, headers, transport=None: {"ok": True, "tool_count": 1,
                                              "tool_names": ["search"]})
    _, key = _register(client)
    h = {"X-API-Key": key}
    client.post("/v1/mcp/servers", headers=h, json={
        "name": "jira", "url": "https://mcp.example.com/mcp", "headers": {}})
    r = client.post("/v1/mcp/servers/jira/test", headers=h)
    assert r.status_code == 200 and r.get_json()["tool_count"] == 1
```

- [ ] **Step 3: 跑测试确认失败**（404：路由不存在）

- [ ] **Step 4: 实现 `backend/hosted/mcp_routes_asgi.py`**

```python
"""ASGI surface for user MCP server config (spec 2026-07-08-user-mcp-servers).

Management endpoints (iOS, api_key) + the consumer-facing ``/v1/mcp/envelopes``
(api_key OR runtime-token — ``require_auth`` resolves both). Everything
delegates to ``hosted.mcp_core``; blocking work runs via ``threadpool.run_db``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_auth
from hosted import mcp_core

router = APIRouter()


@router.get("/v1/mcp/servers")
async def mcp_list(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(mcp_core.list_servers, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/mcp/servers")
async def mcp_upsert(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(mcp_core.upsert_server, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.patch("/v1/mcp/servers/{name}")
async def mcp_patch(name: str, request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(mcp_core.set_enabled, auth.store, name, payload)
    return JSONResponse(body, status_code=status)


@router.delete("/v1/mcp/servers/{name}")
async def mcp_delete(name: str, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(mcp_core.delete_server, auth.store, name)
    return JSONResponse(body, status_code=status)


@router.post("/v1/mcp/servers/{name}/test")
async def mcp_test(name: str, request: Request, auth: AuthResult = Depends(require_auth)):
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        mcp_core.test_server, auth.store, name, caller_api_key)
    return JSONResponse(body, status_code=status)


@router.get("/v1/mcp/envelopes")
async def mcp_envelopes(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(mcp_core.envelopes_payload, auth.store)
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
```

并在 `backend/asgi_app.py` 的 `_ASGI_PACKAGES` 元组（第 33-57 行）`"hosted.setup_routes_asgi",` 之后加一行 `"hosted.mcp_routes_asgi",`。

- [ ] **Step 5: 跑测试确认通过**；随后全量回归

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_routes.py -v && PYTHONPATH=. python -m pytest ../tests -x -q`
Expected: 新测试 PASS；全量与 Task 0 基线一致

- [ ] **Step 6: 改动留工作树，报告完成（不 commit）**

---

### Task 4: poll 契约 — 广告 fingerprint

**Files:**
- Modify: `backend/chat/poll_core.py`（`poll_context` ~:22-34；`build_response` ~:67-78）
- Test: `tests/test_user_mcp_poll.py`

**Interfaces:**
- Consumes: `mcp_core.fingerprint_for_store(store) -> str`
- Produces: poll 响应新增顶层字段 `"user_mcp": {"fingerprint": "<sha256:...|''>"}`（Task 6 consumer 依赖）

- [ ] **Step 1: 写失败测试**（用 make_client 走真实 `/v1/chat/poll?timeout=0`）

```python
# tests/test_user_mcp_poll.py（节选）
def test_poll_advertises_user_mcp_fingerprint(client, monkeypatch):
    _fake_envelope(monkeypatch)
    _, key = _register(client)
    h = {"X-API-Key": key}
    r = client.get("/v1/chat/poll?timeout=0", headers=h)
    assert r.get_json()["user_mcp"] == {"fingerprint": ""}
    client.post("/v1/mcp/servers", headers=h, json={
        "name": "jira", "url": "https://mcp.example.com/mcp", "headers": {}})
    r = client.get("/v1/chat/poll?timeout=0", headers=h)
    assert r.get_json()["user_mcp"]["fingerprint"].startswith("sha256:")
```

- [ ] **Step 2: 跑测试确认失败**（KeyError: 'user_mcp'）

- [ ] **Step 3: 实现**——`poll_core.poll_context` 加（lazy import，注释说明依赖方向同 `resident_runtime_v2` 先例）：

```python
    from hosted import mcp_core  # lazy: chat poll must not own hosted startup

    return {
        "runtime_v2": resident_runtime_v2.resident_runtime_v2_public_profile(store),
        "client_release": {"expected_consumer_commit": chat_consumer.expected_consumer_commit()},
        "user_mcp": {"fingerprint": mcp_core.fingerprint_for_store(store)},
    }
```

`build_response` 返回 dict 加一行 `"user_mcp": context["user_mcp"],`。

- [ ] **Step 4: 跑测试确认通过 + poll 相关既有测试回归**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_poll.py -v && PYTHONPATH=. python -m pytest ../tests -x -q -k "poll or chat"`
Expected: PASS；若既有 poll 契约测试断言了响应的精确键集合，把 `user_mcp` 加进期望

- [ ] **Step 5: 改动留工作树，报告完成（不 commit）**

---

### Task 5: 物化模块 — tools/user_mcp_materialize.py（纯函数）

**Files:**
- Create: `tools/user_mcp_materialize.py`
- Test: `tests/test_user_mcp_materialize.py`

**Interfaces:**
- Consumes: 无（纯 stdlib）。输入 `servers: list[dict]`，元素 `{"name": str, "enabled": bool, "url": str, "headers": dict}`（Task 6 解密后的形状）。
- Produces（Task 6 consumer 依赖的确切签名）:
  - `claude_mcp_json(servers) -> str`（enabled-only；`{"mcpServers": {...}}`；空时返回 `'{"mcpServers": {}}'`）
  - `claude_allow_rules(servers) -> list[str]`（`["mcp__jira__*", ...]`，enabled-only，排序）
  - `merge_settings_allow(settings_text: str|None, rules: list[str]) -> str`（幂等：先滤掉旧 `mcp__` 规则再并入）
  - `codex_config_merged(existing_text: str|None, servers) -> str`（幂等 marker 块合并；无 enabled server 时返回剥掉 marker 块后的原文，可为 `""`）
  - `MARKER_BEGIN` / `MARKER_END` 常量

- [ ] **Step 1: 写失败测试**（覆盖：claude json 形状/禁用剔除/空；allow rules；settings 合并幂等+保留既有规则；codex 块生成+对既有 gateway config 的保留+重复物化幂等+清空；header 值含引号/反斜杠的 TOML 转义）

```python
# tests/test_user_mcp_materialize.py（节选）
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools import user_mcp_materialize as m  # noqa: E402

SRV = [
    {"name": "jira", "enabled": True, "url": "https://a.example.com/mcp",
     "headers": {"Authorization": 'Bearer "quoted"\\x'}},
    {"name": "off", "enabled": False, "url": "https://b.example.com", "headers": {}},
]


def test_claude_mcp_json():
    doc = json.loads(m.claude_mcp_json(SRV))
    assert set(doc["mcpServers"]) == {"jira"}
    assert doc["mcpServers"]["jira"] == {
        "type": "http", "url": "https://a.example.com/mcp",
        "headers": {"Authorization": 'Bearer "quoted"\\x'}}


def test_allow_rules():
    assert m.claude_allow_rules(SRV) == ["mcp__jira__*"]


def test_merge_settings_allow_idempotent():
    base = json.dumps({"permissions": {"defaultMode": "acceptEdits",
                                       "allow": ["Bash(python io_cli.py perception:*)",
                                                 "mcp__stale__*"]}})
    out = json.loads(m.merge_settings_allow(base, ["mcp__jira__*"]))
    allow = out["permissions"]["allow"]
    assert "Bash(python io_cli.py perception:*)" in allow
    assert "mcp__jira__*" in allow and "mcp__stale__*" not in allow
    again = m.merge_settings_allow(m.merge_settings_allow(base, ["mcp__jira__*"]),
                                   ["mcp__jira__*"])
    assert json.loads(again) == out


def test_codex_config_merge_preserves_and_is_idempotent():
    gateway = '[model_providers.feedling_gateway]\nbase_url = "http://127.0.0.1:4000/v1"\n'
    merged = m.codex_config_merged(gateway, SRV)
    assert merged.startswith(gateway)
    assert '[mcp_servers.jira]' in merged and 'url = "https://a.example.com/mcp"' in merged
    assert "off" not in merged.split(m.MARKER_BEGIN)[1]
    assert m.codex_config_merged(merged, SRV) == merged        # 幂等
    assert m.codex_config_merged(merged, []) == gateway         # 清空剥块


def test_codex_header_toml_escaping():
    merged = m.codex_config_merged(None, SRV)
    # json.dumps 转义得到合法 TOML basic string
    assert '"Authorization" = "Bearer \\"quoted\\"\\\\x"' in merged
```

- [ ] **Step 2: 跑测试确认失败**（模块不存在）

- [ ] **Step 3: 实现 `tools/user_mcp_materialize.py`**

```python
"""Pure materialization helpers for user-configured MCP servers.

The resident consumer (hosted CVM AND self-hosted VPS — same process) turns the
decrypted server list into on-disk agent config:
  - claude: an ``--mcp-config`` JSON file + ``settings.json`` permission rules
  - codex:  a marker-delimited ``[mcp_servers.*]`` block merged into
    ``config.toml`` WITHOUT disturbing the spawner-owned gateway section
  - any other runtime: the claude-shaped JSON doubles as the generic
    ``user-mcp.json`` documented for VPS agents (io-onboarding skill).

Pure functions only — no I/O, no env — so the whole surface unit-tests without
importing the consumer.
"""

from __future__ import annotations

import json

MARKER_BEGIN = "# --- feedling user_mcp (managed) — do not edit ---"
MARKER_END = "# --- end feedling user_mcp ---"


def _enabled(servers: list[dict]) -> list[dict]:
    return sorted((s for s in servers if s.get("enabled")),
                  key=lambda s: s["name"])


def claude_mcp_json(servers: list[dict]) -> str:
    doc = {"mcpServers": {
        s["name"]: {"type": "http", "url": s["url"],
                    "headers": dict(s.get("headers") or {})}
        for s in _enabled(servers)
    }}
    return json.dumps(doc, indent=2, ensure_ascii=False)


def claude_allow_rules(servers: list[dict]) -> list[str]:
    return [f"mcp__{s['name']}__*" for s in _enabled(servers)]


def merge_settings_allow(settings_text: str | None, rules: list[str]) -> str:
    try:
        settings = json.loads(settings_text) if settings_text else {}
    except json.JSONDecodeError:
        settings = {}
    perms = settings.setdefault("permissions", {})
    allow = [r for r in perms.get("allow") or [] if not str(r).startswith("mcp__")]
    perms["allow"] = allow + list(rules)
    return json.dumps(settings, indent=2)


def _toml_str(value: str) -> str:
    # json string escaping is valid TOML basic-string escaping for our inputs
    return json.dumps(str(value), ensure_ascii=False)


def _strip_managed_block(text: str) -> str:
    if MARKER_BEGIN not in text:
        return text
    head, _, rest = text.partition(MARKER_BEGIN)
    _, _, tail = rest.partition(MARKER_END)
    return head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")


def codex_config_merged(existing_text: str | None, servers: list[dict]) -> str:
    base = _strip_managed_block(existing_text or "")
    enabled = _enabled(servers)
    if not enabled:
        return base
    lines = [MARKER_BEGIN]
    for s in enabled:
        lines.append(f"[mcp_servers.{s['name']}]")
        lines.append(f"url = {_toml_str(s['url'])}")
        headers = s.get("headers") or {}
        if headers:
            pairs = ", ".join(f"{_toml_str(k)} = {_toml_str(v)}"
                              for k, v in sorted(headers.items()))
            lines.append(f"http_headers = {{ {pairs} }}")
        lines.append("startup_timeout_sec = 20")
        lines.append("")
    lines.append(MARKER_END)
    block = "\n".join(lines)
    if base.strip():
        return base.rstrip("\n") + "\n\n" + block + "\n"
    return block + "\n"
```

- [ ] **Step 4: 跑测试确认通过**（若断言与实现的空白/换行细节不符，修断言使其精确锁定行为）

- [ ] **Step 5: 改动留工作树，报告完成（不 commit）**

---

### Task 6: consumer — poll 感知、拉取解密、物化、按回合注入

**Files:**
- Modify: `tools/chat_resident_consumer.py`：
  - `poll_chat()`（:3863-3871）
  - 新函数区（`_update_chat_runtime_v2_profile` :3874 附近）
  - `run()` 主循环（:7281-7349，`_maybe_self_update` 调用点 :7287 之后）
  - `_render_cli_template()`（:3133-3156）、`_prepare_cli_command()`（:3159）、`call_agent_cli()`（:3328/3337）、`call_agent()`（:3517-3529）
  - 聊天 lane 调用点 :6712 / :6714 / :6721；proactive lane :6066
- Test: `tests/test_user_mcp_consumer.py`（env 前置 + import 模式照抄 `tests/test_chat_resident_consumer_image.py:29-40`）

**Interfaces:**
- Consumes: Task 4 的 poll 字段 `user_mcp.fingerprint`；Task 3 的 `GET /v1/mcp/envelopes`；enclave `POST /v1/envelope/decrypt` → `{"plaintext_b64": ...}`（`backend/enclave/routes/envelope.py:59-64`）；Task 5 的 `user_mcp_materialize` 全部函数。
- Produces:
  - env `USER_MCP_FILE`（默认 `/tmp/feedling_user_mcp_{fingerprint}.json`，命名跟 `CHECKPOINT_FILE` :219-224 惯例）——同时是 claude `--mcp-config` 目标与 VPS 通用 `user-mcp.json`
  - `call_agent(..., lane: str = "background")` 新参数（`"chat"` 才注入 MCP）
  - `{mcp}` 占位符替换语义（Task 7 spawners 依赖）：claude 模板 → `--mcp-config <USER_MCP_FILE>`（chat 且有 enabled server）否则空；codex 模板 → `-c mcp_servers={}`（**非** chat 且有 enabled server）否则空

- [ ] **Step 1: 写失败测试**（纯函数部分：`_user_mcp_mcp_value`、`_render_cli_template` 的 `{mcp}` 替换、`_apply_user_mcp` 的文件写出——网络与解密 mock）

```python
# tests/test_user_mcp_consumer.py（节选；env 前置块照抄 test_chat_resident_consumer_image.py）
def test_mcp_value_claude_chat(monkeypatch, tmp_path):
    import chat_resident_consumer as c
    monkeypatch.setattr(c, "_user_mcp_applied",
                        {"fingerprint": "sha256:x", "servers": [
                            {"name": "jira", "enabled": True,
                             "url": "https://a.example.com", "headers": {}}]})
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    tpl_claude = "claude --allowed-tools 'x' {mcp} -p {message}"
    tpl_codex = "codex exec --json {mcp} {message}"
    assert c._user_mcp_cli_value(tpl_claude, "chat") == f"--mcp-config {tmp_path}/mcp.json"
    assert c._user_mcp_cli_value(tpl_claude, "background") == ""
    assert c._user_mcp_cli_value(tpl_codex, "chat") == ""
    assert c._user_mcp_cli_value(tpl_codex, "background") == "-c mcp_servers={}"
    assert c._user_mcp_cli_value("claude -p {message}", "chat") == ""  # 无 {mcp} 占位符


def test_apply_user_mcp_materializes_files(monkeypatch, tmp_path):
    import base64, json
    import chat_resident_consumer as c
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    (tmp_path / "claude-home").mkdir()
    (tmp_path / "codex-home").mkdir()
    (tmp_path / "claude-home" / "settings.json").write_text(json.dumps(
        {"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(x:*)"]}}))
    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", lambda: {
        "fingerprint": "sha256:new",
        "servers": [{"name": "jira", "enabled": True, "config_envelope": {"id": "e1"}}]})
    monkeypatch.setattr(c, "_decrypt_envelope", lambda env: json.dumps(
        {"url": "https://a.example.com/mcp", "headers": {"X-K": "v"}}).encode())
    c._user_mcp_advertised = {"fingerprint": "sha256:new"}
    c._user_mcp_applied = {"fingerprint": None, "servers": []}
    c._maybe_apply_user_mcp()
    assert json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]["jira"]["url"] \
        == "https://a.example.com/mcp"
    settings = json.loads((tmp_path / "claude-home" / "settings.json").read_text())
    assert "mcp__jira__*" in settings["permissions"]["allow"]
    assert "[mcp_servers.jira]" in (tmp_path / "codex-home" / "config.toml").read_text()
    assert c._user_mcp_applied["fingerprint"] == "sha256:new"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_consumer.py -v`
Expected: FAIL（AttributeError：新函数/全局不存在）

- [ ] **Step 3: 实现 consumer 改动**（分五处，全部新代码如下）

(a) 常量区（`CHECKPOINT_FILE` :219-224 之后）：

```python
USER_MCP_FILE = os.environ.get(
    "USER_MCP_FILE",
    f"/tmp/feedling_user_mcp_{_fingerprint}.json",
)
```

（`_fingerprint` 是 CHECKPOINT_FILE 默认值已用的同一变量；确认其在此处已定义，否则挪到其定义之后。）

(b) 模块级状态 + poll 感知（`_chat_runtime_v2_profile` :3723 与 `_update_chat_runtime_v2_profile` :3874 的同款模式）：

```python
_user_mcp_advertised: dict = {}      # 最近一次 poll 广告的 {fingerprint}
_user_mcp_applied: dict = {"fingerprint": None, "servers": []}  # 已物化状态


def _update_user_mcp_advertised(payload) -> None:
    global _user_mcp_advertised
    if isinstance(payload, dict):
        _user_mcp_advertised = payload
```

`poll_chat()`（:3869-3870，`_update_chat_runtime_v2_profile` 调用旁）加一行：

```python
        _update_user_mcp_advertised(body.get("user_mcp"))
```

(c) 拉取 + 解密 + 物化（新函数区，放 `_fetch_from_enclave` :1188 同一区域；`_HEADERS`/`_ENCLAVE_CLIENT` 复用，认证已由 `_refresh_auth_header` :686-707 统一处理）：

```python
def _fetch_user_mcp_envelopes() -> dict:
    resp = httpx.get(f"{FEEDLING_API_URL}/v1/mcp/envelopes",
                     headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _decrypt_envelope(envelope: dict) -> bytes:
    if not FEEDLING_ENCLAVE_URL or _ENCLAVE_CLIENT is None:
        raise RuntimeError("enclave_unavailable")
    resp = _ENCLAVE_CLIENT.post(
        f"{FEEDLING_ENCLAVE_URL}/v1/envelope/decrypt",
        headers=_HEADERS,
        json={"envelope": envelope, "purpose": "mcp_server_config"},
    )
    resp.raise_for_status()
    return base64.b64decode(resp.json()["plaintext_b64"])


def _maybe_apply_user_mcp() -> None:
    """Re-materialize agent MCP config when the poll-advertised fingerprint
    moved. Failures log and retry on a later poll — never block chat."""
    global _user_mcp_applied
    target = str(_user_mcp_advertised.get("fingerprint") or "")
    if target == (_user_mcp_applied.get("fingerprint") or ""):
        return
    try:
        servers: list[dict] = []
        if target:
            payload = _fetch_user_mcp_envelopes()
            target = str(payload.get("fingerprint") or "")
            for srv in payload.get("servers") or []:
                secret = json.loads(_decrypt_envelope(srv["config_envelope"]))
                servers.append({
                    "name": srv["name"], "enabled": bool(srv.get("enabled")),
                    "url": secret["url"], "headers": secret.get("headers") or {},
                })
        _materialize_user_mcp(servers)
        _user_mcp_applied = {"fingerprint": target, "servers": servers}
        names = [s["name"] for s in servers if s["enabled"]]
        print(f"[user_mcp] applied fingerprint={target or '(empty)'} servers={names}")
    except Exception as e:
        print(f"[user_mcp] apply failed (will retry next poll): {type(e).__name__}: {e}")


def _materialize_user_mcp(servers: list[dict]) -> None:
    from tools import user_mcp_materialize as _m
    # generic file — claude --mcp-config target AND the documented VPS user-mcp.json
    Path(USER_MCP_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(USER_MCP_FILE).write_text(_m.claude_mcp_json(servers))
    os.chmod(USER_MCP_FILE, 0o600)
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if claude_dir and Path(claude_dir).is_dir():
        settings_path = Path(claude_dir) / "settings.json"
        existing = settings_path.read_text() if settings_path.exists() else None
        settings_path.write_text(
            _m.merge_settings_allow(existing, _m.claude_allow_rules(servers)))
    codex_home = os.environ.get("CODEX_HOME", "")
    if codex_home and Path(codex_home).is_dir():
        config_path = Path(codex_home) / "config.toml"
        existing = config_path.read_text() if config_path.exists() else None
        merged = _m.codex_config_merged(existing, servers)
        if merged.strip():
            config_path.write_text(merged)
        elif config_path.exists():
            config_path.unlink()
```

注意 import：consumer 从仓库根跑（`tools/...`）；照它 import 后端模块的既有方式处理 `from tools import user_mcp_materialize`（若 consumer 用同目录相对导入约定，则 `import user_mcp_materialize`——看文件头部既有 import 的写法，与 `io_cli` 的引用方式保持一致）。**新模块会被 `_runtime_repo_files()`（:738-764）经 sys.modules 自动纳入 self-update 白名单**，无需手动登记；确认方法见 Step 6。

(d) run() 主循环挂点（:7287 `_maybe_self_update(result)` 之后、:7349 `_process_messages(messages)` 之前——两个位置都要在拿到 poll result 后调用，保证「携带用户消息的那次 poll 先物化再处理消息」）：

```python
        _maybe_apply_user_mcp()
```

（放在 `_maybe_self_update` 调用的同层级、非 timed_out 分支也执行的位置；若 :7287 在 timed_out 分支内，则在 poll 返回后的公共路径调用一次即可。）

(e) `{mcp}` 按回合注入（lane 从 `call_agent` 一路透传，模式同 `{session_id}`）：

```python
def _user_mcp_cli_value(template: str, lane: str) -> str:
    if "{mcp}" not in template:
        return ""
    has = any(s.get("enabled") for s in _user_mcp_applied.get("servers") or [])
    if not has:
        return ""
    if "codex" in template.split()[0:2] or template.lstrip().startswith("codex"):
        return "-c mcp_servers={}" if lane != "chat" else ""
    return f"--mcp-config {USER_MCP_FILE}" if lane == "chat" else ""
```

- `_render_cli_template()`（:3133）签名加 `lane: str = "background"`，在 `.replace("{session_id}", ...)` 链前加：

```python
    template = template.replace("{mcp}", _user_mcp_cli_value(AGENT_CLI_CMD, lane))
```

（pre-split 替换：值只含受控字符——`--mcp-config <path>` 或 `-c mcp_servers={}`——shlex 安全；空值时占位符收敛为空白。）
- `_prepare_cli_command()`（:3159）、`call_agent_cli()`（:3328）、`call_agent()`（:3517）各加 `lane: str = "background"` 参数并透传。
- 聊天调用点改 `lane="chat"`：:6712、:6714、:6721 三处 `call_agent(...)`。proactive :6066 与其它后台 lane 不动（默认 background）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_user_mcp_consumer.py ../tests/test_chat_resident_consumer_image.py ../tests/test_chat_resident_self_update.py -v`
Expected: 新测试 PASS；consumer 既有测试不回归

- [ ] **Step 5: codex `-c mcp_servers={}` 可行性实测（本机有 codex 0.142.5）**

```bash
codex exec --skip-git-repo-check --json --strict-config -c mcp_servers={} "say hi" 2>&1 | head -5
```

Expected: 无 config 解析错误（可因无 API key 失败，但错误必须是认证/网络类，不是 `-c` 解析类）。
若解析失败：把 `_user_mcp_cli_value` 的 codex 非聊天分支改为返回 `""`，并在 spec §11 已列的「codex 后台回合软门控」限制生效——在 Task 8 的 prompt 段写明「后台回合勿调用 MCP 工具」。把实测结论记进本计划文件此步骤下方。

- [ ] **Step 6: 确认 self-update 覆盖**

Run: `cd backend && PYTHONPATH=. python -c "import sys; sys.path.insert(0,'..'); import tools.chat_resident_consumer" 2>/dev/null; echo "手动验证：_runtime_repo_files() 输出包含 tools/user_mcp_materialize.py"`
（更简单：在 Step 4 的测试里加一条 `assert "tools/user_mcp_materialize.py" in c._runtime_repo_files()`。）

- [ ] **Step 7: 改动留工作树，报告完成（不 commit）**

---

### Task 7: spawners — CLI 模板 `{mcp}` 占位符

**Files:**
- Modify: `backend/agent_runtime/spawners.py`：`_default_cli_cmd`（:254-300）、`_default_thinking_claude_cmd`（:312-330）
- Test: `tests/test_agent_runtime_spawners.py`（更新既有模板断言 + 新增占位符断言）

**Interfaces:**
- Consumes: Task 6 定义的 `{mcp}` 替换语义（claude→`--mcp-config`，codex→`-c mcp_servers={}` 清空覆盖）。
- Produces: 三个模板串中出现 `{mcp}` token，consumer `_render_cli_template` 负责替换。

- [ ] **Step 1: 写失败断言**（加进 `tests/test_agent_runtime_spawners.py`）

```python
def test_default_cli_cmds_carry_mcp_placeholder():
    from agent_runtime import spawners
    codex = spawners._default_cli_cmd("codex", "/h")
    claude = spawners._default_cli_cmd("claude", "/h")
    thinking = spawners._default_thinking_claude_cmd("/h")
    assert "{mcp}" in codex and codex.index("{mcp}") < codex.index("{message}")
    assert "{mcp}" in claude and claude.index("{mcp}") < claude.index("-p {message}")
    assert "{mcp}" in thinking
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**——三处模板插入 `{mcp} `：

codex（:289-294 的 return 串）改为：

```python
        return (
            "codex exec --skip-git-repo-check --json "
            "-c model_reasoning_effort=medium "
            "-c model_reasoning_summary=auto "
            "{mcp} "
            "--dangerously-bypass-approvals-and-sandbox {message}"
        )
```

claude（:295-300）与 thinking（:321-330）在 `--append-system-prompt-file {prompt_file}` 之后、`-p {message}` 之前插入 `{mcp} `：

```python
        f"--append-system-prompt-file {prompt_file} {{mcp}} -p {{message}}"
```

（f-string 内写 `{{mcp}}` 得到字面 `{mcp}`。）

- [ ] **Step 4: 全量跑 spawners 测试并修既有断言**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests/test_agent_runtime_spawners.py ../tests/test_agent_runtime_resident_contract.py -v`
Expected: 既有断言精确匹配模板串的（如 `test_consumer_env_uses_*` 系）按新串更新后全绿。
**兼容性说明**：旧 consumer 遇到新模板的 `{mcp}` 不认识会留下字面 token——但 consumer 与 spawners 同镜像部署（CVM 原子更新），VPS 用户的模板是自己写的没有 `{mcp}`，无跨版本窗口。

- [ ] **Step 5: 改动留工作树，报告完成（不 commit）**

---

### Task 8: prompt 与文档

**Files:**
- Modify: `backend/agent_runtime/agent_tools_prompt.md`（静态短段）
- Modify: `docs/CHANGELOG.md`（landmark 条目，格式照文件既有条目）
- Modify（**另一仓库**）: `~/Projects/**/io-onboarding/skill-resident-agent.md`（先 `ls` 确认本地克隆路径；若无克隆，把段落文本交给用户自行处理，不要猜路径）

**Steps:**

- [ ] **Step 1: agent_tools_prompt.md 追加段**（文件末尾；中英与该文件既有风格一致，若全文英文则用英文）：

```markdown
## User-configured MCP tools

The user may connect external MCP servers in app settings. When present,
their tools appear natively as `mcp__<server>__<tool>`. They are available
ONLY during interactive chat turns — never call them from background /
proactive turns. If such a tool call fails, tell the user plainly what
failed; do not fabricate results.
```

- [ ] **Step 2: skill-resident-agent.md 新段**（io-onboarding 本地克隆；描述 `USER_MCP_FILE`——默认 `/tmp/feedling_user_mcp_<fingerprint>.json`——的位置、格式（`mcpServers` JSON）、语义：consumer 保持其与 app 配置同步，「若你的 runtime 支持 MCP 请加载它；仅聊天回合使用」。**只改文件不 push**——push 即发布，须用户确认。）

- [ ] **Step 3: CHANGELOG 条目**（一段：user_mcp 功能、poll 下发、consumer 物化、endpoints 清单、spec 链接）。

- [ ] **Step 4: 改动留工作树，报告完成（不 commit / 不 push）**

---

### Task 9: 全量回归 + 手工 E2E 清单

**Steps:**

- [ ] **Step 1: 全量测试**

Run: `cd backend && PYTHONPATH=. python -m pytest ../tests -q`
Expected: 与 Task 0 基线一致（只允许基线里已有的失败）。

- [ ] **Step 2: 本地 smoke（不部署）**——`make_client` 之外再用真进程验证一次装配：

```bash
cd backend && PYTHONPATH=. python -c "
import asgi_app
routes = [r.path for r in asgi_app.app.routes]
assert '/v1/mcp/servers' in routes and '/v1/mcp/envelopes' in routes
print('mcp routes mounted OK')"
```

- [ ] **Step 3: 手工 E2E 清单**（需要用户配合部署到 test 环境后执行；写成报告交给用户）：
  1. curl 建配置（真实公共 MCP server 或自建 echo server）→ `/test` 返回工具清单。
  2. `/v1/chat/poll` 响应携带 fingerprint。
  3. 托管 claude 用户聊天回合：agent 能调 `mcp__*` 工具；proactive 回合确认不带 `--mcp-config`（查 runner 日志命令行）。
  4. 托管 codex 用户同上（按 Task 6 Step 5 的实测结论验证门控或确认软门控限制成立）。
  5. iOS 关 enabled 开关 → 下一回合工具消失。
  6. 自跑 consumer（VPS 模式，无 runtime-token 文件）：`USER_MCP_FILE` 物化并随配置更新。

- [ ] **Step 4: 汇总报告**（改动清单、测试结果、E2E 待办、已知限制），交用户决定 commit / 部署。

---

## Self-Review 记录

- **Spec 覆盖**：§2.1→Task 1/3；§2.2→Task 4/6；§2.3→Task 5/6/7；§2.4→Task 8；§3→Task 1；§4→Task 3；§5→Task 6/7；§6/§7→Task 2；§8→各任务错误码 + Task 6 重试；§9（iOS）→契约即 Task 3 端点，iOS 实施另仓不在本计划；§10→各任务测试 + Task 9；§11（后续项）→无任务，符合预期；§12→Task 8。
- **占位符扫描**：无 TBD/TODO；Task 6 Step 5 与 Task 9 Step 3.4 的 codex 门控是显式实验步骤带回退路径，非占位。
- **类型一致性**：`fingerprint_for_store`/`envelopes_payload`/`_user_mcp_cli_value`/`claude_mcp_json` 等签名在生产者与消费者任务间逐一核对一致；`{mcp}` 语义在 Task 6(e) 与 Task 7 相同表述。
