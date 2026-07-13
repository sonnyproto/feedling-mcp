# VPS Onboarding Batch 1 — io_cli onboarding 工具面 + 护栏 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 给 resident agent 一套趁手的 onboarding 工具,让它走 io_cli 就【一次走通、尽量多拿内容、基本不失败】;不碰身份写入语义。

**Architecture:** `card_policy.py`(Batch 0,纯 stdlib)新增 `sanitize_identity_card`(能修就修)。`tools/io_cli.py` 加 onboarding verbs:`identity-init`(先 sanitize 再发,`--fresh-start` 填必填项)、`onboarding-validate`、`chat-verify-loop`、`chat-greet`、`onboard`(下一步引导)、`onboard-start`(启动信号)、`doctor`(五项体检)。resident skill 把 onboarding 步骤指向 io_cli。

**Tech Stack:** Python 3.10+ stdlib(io_cli 用 urllib/argparse,card_policy 纯 stdlib)。pytest(DB 测试用 uv 包装,见 Global Constraints)。

## Global Constraints

- **两档校验(hx 定 0712):强校验只兜死 4 条 —— ①加密信封完整 ②维度值是数字 ③days 有真实锚点(fresh-start=0 永远合法)④agent_name 非字面 runtime 标签。其余模糊的(维度个数/方差/空名/稀疏)全放过。总原则:尽量多拿内容。**
- **"能修就修,别拒"**:值越界→夹 0-100、维度重名→去重、超 12→截断、坏维度→丢弃(sanitize),不拒。
- `card_policy.py` 只 import stdlib;`io_cli.py` 只 stdlib(urllib/argparse)+ 通过 sys.path 够到同仓 `backend/identity/card_policy.py`。
- io_cli cmd 模式(照现有 `cmd_memory_write`):`api_url, auth = _require_backend()` → 纯 payload builder → `_http_json(method, url, auth, payload=...)` → `_emit({"ok":True,...})` 或 `_emit({"ok":False,"http_status":s,"error":b}, 1)`。
- **DB 测试规范命令**(plain pytest 会静默 skip):
  `FEEDLING_TEST_PG="postgresql://postgres:test@127.0.0.1:55432/postgres" uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest --with pytest-asyncio --with requests python -m pytest <target> -v`
- 不碰身份写入语义、不改后端 route;启动信号走现有 `POST /v1/track/event`。

---

### Task 1: `card_policy.sanitize_identity_card`(纯函数,"能修就修")

**Files:** Modify `backend/identity/card_policy.py` · Test `tests/test_identity_card_policy.py`

**Interfaces:** Produces `sanitize_identity_card(card: dict) -> dict`

- [ ] **Step 1: 失败测试(追加)**

```python
def test_sanitize_clamps_dedups_truncates_drops():
    dirty = {"agent_name": "阿锐", "dimensions": [
        {"name": "锐利", "value": 150, "description": "x"},   # 越界 → 夹到 100
        {"name": "锐利", "value": 30, "description": "dup"},   # 重名 → 丢
        {"name": "温情", "value": -5, "description": "y"},     # 越界 → 夹到 0
        {"name": "坏", "value": "hi", "description": "z"},      # 非数字 → 丢
        "not-a-dict",                                           # 非 dict → 丢
    ]}
    out = card_policy.sanitize_identity_card(dirty)
    dims = out["dimensions"]
    assert [d["name"] for d in dims] == ["锐利", "温情"]
    assert dims[0]["value"] == 100 and dims[1]["value"] == 0
    # sanitize 后必然通过强校验(结构)
    assert card_policy.validate_dimensions_structure(dims) == (True, "")

def test_sanitize_truncates_to_max():
    many = {"agent_name": "阿锐", "dimensions": [
        {"name": f"d{i}", "value": 50, "description": "x"} for i in range(20)]}
    assert len(card_policy.sanitize_identity_card(many)["dimensions"]) == card_policy.MAX_DIMENSIONS

def test_sanitize_leaves_name_untouched():
    # 空名/runtime 名字 sanitize 不动(名字是强校验/引导层的事,不在这瞎编)
    assert card_policy.sanitize_identity_card({"agent_name": "", "dimensions": []})["agent_name"] == ""
    assert card_policy.sanitize_identity_card({"agent_name": "Claude", "dimensions": []})["agent_name"] == "Claude"
```

- [ ] **Step 2: 跑确认失败**
Run: `uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest python -m pytest tests/test_identity_card_policy.py -k sanitize -v`
Expected: FAIL — `AttributeError: ... has no attribute 'sanitize_identity_card'`

- [ ] **Step 3: 实现(追加到 card_policy.py)**

```python
def sanitize_identity_card(card: dict) -> dict:
    """Best-effort clean so the card PASSES structure validation WITHOUT losing
    usable content (contract: capture more, don't reject fuzzy issues).
    Clamp values to [0,100]; drop non-dict / non-number-valued / unnamed dims;
    drop duplicate dimension names (keep first); truncate to MAX_DIMENSIONS.
    Does NOT touch agent_name — empty is allowed and a runtime-label name is a
    STRONG check the caller handles; we never invent a name here."""
    if not isinstance(card, dict):
        return card
    out = dict(card)
    dims = card.get("dimensions")
    if isinstance(dims, list):
        cleaned: list = []
        seen: set[str] = set()
        for d in dims:
            if not isinstance(d, dict):
                continue
            name = str(d.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            v = d.get("value")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            nd = dict(d)
            nd["name"] = name
            nd["value"] = max(_VALUE_MIN, min(_VALUE_MAX, v))
            seen.add(name.lower())
            cleaned.append(nd)
            if len(cleaned) >= MAX_DIMENSIONS:
                break
        out["dimensions"] = cleaned
    return out
```

- [ ] **Step 4: 跑确认通过**
Run: `uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest python -m pytest tests/test_identity_card_policy.py -v`
Expected: PASS(新 3 条 + 原有全过)

- [ ] **Step 5: 提交**
```bash
git add backend/identity/card_policy.py tests/test_identity_card_policy.py
git commit -m "feat(identity): card_policy.sanitize_identity_card(能修就修,契约多拿内容)"
```

---

### Task 2: io_cli 接 card_policy + `identity-init` verb

**Files:** Modify `tools/io_cli.py` · Test `tests/test_io_cli_identity.py`(追加;参照该文件现有构造)

**Interfaces:** Consumes `card_policy.sanitize_identity_card`, `card_policy.validate_full_identity_card`(Task 1 + Batch 0)。Produces io_cli verb `identity-init` + pure `_identity_init_payload(...)`.

- [ ] **Step 1: 失败测试(纯 builder,先不碰网络)**

```python
def test_identity_init_payload_fresh_start_and_sanitize():
    from io_cli import _identity_init_payload
    body = _identity_init_payload(
        agent_name="阿锐", self_introduction="hi",
        dimensions=[{"name": "锐利", "value": 150, "description": "x"}],
        days_with_user=None, anchor=None, fresh_start=True)
    assert body["days_with_user"] == 0
    assert len(body["relationship_anchor_evidence"]) >= 8  # fresh-start 标准证据
    assert body["identity"]["dimensions"][0]["value"] == 100  # sanitize 夹过
```

- [ ] **Step 2: 跑确认失败**
Run: `uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest python -m pytest tests/test_io_cli_identity.py -k identity_init_payload -v`
Expected: FAIL — `ImportError: cannot import name '_identity_init_payload'`

- [ ] **Step 3: 实现**

在 `tools/io_cli.py` 顶部(其它 import 之后)加 card_policy 接入:
```python
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
try:
    from identity import card_policy as _card_policy  # single source, pure stdlib
except Exception:
    _card_policy = None
```

纯 builder + cmd + arg 注册:
```python
_FRESH_START_EVIDENCE = "user-confirmed fresh start"

def _identity_init_payload(*, agent_name, self_introduction, dimensions,
                           days_with_user, anchor, fresh_start):
    """Build the /v1/identity/init body. Sanitize the card (clamp/dedup/truncate)
    so structure is valid; fresh_start fills days=0 + standard anchor evidence."""
    card = {
        "agent_name": str(agent_name or ""),
        "self_introduction": str(self_introduction or ""),
        "dimensions": dimensions if isinstance(dimensions, list) else [],
    }
    if _card_policy is not None:
        card = _card_policy.sanitize_identity_card(card)
    if fresh_start:
        days = 0
        anchor = _FRESH_START_EVIDENCE
    else:
        days = int(days_with_user) if days_with_user is not None else None
    return {"identity": card, "days_with_user": days,
            "relationship_anchor_evidence": anchor or ""}

def cmd_identity_init(args):
    api_url, auth = _require_backend()
    dims = json.loads(args.dimensions) if args.dimensions else []
    body = _identity_init_payload(
        agent_name=args.agent_name, self_introduction=args.self_introduction,
        dimensions=dims, days_with_user=args.days_with_user,
        anchor=args.relationship_anchor_evidence, fresh_start=args.fresh_start)
    # 强校验本地预检:只在 sanitize 修不了的 4 条上提示(runtime 名字 / days 缺锚点)
    if _card_policy is not None:
        ok, err = _card_policy.validate_full_identity_card(body["identity"])
        if not ok:
            _emit({"ok": False, "error": err,
                   "hint": "非空名字不能是 runtime 标签(Claude 等);其余结构已自动修正"}, 2)
    if body["days_with_user"] is None:
        _emit({"ok": False, "error": "days_with_user_required",
               "hint": "给 --days-with-user + --relationship-anchor-evidence,或用 --fresh-start"}, 2)
    status, resp = _http_json("POST", f"{api_url}/v1/identity/init", auth, payload=body)
    if status in (200, 201):
        _emit({"ok": True, **(resp if isinstance(resp, dict) else {"result": resp})})
    _emit({"ok": False, "http_status": status, "error": resp}, 1)
```

在 `main()` 的 subparser 区注册(照现有 `mw`/`iw` 的写法):
```python
    ii = sub.add_parser("identity-init", help="Create the identity card (sanitizes + fresh-start).")
    ii.add_argument("--agent-name", default="")
    ii.add_argument("--self-introduction", default="")
    ii.add_argument("--dimensions", default="", help="JSON list of {name,value,description}")
    ii.add_argument("--days-with-user", type=int, default=None)
    ii.add_argument("--relationship-anchor-evidence", default="")
    ii.add_argument("--fresh-start", action="store_true", help="days=0 + standard anchor")
    ii.set_defaults(func=cmd_identity_init)
```

- [ ] **Step 4: 跑确认通过**(纯 builder 测试)
Run: `uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest python -m pytest tests/test_io_cli_identity.py -k identity_init_payload -v`
Expected: PASS

- [ ] **Step 5: 提交**
```bash
git add tools/io_cli.py tests/test_io_cli_identity.py
git commit -m "feat(io_cli): identity-init verb(sanitize + fresh-start,走 io_cli 基本不失败)"
```

---

### Task 3: io_cli 瘦封装 verbs — `onboarding-validate` / `chat-verify-loop` / `chat-greet`

**Files:** Modify `tools/io_cli.py` · Test `tests/test_io_cli_identity.py` 或新 `tests/test_io_cli_onboarding.py`

**Interfaces:** Consumes `_require_backend`, `_http_json`, `_emit`。

- [ ] **Step 1: 失败测试**(用现有 `client` DB fixture,或对无网时的参数校验做纯测试)——先写 `chat-greet` 空消息应报错的纯测试:
```python
def test_greet_requires_message():
    from io_cli import _greet_payload
    assert _greet_payload("") is None
    assert _greet_payload("嗨") == {"text": "嗨"}
```

- [ ] **Step 2: 跑确认失败** — `ImportError: _greet_payload`

- [ ] **Step 3: 实现**(三个 verb + `_greet_payload`;先确认 greet 用 `/v1/chat/message` 还是 `/v1/chat/response` —— READ `backend/chat/routes_asgi.py:120/144` 确认哪个是 agent 主动发的问候,用那个):
```python
def _greet_payload(message):
    message = str(message or "").strip()
    return {"text": message} if message else None   # 字段名以 routes_asgi 实际为准

def cmd_onboarding_validate(args):
    api_url, auth = _require_backend()
    status, body = _http_json("GET", f"{api_url}/v1/onboarding/validate", auth)
    _emit({"ok": status == 200, "http_status": status, **(body if isinstance(body, dict) else {})},
          0 if status == 200 else 1)

def cmd_chat_verify_loop(args):
    api_url, auth = _require_backend()
    status, body = _http_json("POST", f"{api_url}/v1/chat/verify_loop", auth, payload={}, timeout=40)
    _emit({"ok": bool(isinstance(body, dict) and body.get("passing")), "http_status": status,
           **(body if isinstance(body, dict) else {})}, 0 if status == 200 else 1)

def cmd_chat_greet(args):
    api_url, auth = _require_backend()
    payload = _greet_payload(args.message)
    if payload is None:
        _emit({"ok": False, "error": "empty_message: need --message"}, 2)
    status, body = _http_json("POST", f"{api_url}/v1/chat/message", auth, payload=payload)
    _emit({"ok": status in (200, 201), "http_status": status,
           **(body if isinstance(body, dict) else {})}, 0 if status in (200, 201) else 1)
```
main() 注册三个 parser(`onboarding-validate` 无参;`chat-verify-loop` 无参;`chat-greet` 加 `--message`)。

- [ ] **Step 4: 跑确认通过** — `pytest tests/test_io_cli_onboarding.py -k greet -v`
- [ ] **Step 5: 提交** — `feat(io_cli): onboarding-validate / chat-verify-loop / chat-greet verbs`

---

### Task 4: io_cli `onboard`(下一步引导)+ `onboard-start`(启动信号)

**Files:** Modify `tools/io_cli.py` · Test `tests/test_io_cli_onboarding.py`

**Interfaces:** 纯 `_next_onboarding_step(status: dict) -> dict`(当前步 + 下一条命令),纯可测。

- [ ] **Step 1: 失败测试**
```python
def test_next_step_from_bootstrap():
    from io_cli import _next_onboarding_step
    s0 = {"identity_written": False, "chat_loop_verified": False, "agent_messages_count": 0}
    assert _next_onboarding_step(s0)["next_cmd"].startswith("io_cli identity-init")
    s1 = {"identity_written": True, "chat_loop_verified": False, "agent_messages_count": 0}
    assert "verify" in _next_onboarding_step(s1)["next_cmd"]
    s2 = {"identity_written": True, "chat_loop_verified": True, "agent_messages_count": 0}
    assert "greet" in _next_onboarding_step(s2)["next_cmd"]
    s3 = {"identity_written": True, "chat_loop_verified": True, "agent_messages_count": 1}
    assert _next_onboarding_step(s3)["done"] is True
```

- [ ] **Step 2: 跑确认失败** — `ImportError: _next_onboarding_step`

- [ ] **Step 3: 实现**
```python
def _next_onboarding_step(status):
    s = status if isinstance(status, dict) else {}
    if not s.get("identity_written"):
        return {"step": "identity", "done": False,
                "next_cmd": "io_cli identity-init --agent-name <name> --dimensions <json> --fresh-start"}
    if not s.get("chat_loop_verified"):
        return {"step": "live_loop", "done": False, "next_cmd": "io_cli chat-verify-loop"}
    if int(s.get("agent_messages_count") or 0) < 1:
        return {"step": "greet", "done": False, "next_cmd": "io_cli chat-greet --message <greeting>"}
    return {"step": "complete", "done": True, "next_cmd": ""}

def cmd_onboard(args):
    api_url, auth = _require_backend()
    status, body = _http_json("GET", f"{api_url}/v1/bootstrap/status", auth)
    nxt = _next_onboarding_step(body if isinstance(body, dict) else {})
    _emit({"ok": status == 200, "http_status": status, "status": body, **nxt},
          0 if status == 200 else 1)

def cmd_onboard_start(args):
    api_url, auth = _require_backend()
    status, body = _http_json("POST", f"{api_url}/v1/track/event", auth,
                              payload={"event": "resident_onboarding_started"})
    _emit({"ok": status in (200, 201), "http_status": status}, 0 if status in (200, 201) else 1)
```
main() 注册 `onboard`(无参)+ `onboard-start`(无参)。

- [ ] **Step 4: 跑确认通过** — `pytest tests/test_io_cli_onboarding.py -k next_step -v`
- [ ] **Step 5: 提交** — `feat(io_cli): onboard 下一步引导 + onboard-start 启动信号`

---

### Task 5: io_cli `doctor`(五项体检)

**Files:** Modify `tools/io_cli.py` · Test `tests/test_io_cli_onboarding.py`

**Interfaces:** 纯 `_doctor_summary(checks: dict) -> dict`(把各检查结果汇成 ok/failed 列表),纯可测;`cmd_doctor` 跑真实检查。

- [ ] **Step 1: 失败测试**
```python
def test_doctor_summary_lists_failures():
    from io_cli import _doctor_summary
    out = _doctor_summary({"api": True, "enclave": False, "identity": True,
                            "memory": True, "chat_write": False})
    assert out["ok"] is False
    assert set(out["failed"]) == {"enclave", "chat_write"}
```

- [ ] **Step 2: 跑确认失败** — `ImportError: _doctor_summary`

- [ ] **Step 3: 实现**
```python
def _doctor_summary(checks):
    failed = [k for k, v in (checks or {}).items() if not v]
    return {"ok": not failed, "checks": checks, "failed": failed}

def cmd_doctor(args):
    auth = _auth_headers()
    api_url = _env("FEEDLING_API_URL")
    enclave_url = _env("FEEDLING_ENCLAVE_URL")
    def _ok(method, url, insecure=False):
        try:
            s, _ = _http_json(method, url, auth, insecure=insecure, timeout=10)
            return 200 <= s < 300
        except Exception:
            return False
    checks = {
        "api": bool(api_url) and _ok("GET", f"{api_url.rstrip('/')}/v1/users/whoami"),
        "enclave": bool(enclave_url) and _ok("GET", f"{enclave_url.rstrip('/')}/v1/identity/get", insecure=True),
        "identity": bool(api_url) and _ok("GET", f"{api_url.rstrip('/')}/v1/identity/get"),
        "memory": bool(api_url) and _ok("GET", f"{api_url.rstrip('/')}/v1/memory/index"),
        "chat_write": bool(api_url) and _ok("GET", f"{api_url.rstrip('/')}/v1/chat/history?limit=1"),
    }
    out = _doctor_summary(checks)
    _emit(out, 0 if out["ok"] else 1)
```
(注:`identity`/`chat_write` 用只读探针代理"能不能读/写通路";别真发消息。若 `/v1/identity/get` 在没身份时返 404,把 identity 检查视为"通路 OK"—— READ 端点行为后决定,别把"还没建身份"当失败。)
main() 注册 `doctor`(无参)。

- [ ] **Step 4: 跑确认通过** — `pytest tests/test_io_cli_onboarding.py -k doctor -v`
- [ ] **Step 5: 提交** — `feat(io_cli): doctor 五项体检(环境问题早暴露)`

---

### Task 6: resident skill 指向 io_cli

**Files:** Modify `skill-resident-agent.md`(在 **io-onboarding** worktree `/Users/hx/Projects/io/io-onboarding-vps-unify`,分支 `codex/vps-onboarding-flow-unify`)

**无测试**(文档)。

- [ ] **Step 1:** READ 当前 `skill-resident-agent.md` 的 onboarding 步骤段。
- [ ] **Step 2:** 把 onboarding 各步的调用改成明确的 io_cli 命令(CLI-runtime 规范路径):连上先 `io_cli doctor` → `io_cli onboard-start` → `io_cli onboard`(看下一步)→ `io_cli identity-init [--fresh-start]` → `io_cli onboarding-validate` → `io_cli chat-verify-loop` → `io_cli chat-greet`。`feedling_*` 工具名 / 裸 HTTP 仅作"其他 runtime 等价物"附注。强调:走 io_cli,结构问题自动修、给不出锚点用 `--fresh-start`,不必自己拼 HTTP。
- [ ] **Step 3:** 提交(在 io-onboarding worktree):`docs(skill): resident onboarding 步骤统一指向 io_cli`。

---

## Self-Review
- **Spec 覆盖**:sanitize(能修就修)=Task1;identity-init(sanitize+fresh-start,走 io_cli 不失败)=Task2;瘦封装=Task3;onboard/start=Task4;doctor=Task5;skill 指向=Task6。两档原则落在 Task1(sanitize 修模糊)+ Task2(强校验只提示 4 条修不了的)。
- **无占位符**:纯函数(sanitize/payload/next-step/doctor-summary)给了完整代码;io_cli cmd 按现有 `cmd_memory_write` 模式,给了完整实现。
- **执行者需先 READ 确认的点**(已在任务内标注,非占位符):① greet 用 `/v1/chat/message` vs `/v1/chat/response`(Task3);② `/v1/identity/get` 无身份时的状态码,别把"未建身份"当 doctor 失败(Task5);③ `test_io_cli_identity.py` 现有 fixture/import 风格(Task2/3)。
- **类型一致**:所有纯函数返回 dict / `(bool,str)`;cmd 统一 `_emit`。
