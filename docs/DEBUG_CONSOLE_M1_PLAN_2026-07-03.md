# DebugConsole M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the "send a message" main chain (and the LLM call inside it) fully visible in a per-user iOS DebugConsole, so a tester can see the model's input/output and pinpoint which step stalled/errored.

**Architecture:** Upgrade `debug_trace.py` into the single event backbone (adds `explain`/`content_excerpt`/`dur_ms`). The live path is the resident consumer (`tools/chat_resident_consumer.py`) driving `claude`/`codex` CLI; it is HTTP-only, so it reports events via a new `POST /v1/debug/trace/event`. Backend traces the boundaries it owns (message-in, reply-out, capture). iOS fetches the flat event list and does grouping/filtering client-side.

**Tech Stack:** Python 3 / Flask / pytest (backend + consumer, repo `feedling-mcp`, branch `feat/debug-console`); Swift / SwiftUI (repo `feedling-mcp-ios`, branch `feat/debug-console`).

## Global Constraints

- **Runtime safety (hx 硬要求 "绝不能影响业务流程"):** every emit path is best-effort — wrapped in `try/except`, never re-raises, never on the business return path; consumer emit is fire-and-forget with a short httpx timeout and must never block/slow a CLI turn or reply write. When the gate is off, `trace_event` is a full no-op (no blob read/write).
- **Privacy / verbose:** `content_excerpt` is filled only when `verbose_enabled(store)` is true; env `FEEDLING_DEBUG_VERBOSE=0` force-strips it. Excerpt caps: **≤ 4096 bytes per event, ≈ 1024 bytes per field**, truncation marked with `…(truncated)`. Only snapshots/excerpts — never full prompt/history/tool-output.
- **Ring size:** verbose mode `_MAX_EVENTS = 200`; non-verbose stays `500`.
- **Grouping/filtering:** client-side. Server returns a flat event list; `GET /v1/debug/trace` supports only `limit` + single `subsystem`.
- **trace_id:** the user message id threads the whole turn. On reply, `trace_id = reply_to_message_id or msg["id"]`.
- **Modules (11, default all selected in UI):** `route, context, agent, memory, worldbook, genesis, identity, proactive, perception, push, account`. M1 emits `route`, `context`, `agent`, `memory`(capture) only; the rest render as empty chips.
- **Two repos, commit in each independently.** Backend/consumer tasks commit in `feedling-mcp`; iOS tasks commit in `feedling-mcp-ios`.

## Execution Environment (READ FIRST — overrides any path/command in task bodies)

- **Backend/consumer repo:** `/Users/hx/Projects/io/feedling-mcp`, branch `feat/debug-console`.
- **iOS repo:** `/Users/hx/Projects/io/feedling-mcp-ios`, branch `feat/debug-console`.
- **Python for ALL backend/consumer commands:** `~/feedling-venv/bin/python` (has pytest 9.1.1 + psycopg + flask + pyflakes + consumer deps). The dev Postgres is already running at `127.0.0.1:55432`; `tests/conftest.py` auto-provisions a throwaway DB per session — do not start one.
- **Tests live in repo-root `tests/`** — NOT `backend/tests/`. Run a file with `~/feedling-venv/bin/python -m pytest tests/<file>.py -v`.
- **Model every NEW backend test on `tests/test_chat_route_debug_trace.py`** (real `client` fixture via `/v1/users/register` + real DB, per `tests/conftest.py`). The illustrative fake-store snippets in task bodies below are a starting point; conform to that harness — each test file must begin with `import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))` before importing backend modules.
- **Lint after backend changes:** `~/feedling-venv/bin/python -m pyflakes backend/<changed-package>` must be clean.
- **Consumer syntax check:** `~/feedling-venv/bin/python -m pyflakes tools/chat_resident_consumer.py` (do not import it — heavy env deps; there is also `tests/test_chat_resident_consumer.py` to model consumer tests on).
- **iOS builds/tests:** `xcodebuild` (Xcode 26.2 present), scheme `FeedlingTest`. Build: `xcodebuild -scheme FeedlingTest -destination 'generic/platform=iOS' build`. Unit test: `xcodebuild test -scheme FeedlingTest -destination 'platform=iOS Simulator,name=iPhone 15' -only-testing:FeedlingTestTests/<Suite>`.
- **Task numbering note:** plan Tasks 9 and 10 are executed as ONE task (the view in Task 9 does not compile until Task 10's `TurnCard`/`EventRow` exist). Dispatch them together; single commit at the end.

---

## File Structure

**feedling-mcp (branch `feat/debug-console`):**
- Modify `backend/debug_trace.py` — add `explain`/`content_excerpt`/`dur_ms` params, `verbose_enabled()`, `_safe_content_excerpt()`, verbose ring cap.
- Modify `backend/diagnostics/routes.py` — add `POST /v1/debug/trace/event`; add `verbose` to `GET /v1/debug/trace` body.
- Modify `backend/chat/routes.py` — trace_id glue on response + richer `route.chat.message` / `route.chat.response` events.
- Modify `tools/chat_resident_consumer.py` — `_emit_debug_trace()` helper, refactor `_log_cli_turn_timing()` to return a metrics dict, emit `agent.model.call.*` + `context.build` around `call_agent_cli`.
- Test `tests/test_debug_trace.py` (new), `tests/test_debug_trace_event_route.py` (new). *(repo-root `tests/`)*

**feedling-mcp-ios (branch `feat/debug-console`):**
- Modify `App/FeedlingTest/API/FeedlingAPI.swift` — extend `FlowTraceEvent` (+`explain`, `contentExcerptJSON`, `durMs`), parse them, return `verbose`.
- Create `App/FeedlingTest/App/DebugConsole/TurnGrouping.swift` — pure grouping/stall logic (unit-testable).
- Create `App/FeedlingTest/App/DebugConsole/DebugConsoleView.swift` — the console UI.
- Modify `App/FeedlingTest/App/DebugTool.swift` — replace `FlowTracePanel` entry with `DebugConsoleView`.
- Test `App/FeedlingTestTests/TurnGroupingTests.swift` (new).

---

## Task 1: Extend `debug_trace.trace_event` (backbone)

**Files:**
- Modify: `backend/debug_trace.py`
- Test: `tests/test_debug_trace.py`

**Interfaces:**
- Produces: `trace_event(store, *, subsystem, type, summary="", explain="", detail=None, content_excerpt=None, actor="backend", status="ok", trace_id="", turn_id="", job_id="", dur_ms=None)`; `verbose_enabled(store) -> bool`; `_safe_content_excerpt(d) -> dict`. Existing `read_trace`/`is_enabled`/`set_enabled`/`clear_trace` unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_debug_trace.py
import importlib
import debug_trace


class FakeStore:
    def __init__(self, uid="u1"):
        self.user_id = uid


def _reset(monkeypatch):
    """In-memory blob store + force gate ON."""
    blobs = {}
    monkeypatch.setattr(debug_trace.db, "get_blob", lambda uid, k: blobs.get((uid, k)))
    monkeypatch.setattr(debug_trace.db, "set_blob", lambda uid, k, v: blobs.__setitem__((uid, k), v))
    monkeypatch.setattr(debug_trace, "_hard_disabled", lambda: False)
    debug_trace._flag_cache.clear()
    return blobs


def test_verbose_off_strips_content_excerpt(monkeypatch):
    _reset(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.setenv("FEEDLING_DEBUG_VERBOSE", "0")  # force strip
    debug_trace.trace_event(store, subsystem="agent", type="agent.model.call.done",
                            explain="模型返回", content_excerpt={"reply": "hello"}, dur_ms=12.0)
    ev = debug_trace.read_trace(store, limit=10)[0]
    assert ev["explain"] == "模型返回"
    assert ev["dur_ms"] == 12.0
    assert ev.get("content_excerpt") in (None, {}, )  # stripped when verbose off


def test_content_excerpt_field_truncation(monkeypatch):
    _reset(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.delenv("FEEDLING_DEBUG_VERBOSE", raising=False)  # verbose defaults ON with gate
    big = "x" * 5000
    debug_trace.trace_event(store, subsystem="agent", type="t",
                            content_excerpt={"prompt": big})
    ev = debug_trace.read_trace(store, limit=10)[0]
    assert len(ev["content_excerpt"]["prompt"]) <= 1024 + len("…(truncated)")
    assert ev["content_excerpt"]["prompt"].endswith("…(truncated)")


def test_verbose_ring_cap(monkeypatch):
    _reset(monkeypatch)
    store = FakeStore()
    debug_trace.set_enabled(store, True)
    monkeypatch.delenv("FEEDLING_DEBUG_VERBOSE", raising=False)
    for i in range(260):
        debug_trace.trace_event(store, subsystem="route", type=f"t{i}")
    assert len(debug_trace.read_trace(store, limit=1000)) == 200  # verbose cap
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/feedling-venv/bin/python -m pytest tests/test_debug_trace.py -v`
Expected: FAIL — `trace_event() got an unexpected keyword argument 'explain'`.

- [ ] **Step 3: Implement in `backend/debug_trace.py`**

Add near the top constants:

```python
_MAX_EVENTS = 500
_MAX_EVENTS_VERBOSE = 200
_EXCERPT_FIELD_MAX = 1024
_EXCERPT_EVENT_MAX = 4096
_TRUNC_MARK = "…(truncated)"
```

Add after `set_enabled`:

```python
def verbose_enabled(store) -> bool:
    """Whether to record plaintext content_excerpt. Defaults to is_enabled;
    FEEDLING_DEBUG_VERBOSE=0 force-strips (prod safety valve)."""
    if os.environ.get("FEEDLING_DEBUG_VERBOSE", "").strip().lower() in ("0", "false", "off", "no"):
        return False
    return is_enabled(store)


def _safe_content_excerpt(d: dict[str, Any] | None) -> dict[str, Any]:
    """Metadata-free plaintext excerpt: per-field and per-event byte caps,
    truncation marked. Only str/number fields; drops anything exotic."""
    if not isinstance(d, dict):
        return {}
    out: dict[str, Any] = {}
    budget = _EXCERPT_EVENT_MAX
    for k, v in list(d.items())[:20]:
        if budget <= 0:
            break
        key = str(k)[:40]
        s = v if isinstance(v, str) else str(v)
        field_cap = min(_EXCERPT_FIELD_MAX, budget)
        if len(s.encode("utf-8")) > field_cap:
            s = s.encode("utf-8")[:field_cap].decode("utf-8", "ignore") + _TRUNC_MARK
        out[key] = s
        budget -= len(s.encode("utf-8"))
    return out
```

Replace the `trace_event` signature + event dict + cap logic:

```python
def trace_event(
    store,
    *,
    subsystem: str,
    type: str,
    summary: str = "",
    explain: str = "",
    detail: dict[str, Any] | None = None,
    content_excerpt: dict[str, Any] | None = None,
    actor: str = "backend",
    status: str = "ok",
    trace_id: str = "",
    turn_id: str = "",
    job_id: str = "",
    dur_ms: float | None = None,
) -> None:
    try:
        if not is_enabled(store):
            return
        uid = getattr(store, "user_id", "") or ""
        if not uid:
            return
        now = time.time()
        verbose = verbose_enabled(store)
        event = {
            "ts": now,
            "subsystem": str(subsystem or "")[:40],
            "type": str(type or "")[:80],
            "actor": str(actor or "backend")[:40],
            "status": str(status or "ok")[:20],
            "summary": str(summary or "")[:300],
            "explain": str(explain or "")[:600],
            "trace_id": str(trace_id or "")[:120],
            "turn_id": str(turn_id or "")[:120],
            "job_id": str(job_id or "")[:120],
            "detail": _safe_detail(detail),
        }
        if dur_ms is not None:
            try:
                event["dur_ms"] = round(float(dur_ms), 1)
            except (TypeError, ValueError):
                pass
        if verbose and content_excerpt:
            event["content_excerpt"] = _safe_content_excerpt(content_excerpt)
        buf = db.get_blob(uid, DEBUG_TRACE_BLOB)
        events = buf.get("events") if isinstance(buf, dict) and isinstance(buf.get("events"), list) else []
        events.append(event)
        cutoff = now - _TTL_SEC
        cap = _MAX_EVENTS_VERBOSE if verbose else _MAX_EVENTS
        events = [e for e in events if float(e.get("ts") or 0) >= cutoff][-cap:]
        db.set_blob(uid, DEBUG_TRACE_BLOB, {"v": 1, "events": events})
    except Exception:
        pass  # observability must never break the actual flow
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/feedling-venv/bin/python -m pytest tests/test_debug_trace.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hx/Projects/io/feedling-mcp
git add backend/debug_trace.py tests/test_debug_trace.py
git commit -m "feat(debug_trace): explain/content_excerpt/dur_ms + verbose gate + caps

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Emit endpoint `POST /v1/debug/trace/event` + `verbose` in GET

**Files:**
- Modify: `backend/diagnostics/routes.py` (after the existing `debug_trace_clear` at line ~148)
- Test: `tests/test_debug_trace_event_route.py`

**Interfaces:**
- Consumes: `debug_trace.trace_event` (Task 1), `debug_trace.verbose_enabled`.
- Produces: route `POST /v1/debug/trace/event` returning `{"status":"ok"}`; `GET /v1/debug/trace` body gains `"verbose": <bool>`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_debug_trace_event_route.py
import json
import pytest
import app as app_module
import debug_trace


@pytest.fixture
def client(monkeypatch):
    blobs = {}
    monkeypatch.setattr(debug_trace.db, "get_blob", lambda uid, k: blobs.get((uid, k)))
    monkeypatch.setattr(debug_trace.db, "set_blob", lambda uid, k, v: blobs.__setitem__((uid, k), v))
    monkeypatch.setattr(debug_trace, "_hard_disabled", lambda: False)
    debug_trace._flag_cache.clear()

    class S:  # minimal store stub
        user_id = "u1"
    from accounts import auth
    monkeypatch.setattr(auth, "require_user", lambda: S())
    debug_trace.set_enabled(S(), True)
    return app_module.app.test_client()


def test_emit_event_records(client):
    r = client.post("/v1/debug/trace/event", json={
        "event": {"subsystem": "agent", "type": "agent.model.call.done",
                  "explain": "模型返回", "dur_ms": 2300}})
    assert r.status_code == 200
    g = client.get("/v1/debug/trace?limit=10")
    body = g.get_json()
    assert "verbose" in body
    assert any(e["type"] == "agent.model.call.done" for e in body["events"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/feedling-venv/bin/python -m pytest tests/test_debug_trace_event_route.py -v`
Expected: FAIL — 404 on `/v1/debug/trace/event` (route not registered).

- [ ] **Step 3: Implement in `backend/diagnostics/routes.py`**

In `debug_trace_read`, add `verbose` to the response:

```python
    return jsonify({
        "enabled": debug_trace.is_enabled(store),
        "deploy_enabled": debug_trace._deploy_enabled(),
        "verbose": debug_trace.verbose_enabled(store),
        "events": debug_trace.read_trace(store, limit=limit, subsystem=subsystem),
    }), 200
```

Append the new route:

```python
@bp.route("/v1/debug/trace/event", methods=["POST"])
def debug_trace_emit():
    """A resident consumer (HTTP-only, no DB) reports one flow event. Auth via
    the same per-user key; recording is gated + best-effort. Field-picking keeps
    a careless caller from injecting arbitrary keys."""
    from accounts.auth import require_user
    import debug_trace

    store = require_user()
    payload = request.get_json(silent=True) or {}
    ev = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    dur = ev.get("dur_ms")
    try:
        dur = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur = None
    debug_trace.trace_event(
        store,
        subsystem=str(ev.get("subsystem") or ""),
        type=str(ev.get("type") or ""),
        summary=str(ev.get("summary") or ""),
        explain=str(ev.get("explain") or ""),
        detail=ev.get("detail") if isinstance(ev.get("detail"), dict) else None,
        content_excerpt=ev.get("content_excerpt") if isinstance(ev.get("content_excerpt"), dict) else None,
        actor=str(ev.get("actor") or "vps_resident"),
        status=str(ev.get("status") or "ok"),
        trace_id=str(ev.get("trace_id") or ""),
        turn_id=str(ev.get("turn_id") or ""),
        dur_ms=dur,
    )
    return jsonify({"status": "ok"}), 200
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/feedling-venv/bin/python -m pytest tests/test_debug_trace_event_route.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/diagnostics/routes.py tests/test_debug_trace_event_route.py
git commit -m "feat(debug): POST /v1/debug/trace/event emit endpoint + verbose flag in GET

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: trace_id glue + backend boundary events (route module)

**Files:**
- Modify: `backend/chat/routes.py` (message handler ~line 68-77; response trace ~line 418; gated trace ~line 286)

**Interfaces:**
- Consumes: `debug_trace.trace_event` (Task 1). `reply_to_message_id` is computed at `chat/routes.py:375`; the response `msg` has `msg["id"]`.
- Produces: no new symbols; enriches existing `route.chat.message` / `route.chat.response` / gated events with `trace_id` + `explain` + `content_excerpt`.

> **Note:** there is no unit test harness for these encrypted routes; this task is verified by the M1 e2e (Task 9). Keep each change inside the existing `debug_trace.trace_event(...)` call — do not add logic to the return path.

- [ ] **Step 1: Enrich `route.chat.message`** (existing call ~line 73). Replace its args with:

```python
    debug_trace.trace_event(
        store,
        subsystem="route",
        type="chat.message",
        actor="ios",
        trace_id=msg["id"],
        turn_id=msg["id"],
        summary=f"user message stored id={msg['id']}",
        explain="收到用户消息，已入库并唤醒 resident consumer",
        detail={"content_type": content_type, "msg_id": msg["id"]},
        content_excerpt={"user_message": _plaintext_for_trace(payload, envelope)} if content_type == "text" else None,
    )
```

Add a tiny local helper near the top of the module (user text is only available when the client also sent a plaintext preview; otherwise leave empty — never decrypt):

```python
def _plaintext_for_trace(payload: dict, envelope: dict) -> str:
    """Best-effort plaintext for the debug excerpt ONLY. The server never
    decrypts; use a client-provided preview if present, else empty."""
    return str(payload.get("debug_preview") or envelope.get("synthetic_marker") or "")[:1000]
```

- [ ] **Step 2: Add trace_id to the response event** (existing call ~line 418):

```python
    debug_trace.trace_event(
        store,
        subsystem="route",
        type="chat.response",
        actor="agent",
        trace_id=(reply_to_message_id or msg["id"]),
        turn_id=(reply_to_message_id or msg["id"]),
        summary=f"agent reply stored id={msg['id']} source={source}",
        explain=f"agent 回复已入库（source={source}）",
        detail={"source": source, "content_type": content_type, "msg_id": msg["id"]},
    )
```

- [ ] **Step 3: Add trace_id to the gated event** (existing call ~line 286): add `trace_id=(reply_to_message_id or ""), turn_id=(reply_to_message_id or ""),` to that `trace_event(...)`. (`reply_to_message_id` is parsed later at 375; hoist its 4-line parse above the gate call so it is in scope, or recompute inline `str(payload.get("reply_to_message_id") or payload.get("reply_to_id") or payload.get("in_reply_to") or "")`.)

- [ ] **Step 4: Verify import compiles**

Run: `~/feedling-venv/bin/python -c "import sys; sys.path.insert(0,'backend'); import chat.routes"`
Expected: no error.

- [ ] **Step 5: Commit**

```bash
git add backend/chat/routes.py
git commit -m "feat(debug): trace_id glue + richer route.chat.* events (turn grouping)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `memory.capture.*` backend events

**Files:**
- Modify: the capture entrypoint. **Locate it first:** `cd backend && grep -rn "def .*capture" memory/service.py memory/actions.py` and find where a capture job runs/queues for a turn.
- Test: none (covered by e2e Task 9).

**Interfaces:**
- Consumes: `debug_trace.trace_event`. The capture entrypoint has access to `store` and the resulting card(s).

- [ ] **Step 1: Emit at the capture boundary.** At the start of the capture run, emit start; at success/failure emit done/error. Insert (adapt the variable names to the located function):

```python
import debug_trace
debug_trace.trace_event(store, subsystem="memory", type="memory.capture.start",
                        actor="backend", trace_id=trace_id, summary="capture started")
# ... existing capture logic produces `cards` (list) ...
debug_trace.trace_event(
    store, subsystem="memory", type="memory.capture.done", actor="backend",
    trace_id=trace_id, dur_ms=(time.monotonic() - _t0) * 1000,
    summary=f"captured {len(cards)} card(s)",
    explain=(f"本轮抓取到 {len(cards)} 条新记忆" if cards else "本轮没有可抓取的新记忆（合法）"),
    content_excerpt={"cards": " | ".join(c.get("title", "") for c in cards)[:1000]} if cards else None,
)
```

Wrap the capture body so a failure emits `memory.capture.error` with the exception string, then re-raises only if the original code did. `trace_id` here = the user message id that triggered capture (thread it from the job payload; if unavailable, pass `""`).

- [ ] **Step 2: Verify import compiles**

Run: `~/feedling-venv/bin/python -c "import sys; sys.path.insert(0,'backend'); import memory.service, memory.actions"`
Expected: no error.

- [ ] **Step 3: Commit**

```bash
git add backend/memory/service.py backend/memory/actions.py
git commit -m "feat(debug): memory.capture.start/done/error events

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Consumer emit helper + timing refactor

**Files:**
- Modify: `tools/chat_resident_consumer.py` (add helper near `_HEADERS` at line ~409; refactor `_log_cli_turn_timing` at line ~2874)

**Interfaces:**
- Consumes: `_HEADERS`, `FEEDLING_API_URL`, `httpx` (all already in module).
- Produces: `_emit_debug_trace(subsystem, type, *, status="ok", summary="", explain="", detail=None, content_excerpt=None, trace_id="", dur_ms=None) -> None` (fire-and-forget); `_cli_turn_metrics(cmd, result, wall_ms) -> dict` (returns `{driver, rc, wall_ms, agent_ms, api_ms, num_turns, steps, input_tokens, output_tokens, out_chars}`); `_log_cli_turn_timing` keeps logging by calling `_cli_turn_metrics`.

- [ ] **Step 1: Add the emit helper** after `_HEADERS`:

```python
def _emit_debug_trace(subsystem: str, type: str, *, status: str = "ok",
                      summary: str = "", explain: str = "", detail: dict | None = None,
                      content_excerpt: dict | None = None, trace_id: str = "",
                      dur_ms: float | None = None) -> None:
    """Fire-and-forget flow-trace emit. Best-effort: short timeout, never raises,
    never blocks a turn. The backend gates + drops it if debug is off."""
    try:
        httpx.post(
            f"{FEEDLING_API_URL}/v1/debug/trace/event",
            json={"event": {
                "subsystem": subsystem, "type": type, "status": status,
                "summary": summary, "explain": explain, "detail": detail or {},
                "content_excerpt": content_excerpt or {}, "trace_id": trace_id,
                "turn_id": trace_id, "actor": "vps_resident", "dur_ms": dur_ms,
            }},
            headers=_HEADERS, timeout=3,
        )
    except Exception:
        pass  # observability must never affect the turn
```

- [ ] **Step 2: Refactor `_log_cli_turn_timing` to expose metrics.** Extract the metric computation into `_cli_turn_metrics` returning a dict, and make `_log_cli_turn_timing` call it then log (preserving the exact existing log lines):

```python
def _cli_turn_metrics(cmd: list[str], result: "subprocess.CompletedProcess", wall_ms: int) -> dict:
    """Driver-aware metrics for one CLI turn. Never raises."""
    m = {"driver": "codex" if _is_codex_cmd(cmd) else "claude", "rc": result.returncode,
         "wall_ms": wall_ms, "agent_ms": None, "api_ms": None, "num_turns": None,
         "steps": None, "input_tokens": None, "output_tokens": None,
         "out_chars": len(result.stdout or "")}
    try:
        if m["driver"] == "codex":
            m.update(_codex_turn_metrics(result.stdout or ""))
        else:
            for obj in _json_objects_from_cli_output(result.stdout or ""):
                if isinstance(obj, dict) and obj.get("type") == "result":
                    m["agent_ms"] = obj.get("duration_ms")
                    m["api_ms"] = obj.get("duration_api_ms")
                    m["num_turns"] = obj.get("num_turns")
                    break
    except Exception:
        pass
    return m
```

Then in `_log_cli_turn_timing`, replace the inline computations with `m = _cli_turn_metrics(cmd, result, wall_ms)` and keep the two existing `log.info(...)` lines reading from `m`.

- [ ] **Step 3: Verify import compiles**

Run: `cd /Users/hx/Projects/io/feedling-mcp && ~/feedling-venv/bin/python -m pyflakes tools/chat_resident_consumer.py`
Expected: no error (syntax check; the module has heavy env deps so avoid importing it directly).

- [ ] **Step 4: Commit**

```bash
git add tools/chat_resident_consumer.py
git commit -m "feat(consumer): _emit_debug_trace helper + _cli_turn_metrics refactor

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Consumer `agent.model.call.*` + `context.build` events

**Files:**
- Modify: `tools/chat_resident_consumer.py` — `call_agent_cli` (line ~2927) and where the user message + trace_id are in scope.

**Interfaces:**
- Consumes: `_emit_debug_trace`, `_cli_turn_metrics` (Task 5). `call_agent_cli(message, image_paths, raw_text)` runs the subprocess; the user message id (poll id) is the `trace_id`.

- [ ] **Step 1: Thread `trace_id` into `call_agent_cli`.** Add an optional param `trace_id: str = ""` to `call_agent_cli` and pass the poll message id from its caller (locate with `grep -n "call_agent_cli(" tools/chat_resident_consumer.py`; the caller has the message row).

- [ ] **Step 2: Emit start/done/error around the subprocess** inside `call_agent_cli`:

```python
    _turn_t0 = time.monotonic()
    _emit_debug_trace("agent", "agent.model.call.start", trace_id=trace_id,
                      summary="cli turn start",
                      explain="模型调用发起（" + ("codex" if _is_codex_cmd(cmd) else "claude") + "）",
                      content_excerpt={"prompt_head": (message or "")[:1000]})
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        _emit_debug_trace("agent", "agent.model.call.error", status="error", trace_id=trace_id,
                          dur_ms=(time.monotonic() - _turn_t0) * 1000,
                          summary="cli turn timeout", explain="模型调用超时（120s 上限）— 卡在模型这一步")
        log.warning(
            "[turn-timing] driver=%s rc=timeout wall_ms=%d (hit 120s subprocess cap)",
            "codex" if _is_codex_cmd(cmd) else "claude",
            int((time.monotonic() - _turn_t0) * 1000),
        )
        raise
    _wall_ms = int((time.monotonic() - _turn_t0) * 1000)
    _log_cli_turn_timing(cmd, result, _wall_ms)
    _m = _cli_turn_metrics(cmd, result, _wall_ms)
    _emit_debug_trace(
        "agent", "agent.model.call.done" if result.returncode == 0 else "agent.model.call.error",
        status="ok" if result.returncode == 0 else "error", trace_id=trace_id, dur_ms=_wall_ms,
        summary=f"cli turn rc={result.returncode} {_m['driver']}",
        explain=(f"模型返回（{_m['driver']}，{_wall_ms}ms" +
                 (f"，{_m['num_turns']} 轮" if _m.get('num_turns') else "") + "）"
                 if result.returncode == 0 else f"模型调用失败 rc={result.returncode}"),
        detail={k: _m[k] for k in ("driver", "rc", "agent_ms", "api_ms", "num_turns",
                                   "steps", "input_tokens", "output_tokens")},
        content_excerpt={"reply_head": (result.stdout or "")[:1000],
                         "stderr_head": (result.stderr or "")[:500]},
    )
```

- [ ] **Step 3: Emit `context.build`** where screen/history is attached (locate `_screen_context_for_message` call site with `grep -n "_screen_context_for_message\|_should_attach_screen_context" tools/chat_resident_consumer.py`). After context is assembled for the turn:

```python
    _emit_debug_trace("context", "context.build", trace_id=trace_id,
                      summary="context assembled",
                      explain=("本轮附加了屏幕上下文" if _screen_attached else "本轮未附加屏幕上下文"),
                      detail={"screen_attached": bool(_screen_attached)})
```

(Use whatever boolean the surrounding code already has for "screen attached"; if none, derive from the returned frames list length.)

- [ ] **Step 4: Verify syntax**

Run: `~/feedling-venv/bin/python -m pyflakes tools/chat_resident_consumer.py`
Expected: no error.

- [ ] **Step 5: Commit**

```bash
git add tools/chat_resident_consumer.py
git commit -m "feat(consumer): agent.model.call.* + context.build flow events (LLM in/out + stall)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: iOS — extend `FlowTraceEvent` + parse `explain`/`contentExcerpt`/`durMs`/`verbose`

**Files:**
- Modify: `App/FeedlingTest/API/FeedlingAPI.swift` (struct ~line 3157; `fetchFlowTrace` ~line 3182)

**Interfaces:**
- Produces: `FlowTraceEvent` gains `let explain: String`, `let contentExcerptJSON: String`, `let durMs: Double?`, `let traceId: String`; `fetchFlowTrace` returns `(events:, deployEnabled:, verbose:)`.

- [ ] **Step 1: Extend the struct**

```swift
    struct FlowTraceEvent: Identifiable {
        let id = UUID()
        let ts: Double
        let subsystem: String
        let type: String
        let actor: String
        let status: String
        let summary: String
        let explain: String
        let detailJSON: String
        let contentExcerptJSON: String
        let durMs: Double?
        let traceId: String
    }
```

- [ ] **Step 2: Parse the new fields** in `fetchFlowTrace`'s `raw.map`, and return `verbose`:

```swift
        let verbose = (obj["verbose"] as? Bool) ?? false
        let events: [FlowTraceEvent] = raw.map { e in
            func jsonStr(_ key: String) -> String {
                guard let d = e[key] as? [String: Any], !d.isEmpty,
                      let dd = try? JSONSerialization.data(withJSONObject: d, options: [.sortedKeys]),
                      let s = String(data: dd, encoding: .utf8) else { return "" }
                return s
            }
            return FlowTraceEvent(
                ts: (e["ts"] as? Double) ?? 0,
                subsystem: e["subsystem"] as? String ?? "",
                type: e["type"] as? String ?? "",
                actor: e["actor"] as? String ?? "",
                status: e["status"] as? String ?? "",
                summary: e["summary"] as? String ?? "",
                explain: e["explain"] as? String ?? "",
                detailJSON: jsonStr("detail"),
                contentExcerptJSON: jsonStr("content_excerpt"),
                durMs: e["dur_ms"] as? Double,
                traceId: e["trace_id"] as? String ?? "")
        }
        return (events, deployEnabled, verbose)
```

Update the function signature to `-> (events: [FlowTraceEvent], deployEnabled: Bool, verbose: Bool)`.

- [ ] **Step 3: Fix the one existing caller** (`FlowTracePanel.load()` in `DebugTool.swift`) to destructure the 3-tuple, so the project still compiles. (It is replaced entirely in Task 10, but must compile in between.)

- [ ] **Step 4: Build**

Run: `cd /Users/hx/Projects/io/feedling-mcp-ios && xcodebuild -scheme FeedlingTest -destination 'generic/platform=iOS' build 2>&1 | tail -5`
Expected: `** BUILD SUCCEEDED **`.

- [ ] **Step 5: Commit**

```bash
cd /Users/hx/Projects/io/feedling-mcp-ios
git add App/FeedlingTest/API/FeedlingAPI.swift App/FeedlingTest/App/DebugTool.swift
git commit -m "feat(ios): FlowTraceEvent gains explain/contentExcerpt/durMs/traceId + verbose

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: iOS — pure grouping/stall logic (unit-testable)

**Files:**
- Create: `App/FeedlingTest/App/DebugConsole/TurnGrouping.swift`
- Test: `App/FeedlingTestTests/TurnGroupingTests.swift`

**Interfaces:**
- Produces: `enum TurnGrouping` with `static func group(_ events: [FeedlingAPI.FlowTraceEvent]) -> [Turn]` and `struct Turn { let traceId: String; let events: [FeedlingAPI.FlowTraceEvent]; var title: String; var totalDurMs: Double; var terminalStatus: String; var isStalled: Bool }`. A turn `isStalled` when it has a `*.start` with no matching `*.done`/`*.error` (same `type` prefix).

- [ ] **Step 1: Write the failing test**

```swift
// App/FeedlingTestTests/TurnGroupingTests.swift
import XCTest
@testable import FeedlingTest

final class TurnGroupingTests: XCTestCase {
    private func ev(_ trace: String, _ type: String, _ status: String = "ok", ts: Double = 0) -> FeedlingAPI.FlowTraceEvent {
        FeedlingAPI.FlowTraceEvent(ts: ts, subsystem: "agent", type: type, actor: "vps_resident",
                                   status: status, summary: "", explain: "", detailJSON: "",
                                   contentExcerptJSON: "", durMs: nil, traceId: trace)
    }

    func test_groups_by_trace_and_orders_ascending() {
        let turns = TurnGrouping.group([ev("A", "agent.model.call.done", ts: 2),
                                        ev("A", "route.chat.message", ts: 1),
                                        ev("B", "route.chat.message", ts: 3)])
        XCTAssertEqual(turns.count, 2)
        let a = turns.first { $0.traceId == "A" }!
        XCTAssertEqual(a.events.map { $0.type }, ["route.chat.message", "agent.model.call.done"])
    }

    func test_detects_stall_when_start_has_no_done() {
        let turns = TurnGrouping.group([ev("A", "agent.model.call.start", ts: 1)])
        XCTAssertTrue(turns[0].isStalled)
    }

    func test_not_stalled_when_start_and_done_present() {
        let turns = TurnGrouping.group([ev("A", "agent.model.call.start", ts: 1),
                                        ev("A", "agent.model.call.done", ts: 2)])
        XCTAssertFalse(turns[0].isStalled)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hx/Projects/io/feedling-mcp-ios && xcodebuild test -scheme FeedlingTest -destination 'platform=iOS Simulator,name=iPhone 15' -only-testing:FeedlingTestTests/TurnGroupingTests 2>&1 | tail -15`
Expected: FAIL — `TurnGrouping` not found.

- [ ] **Step 3: Implement `TurnGrouping.swift`**

```swift
import Foundation

enum TurnGrouping {
    struct Turn: Identifiable {
        var id: String { traceId }
        let traceId: String
        let events: [FeedlingAPI.FlowTraceEvent]
        var title: String
        var totalDurMs: Double
        var terminalStatus: String   // "ok" | "error" | "blocked" | "stalled"
        var isStalled: Bool
    }

    /// Group events by trace_id (empty trace_id → one "ungrouped" bucket),
    /// order each group ascending by ts, and mark a turn stalled when a
    /// `*.start` has no matching `*.done`/`*.error` (matched by the type stem
    /// before `.start`).
    static func group(_ events: [FeedlingAPI.FlowTraceEvent]) -> [Turn] {
        let buckets = Dictionary(grouping: events) { $0.traceId.isEmpty ? "ungrouped" : $0.traceId }
        var turns: [Turn] = buckets.map { (key, evs) in
            let ordered = evs.sorted { $0.ts < $1.ts }
            let stalled = detectStall(ordered)
            let anyErr = ordered.contains { $0.status == "error" }
            let anyBlocked = ordered.contains { $0.status == "blocked" }
            let terminal = stalled ? "stalled" : (anyErr ? "error" : (anyBlocked ? "blocked" : "ok"))
            let total = ordered.compactMap { $0.durMs }.reduce(0, +)
            let title = ordered.first(where: { !$0.explain.isEmpty })?.explain
                ?? ordered.first?.summary ?? key
            return Turn(traceId: key, events: ordered, title: title,
                        totalDurMs: total, terminalStatus: terminal, isStalled: stalled)
        }
        // Newest turn first (by latest event ts).
        turns.sort { ($0.events.last?.ts ?? 0) > ($1.events.last?.ts ?? 0) }
        return turns
    }

    private static func stem(_ type: String) -> String {
        for suffix in [".start", ".done", ".error"] where type.hasSuffix(suffix) {
            return String(type.dropLast(suffix.count))
        }
        return type
    }

    private static func detectStall(_ ordered: [FeedlingAPI.FlowTraceEvent]) -> Bool {
        var openStems = Set<String>()
        for e in ordered {
            let s = stem(e.type)
            if e.type.hasSuffix(".start") { openStems.insert(s) }
            else if e.type.hasSuffix(".done") || e.type.hasSuffix(".error") { openStems.remove(s) }
        }
        return !openStems.isEmpty
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `xcodebuild test -scheme FeedlingTest -destination 'platform=iOS Simulator,name=iPhone 15' -only-testing:FeedlingTestTests/TurnGroupingTests 2>&1 | tail -15`
Expected: `Test Suite 'TurnGroupingTests' passed`.

- [ ] **Step 5: Commit**

```bash
git add App/FeedlingTest/App/DebugConsole/TurnGrouping.swift App/FeedlingTestTests/TurnGroupingTests.swift
git commit -m "feat(ios): TurnGrouping pure logic (group by trace_id + stall detection)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: iOS — `DebugConsoleView` (chips / search / status filter / grouped⇄flat)

**Files:**
- Create: `App/FeedlingTest/App/DebugConsole/DebugConsoleView.swift`

**Interfaces:**
- Consumes: `FeedlingAPI.fetchFlowTrace` (Task 7), `TurnGrouping.group` (Task 8).
- Produces: `struct DebugConsoleView: View`.

- [ ] **Step 1: Implement the view.** Follow the existing `FlowTracePanel` styling in `DebugTool.swift` (List/refreshable/color-per-subsystem). Key state and behavior — 11 module chips default-all-selected, a `分组/扁平` picker, a search field, a status filter, client-side filtering:

```swift
import SwiftUI

struct DebugConsoleView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var all: [FeedlingAPI.FlowTraceEvent] = []
    @State private var deployEnabled = true
    @State private var verbose = false
    @State private var loading = false
    @State private var errorText: String?
    @State private var grouped = true
    @State private var search = ""
    @State private var statusFilter = "all"          // all|ok|error|blocked
    private static let modules = ["route","context","agent","memory","worldbook",
                                  "genesis","identity","proactive","perception","push","account"]
    @State private var selected = Set(modules)        // default all selected

    private var filtered: [FeedlingAPI.FlowTraceEvent] {
        all.filter { e in
            selected.contains(e.subsystem)
            && (statusFilter == "all" || e.status == statusFilter)
            && (search.isEmpty
                || e.type.localizedCaseInsensitiveContains(search)
                || e.explain.localizedCaseInsensitiveContains(search)
                || e.summary.localizedCaseInsensitiveContains(search)
                || e.contentExcerptJSON.localizedCaseInsensitiveContains(search))
        }
    }

    var body: some View {
        NavigationView {
            VStack(spacing: 0) {
                if !deployEnabled {
                    banner("⚠️ 后端 FEEDLING_V1_FLOW_TRACE=0 硬关了 — 不会记录任何事件", .orange)
                } else if !verbose {
                    banner("ℹ️ verbose 关（只有 metadata，无明文 excerpt）", .secondary)
                }
                controls
                if let errorText { Text(errorText).font(.caption).foregroundColor(.red).padding(8) }
                content
            }
            .navigationTitle("Debug Console")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } }
                ToolbarItem(placement: .primaryAction) {
                    Button(loading ? "…" : "刷新") { Task { await load() } }.disabled(loading)
                }
            }
        }
        .task { await load() }
    }

    private func banner(_ t: String, _ c: Color) -> some View {
        Text(t).font(.caption2).foregroundColor(c).frame(maxWidth: .infinity, alignment: .leading)
            .padding(6).background(c.opacity(0.1))
    }

    private var controls: some View {
        VStack(spacing: 6) {
            HStack {
                Picker("", selection: $grouped) { Text("分组").tag(true); Text("扁平").tag(false) }
                    .pickerStyle(.segmented).frame(width: 140)
                Picker("", selection: $statusFilter) {
                    Text("全部").tag("all"); Text("ok").tag("ok"); Text("error").tag("error"); Text("blocked").tag("blocked")
                }.pickerStyle(.menu)
                Spacer()
                Button("全选") { selected = Set(Self.modules) }.font(.caption)
                Button("清空") { selected = [] }.font(.caption)
            }
            TextField("搜索 type / explain / 明文…", text: $search).textFieldStyle(.roundedBorder).font(.caption)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(Self.modules, id: \.self) { m in
                        Button(m) { if selected.contains(m) { selected.remove(m) } else { selected.insert(m) } }
                            .font(.caption2)
                            .padding(.horizontal, 8).padding(.vertical, 3)
                            .background(selected.contains(m) ? Color.accentColor.opacity(0.25) : Color.gray.opacity(0.12))
                            .cornerRadius(8)
                    }
                }
            }
        }.padding(.horizontal).padding(.vertical, 6)
    }

    @ViewBuilder private var content: some View {
        if filtered.isEmpty && !loading {
            Spacer(); Text("（暂无事件 — 走一遍流程再刷新）").font(.caption).foregroundColor(.secondary); Spacer()
        } else if grouped {
            List(TurnGrouping.group(filtered)) { TurnCard(turn: $0) }.listStyle(.plain).refreshable { await load() }
        } else {
            List(filtered) { EventRow(event: $0) }.listStyle(.plain).refreshable { await load() }
        }
    }

    private func load() async {
        loading = true; defer { loading = false }
        do {
            let r = try await FeedlingAPI.shared.fetchFlowTrace(limit: 500)
            all = r.events; deployEnabled = r.deployEnabled; verbose = r.verbose; errorText = nil
        } catch { errorText = error.localizedDescription }
    }
}
```

- [ ] **Step 2: Build**

Run: `xcodebuild -scheme FeedlingTest -destination 'generic/platform=iOS' build 2>&1 | tail -5`
Expected: fails — `TurnCard` / `EventRow` not defined (Task 10 adds them). This is expected; do NOT commit yet.

- [ ] **Step 3: Continue to Task 10** (TurnCard + EventRow complete the view). Commit both together at the end of Task 10.

---

## Task 10: iOS — `TurnCard` / `EventRow` (human-first rows + stall/gap) + copy + wire-in

**Files:**
- Modify: `App/FeedlingTest/App/DebugConsole/DebugConsoleView.swift` (append `TurnCard`, `EventRow`)
- Modify: `App/FeedlingTest/App/DebugTool.swift` (replace `FlowTracePanel` usage with `DebugConsoleView`)

**Interfaces:**
- Consumes: `TurnGrouping.Turn`, `FeedlingAPI.FlowTraceEvent`, `UIPasteboard`.

- [ ] **Step 1: Append `TurnCard` + `EventRow`** to `DebugConsoleView.swift`:

```swift
private struct TurnCard: View {
    let turn: TurnGrouping.Turn
    @State private var expanded = false
    private var mark: String {
        switch turn.terminalStatus { case "stalled": return "⏳"; case "error": return "✗"; case "blocked": return "⛔"; default: return "✓" }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button { expanded.toggle() } label: {
                HStack {
                    Text(mark)
                    Text(turn.title).font(.caption).bold().lineLimit(2)
                    Spacer()
                    if turn.totalDurMs > 0 { Text("\(Int(turn.totalDurMs))ms").font(.caption2).foregroundColor(.secondary) }
                    Button { copyTurn() } label: { Image(systemName: "doc.on.doc").font(.caption2) }
                }
            }.buttonStyle(.plain)
            if expanded {
                ForEach(turn.events) { EventRow(event: $0, compact: true) }
                if turn.isStalled {
                    Text("⏳ 有步骤只 start 没 done — 卡在这一步（模型未返回）").font(.caption2).foregroundColor(.orange)
                }
            }
        }.padding(.vertical, 3)
    }
    private func copyTurn() {
        UIPasteboard.general.string = turn.events.map(EventRow.plain).joined(separator: "\n")
    }
}

private struct EventRow: View {
    let event: FeedlingAPI.FlowTraceEvent
    var compact = false
    @State private var showTech = false
    private var statusColor: Color { event.status == "ok" ? .green : (event.status == "error" ? .red : .orange) }
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(event.subsystem).font(.caption2).bold()
                    .padding(.horizontal, 5).padding(.vertical, 1).background(Color.gray.opacity(0.18)).cornerRadius(4)
                Text(event.type).font(.caption2)
                Spacer()
                if let d = event.durMs { Text("\(Int(d))ms").font(.caption2).foregroundColor(.secondary) }
                Circle().fill(statusColor).frame(width: 7, height: 7)
            }
            if !event.explain.isEmpty { Text(event.explain).font(.caption).foregroundColor(.primary) }
            Button(showTech ? "隐藏技术细节" : "技术细节 / 明文") { showTech.toggle() }.font(.caption2)
            if showTech {
                if !event.detailJSON.isEmpty { Text(event.detailJSON).font(.system(.caption2, design: .monospaced)).foregroundColor(.secondary) }
                if !event.contentExcerptJSON.isEmpty { Text(event.contentExcerptJSON).font(.system(.caption2, design: .monospaced)).foregroundColor(.secondary) }
                Button { UIPasteboard.general.string = EventRow.plain(event) } label: { Text("复制这条").font(.caption2) }
            }
        }.padding(.vertical, compact ? 1 : 3)
    }
    static func plain(_ e: FeedlingAPI.FlowTraceEvent) -> String {
        "[\(e.subsystem)] \(e.type) (\(e.status)) \(e.durMs.map { "\(Int($0))ms" } ?? "")\n\(e.explain)\ndetail=\(e.detailJSON)\ncontent=\(e.contentExcerptJSON)"
    }
}
```

- [ ] **Step 2: Wire into `DebugTool.swift`.** Change the `showFlowTrace` sheet to present `DebugConsoleView`, and rename the action-row label:

```swift
        .sheet(isPresented: $showFlowTrace) {
            DebugConsoleView()
        }
```
and the entry row:
```swift
            actionRow(label: "Debug Console (全流程日志)", trailing: "›") {
                showFlowTrace = true
            }
```
(Leave the `debugFlowTrace` toggle + `setFlowTraceEnabled` wiring as-is — it still gates recording.)

- [ ] **Step 3: Build**

Run: `xcodebuild -scheme FeedlingTest -destination 'generic/platform=iOS' build 2>&1 | tail -5`
Expected: `** BUILD SUCCEEDED **`.

- [ ] **Step 4: Commit**

```bash
git add App/FeedlingTest/App/DebugConsole/DebugConsoleView.swift App/FeedlingTest/App/DebugTool.swift
git commit -m "feat(ios): DebugConsole UI — grouped/flat, chips, search, copy; replaces FlowTracePanel

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: End-to-end verification (real test deploy)

**Files:** none (verification only). Follows the io crypto-e2e rule: verify on a real `test` deploy, not a local fake.

- [ ] **Step 1: Enable.** Deploy `feat/debug-console` (backend + consumer) to test; on device, open DebugTool → toggle **v1 flow trace** ON → open **Debug Console**.

- [ ] **Step 2: Happy path.** Send a normal message. Refresh. Expected in grouped view: one turn card whose steps include `route.chat.message` → `context.build` → `agent.model.call.start` → `agent.model.call.done` (with `dur_ms` + reply excerpt) → `route.chat.response` → `memory.capture.done`. Each row shows a human `explain` line; expanding shows detail + plaintext excerpt.

- [ ] **Step 3: Stall path.** Temporarily point the consumer at a model that will hit the 120s cap (or kill the provider). Send a message. Expected: the turn card shows `⏳` terminal and "只 start 没 done — 卡在这一步", pinpointing the model step.

- [ ] **Step 4: Runtime-safety spot check.** With the gate OFF (toggle v1 flow trace off), send a message and confirm normal reply (no behavior change, no latency). Confirm no `/v1/debug/trace/event` load. This validates Global Constraint #1.

- [ ] **Step 5: Verbose off.** Set `FEEDLING_DEBUG_VERBOSE=0` on the backend; confirm events still appear but with no plaintext excerpt (banner shows "verbose 关").

---

## Self-Review Notes (coverage vs spec)

- Backbone (`explain`/`content_excerpt`/`dur_ms`/`verbose_enabled`/caps) → Task 1. Emit endpoint + `verbose` → Task 2. trace_id glue → Task 3. Capture → Task 4. Consumer LLM in/out + stall → Tasks 5–6. iOS parse/group/UI/copy → Tasks 7–10. Runtime-safety + e2e → Task 11.
- Deferred to M2/M3 (out of scope here, per spec §11): memory read side, worldbook, genesis, identity, proactive/perception bridge, push, account, tool-level `agent.tool.call`, hosted/turn.py path.
- Open follow-ups for the executor: Task 3's `debug_preview` plaintext depends on the iOS client sending a preview; if it does not, the `route.chat.message` excerpt is empty (acceptable — the model.call excerpt still carries the message head). Task 4 requires locating the exact capture entrypoint on the live path (consumer capture job vs backend); thread `trace_id` from the triggering message if available.
