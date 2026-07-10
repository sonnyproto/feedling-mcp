# Chat File Upload (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let IO Chat accept file messages (`content_type=file`) end-to-end so the agent can read the file's content and reply in the same turn — the backend half of iOS PR feedling-mcp-ios#72.

**Architecture:** Thread `file` alongside `image` through the existing chat pipeline (hosted send + VPS client-sealed send → store → enclave decrypt → resident consumer). The server never parses file content (VPS envelopes are opaque); all content extraction (docx/xlsx text, text sniffing, on-disk landing, prompt assembly) happens in the shared resident consumer / agent side. Image files (jpg/png/gif/webp) are re-piped into the existing `content_type=image` path so they get vision.

**Tech Stack:** Python (backend + `tools/chat_resident_consumer.py`), stdlib `zipfile` for docx/xlsx (no new deps), pytest.

## Global Constraints

- **Server file size cap = `26_214_400` bytes (25 MiB), and MUST be ≥ the iOS client cap.** iOS gatekeeps at `25 * 1024 * 1024` (`ChatEmptyStateView.swift`). The server must NEVER bounce a file the client accepted — 413 is a fallback for non-iOS/future clients only. Any future limit change: client ≤ server, never the reverse.
- **iOS is not touched in this plan.** Only `feedling-mcp` backend + `tools/chat_resident_consumer.py`.
- **Server never decrypts.** File bytes ride the envelope opaquely; `content_type="file"`, `file_name`/`file_mime` are plaintext metadata only.
- **Extraction is stdlib-only** (`zipfile`), lives in the consumer, and must be deterministic — no "agent runs Bash unzip and improvises".
- **Consumer is one shared file** (`tools/chat_resident_consumer.py`) used by both hosted and VPS resident. VPS resident-runtime pins a consumer commit — deploying requires bumping that pin (out of scope for code, noted for rollout).
- **Display filename preserves the original (incl. CJK like 《离职协议.docx》)**; only strip path separators/control chars server-side. The aggressive ASCII clean (`[^A-Za-z0-9_.-]`) applies ONLY to the on-disk path built in the consumer.
- **Prompt assembly rule:** always report the original filename; explicitly declare any extraction ("system extracted to plain text") and any truncation. Weak models won't infer it.
- **Error returns use stable snake_case slugs** and every new slug is registered in `docs/API_ERRORS.md` (a test guards key slugs). Dynamic detail goes in `detail`.
- **Tests:** pure unit tests (no DB) get their filename added to the `_PURE_UNIT` set in `tests/conftest.py`. Run suite with:
  ```bash
  python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
  python -m pyflakes backend/<changed package>
  ```
- **Touching the envelope path → a real test-deploy e2e is mandatory** (Task 9), not just local fake-decrypt.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `backend/core/store.py` | `append_chat` accepts `content_type="file"` + `file_name`/`file_mime` extra keys | 1 |
| `backend/hosted/turn.py` | `_model_api_file_payload()` classify/validate; `MODEL_API_MAX_FILE_BYTES`; add `gif` to image whitelist; filename/text helpers | 2 |
| `backend/hosted/chat_send_core.py` | Hosted send file branch: image-repipe / file envelope / extras / caption | 3 |
| `backend/chat/chat_core.py` | VPS user-ingest (`write_message`) accepts `file` + carries `file_name`/`file_mime` extra | 4 |
| `backend/enclave/routes/chat.py` | `ctype=="file"` decrypt branch emits `file_b64`/`file_mime`/`file_name` (+caption) | 5 |
| `tools/chat_resident_consumer.py` | Extraction helpers (docx/xlsx/text-sniff); on-disk landing; prompt templates (CLI + HTTP inline); dispatch | 6, 7 |
| `docs/API_ERRORS.md` | Register new slugs | 8 |
| flow-trace `has_file` | `route.decided` detail parity | 8 |

---

## Task 1: store.py — accept `content_type="file"` + file metadata extras

**Files:**
- Modify: `backend/core/store.py:356` (the `ct = ...` coercion) and the extra-key allowlist starting `backend/core/store.py:382`
- Test: `tests/test_store_append_chat_file.py` (new, pure unit)

**Interfaces:**
- Produces: `store.append_chat(role, source, envelope, content_type="file", extra={"file_name": ..., "file_mime": ...})` persists `content_type="file"` (not silently coerced to `"text"`) and keeps `file_name`/`file_mime` in the stored row.

- [ ] **Step 1: Write the failing test**

Create `tests/test_store_append_chat_file.py`:

```python
"""append_chat must persist content_type='file' + file_name/file_mime extras.

Pure unit test — no DB. Uses the in-memory dict store path already exercised by
the other store unit tests. Added to _PURE_UNIT in tests/conftest.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from core import store as core_store  # noqa: E402


def _envelope():
    return {
        "id": "env_file_1",
        "v": 1,
        "body_ct": "ct",
        "nonce": "nn",
        "K_user": "ku",
        "K_enclave": "ke",
        "visibility": "shared",
        "owner_user_id": "usr_1",
    }


def test_append_chat_preserves_file_content_type_and_metadata(tmp_path, monkeypatch):
    s = core_store.UserStore.__new__(core_store.UserStore)
    core_store.UserStore._test_init_inmemory(s, user_id="usr_1")  # helper below
    msg = s.append_chat(
        "user", "chat", _envelope(),
        content_type="file",
        extra={"file_name": "离职协议.docx", "file_mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    )
    assert msg["content_type"] == "file"
    assert msg["file_name"] == "离职协议.docx"
    assert msg["file_mime"].endswith("wordprocessingml.document")
```

> If `UserStore` has no in-memory test constructor, mirror whatever the existing `tests/test_store_*.py` unit tests use to instantiate a store without Postgres. Read one before writing this test and copy its setup verbatim; replace the `_test_init_inmemory` line accordingly.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_store_append_chat_file.py -v`
Expected: FAIL — `content_type` comes back `"text"` (coerced) and `file_name` missing.

- [ ] **Step 3: Make `ct` accept `"file"`**

`backend/core/store.py:356`, change:

```python
        ct = content_type if content_type in ("text", "image") else "text"
```
to:
```python
        ct = content_type if content_type in ("text", "image", "file") else "text"
```

- [ ] **Step 4: Add `file_name`/`file_mime` to the extra allowlist**

In the `for key in (...)` tuple starting `backend/core/store.py:382`, add next to `"image_mime",`:

```python
                "image_mime",
                "file_name",
                "file_mime",
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_store_append_chat_file.py -v`
Expected: PASS

- [ ] **Step 6: Register the pure-unit test**

Add `"test_store_append_chat_file.py"` to the `_PURE_UNIT` set in `tests/conftest.py`.

- [ ] **Step 7: Commit**

```bash
git add backend/core/store.py tests/test_store_append_chat_file.py tests/conftest.py
git commit -m "feat(store): accept content_type=file + file_name/file_mime extras"
```

---

## Task 2: turn.py — `_model_api_file_payload()` classifier + validator

**Files:**
- Modify: `backend/hosted/turn.py` — near `MODEL_API_MAX_IMAGE_BYTES` (line ~1747) and `_model_api_image_payload` (line ~1750); the image-mime regex at line ~1755
- Test: `tests/test_model_api_file_payload.py` (new, pure unit)

**Interfaces:**
- Produces:
  ```python
  MODEL_API_MAX_FILE_BYTES = 26_214_400  # 25 MiB, >= iOS client cap

  # Returns (parse, err). parse is None when no file present OR on error.
  # err is None on success/absent, else (body_dict, status_int).
  # parse dict on success:
  #   {"kind": "image"|"file", "bytes": bytes, "mime": str, "name": str}
  def _model_api_file_payload(payload: dict) -> tuple[dict | None, tuple[dict, int] | None]: ...
  ```
  `kind=="image"` means "re-pipe through the existing image path"; `kind=="file"` means "store as content_type=file".
- Also: `_model_api_image_payload` now accepts `image/gif`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_model_api_file_payload.py`:

```python
"""_model_api_file_payload classification + validation. Pure unit."""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from hosted import turn  # noqa: E402


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_absent_file_returns_none_none():
    assert turn._model_api_file_payload({}) == (None, None)


def test_png_file_repipes_as_image():
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64(b"\x89PNG\r\n\x1a\n..."), "file_name": "shot.png", "file_mime": "image/png"}
    )
    assert err is None
    assert parse["kind"] == "image"
    assert parse["mime"] == "image/png"


def test_gif_allowed_as_image():
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64(b"GIF89a..."), "file_name": "a.gif", "file_mime": "image/gif"}
    )
    assert err is None and parse["kind"] == "image" and parse["mime"] == "image/gif"


def test_heic_rejected_with_hint():
    parse, (body, status) = turn._model_api_file_payload(
        {"file_b64": _b64(b"\x00\x00\x00 ftypheic"), "file_name": "p.heic", "file_mime": "image/heic"}
    )
    assert parse is None and status == 400
    assert body["error"] == "unsupported_file_type" and "hint" in body


def test_docx_by_extension_is_file():
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64(b"PK\x03\x04binary-zip"), "file_name": "报告.docx", "file_mime": ""}
    )
    assert err is None and parse["kind"] == "file"
    assert parse["name"] == "报告.docx"  # unicode display name preserved


def test_plain_text_sniff_accepts_source_code():
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64("def f():\n    return 1\n".encode()), "file_name": "s.py", "file_mime": ""}
    )
    assert err is None and parse["kind"] == "file"


def test_binary_without_known_ext_rejected():
    parse, (body, status) = turn._model_api_file_payload(
        {"file_b64": _b64(b"\x00\x01\x02\x03NUL-inside"), "file_name": "blob.bin", "file_mime": ""}
    )
    assert parse is None and status == 400 and body["error"] == "unsupported_file_type"


def test_doc_old_binary_rejected():
    parse, (body, status) = turn._model_api_file_payload(
        {"file_b64": _b64(b"\xd0\xcf\x11\xe0old-ole"), "file_name": "old.doc", "file_mime": ""}
    )
    assert parse is None and status == 400 and body["error"] == "unsupported_file_type"


def test_invalid_base64_rejected():
    parse, (body, status) = turn._model_api_file_payload(
        {"file_b64": "!!!not-base64!!!", "file_name": "x.txt"}
    )
    assert parse is None and status == 400 and body["error"] == "invalid_file"


def test_oversize_rejected_413():
    big = b"a" * (turn.MODEL_API_MAX_FILE_BYTES + 1)
    parse, (body, status) = turn._model_api_file_payload(
        {"file_b64": _b64(big), "file_name": "big.txt", "file_mime": "text/plain"}
    )
    assert parse is None and status == 413 and body["error"] == "payload_too_large"
    assert body["max_bytes"] == turn.MODEL_API_MAX_FILE_BYTES


def test_display_name_strips_path_but_keeps_unicode():
    parse, err = turn._model_api_file_payload(
        {"file_b64": _b64(b"hello"), "file_name": "../../离职协议.txt", "file_mime": "text/plain"}
    )
    assert err is None and parse["name"] == "离职协议.txt"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_model_api_file_payload.py -v`
Expected: FAIL — `_model_api_file_payload` / `MODEL_API_MAX_FILE_BYTES` not defined.

- [ ] **Step 3: Add the gif allowance + constant + helpers + classifier**

In `backend/hosted/turn.py`, update the image mime regex at line ~1755:

```python
    if not re.match(r"^image/(jpeg|jpg|png|webp|gif)$", mime, re.I):
        return None, "", "image_mime must be image/jpeg, image/png, image/webp, or image/gif"
```

Then add, right after `MODEL_API_MAX_IMAGE_BYTES = 2_000_000` (line ~1747):

```python
MODEL_API_MAX_FILE_BYTES = 26_214_400  # 25 MiB — MUST be >= iOS client cap (ChatEmptyStateView 25*1024*1024)

_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
_DOC_EXTS = {"pdf", "docx", "xlsx"}
_DEFAULT_DOC_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _file_ext(name: str) -> str:
    _, _, ext = name.rpartition(".")
    return ext.lower() if "." in name else ""


def _display_file_name(raw: str) -> str:
    # Keep the user-facing name (incl. CJK); strip path components + control
    # chars only. The fs-safe ASCII clean happens later in the consumer when it
    # builds an on-disk path — do NOT ascii-ize here or 《离职协议.docx》 → ___.docx.
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable() and ch not in ('\n', '\r', '\t'))
    base = base.strip().strip(".") or "file"
    return base[:120]


def _normalize_image_mime(mime: str, ext: str) -> str | None:
    m = mime.lower()
    if m in ("image/jpeg", "image/jpg"):
        return "image/jpeg"
    if m in ("image/png", "image/webp", "image/gif"):
        return m
    by_ext = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "webp": "image/webp", "gif": "image/gif"}
    return by_ext.get(ext)  # None for heic and anything unsupported


def _looks_like_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _model_api_file_payload(payload: dict) -> tuple[dict | None, tuple[dict, int] | None]:
    raw = str(payload.get("file_b64") or "").strip()
    if not raw:
        return None, None
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
        raw = raw.strip()
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        return None, ({"error": "invalid_file", "detail": "file_b64 must be valid base64"}, 400)
    if not data:
        return None, ({"error": "invalid_file", "detail": "file_b64 must not be empty"}, 400)
    if len(data) > MODEL_API_MAX_FILE_BYTES:
        return None, ({"error": "payload_too_large", "detail": "file too large",
                       "max_bytes": MODEL_API_MAX_FILE_BYTES}, 413)
    name = _display_file_name(str(payload.get("file_name") or "").strip())
    mime = str(payload.get("file_mime") or "").strip().lower()
    ext = _file_ext(name)

    if mime.startswith("image/") or ext in _IMAGE_EXTS:
        img_mime = _normalize_image_mime(mime, ext)
        if img_mime is None:
            return None, ({"error": "unsupported_file_type",
                           "detail": f"image type not supported: {mime or ext or 'unknown'}",
                           "hint": "convert to jpeg/png/webp/gif before sending"}, 400)
        return {"kind": "image", "bytes": data, "mime": img_mime, "name": name}, None

    if ext in _DOC_EXTS:
        return {"kind": "file", "bytes": data,
                "mime": mime or _DEFAULT_DOC_MIME[ext], "name": name}, None

    if _looks_like_text(data):
        return {"kind": "file", "bytes": data, "mime": mime or "text/plain", "name": name}, None

    return None, ({"error": "unsupported_file_type",
                   "detail": f"file type not supported: {ext or mime or 'binary'}",
                   "hint": "supported: pdf, docx, xlsx, images, or utf-8 text/source files"}, 400)
```

> `base64` and `re` are already imported at the top of `turn.py` (used by `_model_api_image_payload`). Verify before adding; do not re-import.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_model_api_file_payload.py -v`
Expected: PASS (all 11 tests)

- [ ] **Step 5: Register the pure-unit test**

Add `"test_model_api_file_payload.py"` to `_PURE_UNIT` in `tests/conftest.py`.

- [ ] **Step 6: Commit**

```bash
git add backend/hosted/turn.py tests/test_model_api_file_payload.py tests/conftest.py
git commit -m "feat(hosted): _model_api_file_payload classifier + gif image support"
```

---

## Task 3: hosted/chat_send_core.py — wire the file branch into hosted send

**Files:**
- Modify: `backend/hosted/chat_send_core.py:47-146`
- Test: `tests/test_asgi_hosted_chat_send.py` (extend — mirror the existing image case)

**Interfaces:**
- Consumes: `hosted_turn._model_api_file_payload` (Task 2), `store.append_chat(content_type="file", extra=...)` (Task 1), existing `core_envelope._build_shared_envelope_for_store`, `chat_service._chat_caption_extra_from_envelope`.
- Produces: a file turn stored as `content_type="file"` with `file_name`/`file_mime` in extra (image files stored as `content_type="image"` via the existing path). `route.decided` detail carries `has_file`.

- [ ] **Step 1: Write the failing test**

In `tests/test_asgi_hosted_chat_send.py`, add (mirror the existing send test's fixtures for provider/supervisor/enclave stubbing — copy that setup):

```python
def test_send_file_stores_file_turn(monkeypatch):
    # Arrange: same stubs as the image/text send test in this file (provider
    # configured, supervisor live, enclave envelope build stubbed). Reuse the
    # existing helper/fixture used by test_send_image_* — do not re-stub by hand.
    client, store = _configured_client(monkeypatch)  # existing helper name
    import base64
    body = {
        "content_type": "file",
        "file_name": "notes.md",
        "file_mime": "text/markdown",
        "file_b64": base64.b64encode(b"# Title\nbody\n").decode(),
        "message": "read this",
    }
    resp = client.post("/v1/model_api/chat/send", json=body)
    assert resp.status_code == 202
    last = store.latest_chat_row()  # existing test helper for the appended row
    assert last["content_type"] == "file"
    assert last["file_name"] == "notes.md"
    assert last["file_mime"] == "text/markdown"


def test_send_image_file_repipes_as_image(monkeypatch):
    client, store = _configured_client(monkeypatch)
    import base64
    body = {
        "content_type": "file",
        "file_name": "pic.png",
        "file_mime": "image/png",
        "file_b64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
    }
    resp = client.post("/v1/model_api/chat/send", json=body)
    assert resp.status_code == 202
    assert store.latest_chat_row()["content_type"] == "image"


def test_send_unsupported_file_400(monkeypatch):
    client, _ = _configured_client(monkeypatch)
    import base64
    body = {
        "content_type": "file",
        "file_name": "x.bin",
        "file_mime": "",
        "file_b64": base64.b64encode(b"\x00\x01\x02bin").decode(),
    }
    resp = client.post("/v1/model_api/chat/send", json=body)
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_file_type"
```

> Helper names (`_configured_client`, `store.latest_chat_row`) are placeholders — read the existing image/text send tests in this file and use their real fixtures/helpers. The point of the test is the three asserts on status + stored row.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_asgi_hosted_chat_send.py -k file -v`
Expected: FAIL — file turns currently fall through to `message required` (400) because file has no `message` and `has_image` is False.

- [ ] **Step 3: Implement the file branch**

In `backend/hosted/chat_send_core.py`, right after the image parse block (after line 51 `has_image = image_bytes is not None`), insert:

```python
    file_parse, file_err = hosted_turn._model_api_file_payload(payload)
    if file_err:
        return file_err  # (body, status) already shaped
    # An image sent through the file picker re-pipes into the image path so it
    # gets vision — reuse the exact image envelope/append below.
    if file_parse is not None and file_parse["kind"] == "image":
        image_bytes = file_parse["bytes"]
        image_mime = file_parse["mime"]
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        has_image = True
        file_parse = None
    has_file = file_parse is not None
```

Update `message_for_context` (line 53) so a bare file turn is not rejected by the `message required` guard:

```python
    message_for_context = message or (
        "User sent an image." if has_image else ("User sent a file." if has_file else "")
    )
```

Where the image `extra` is assembled (after line 108's caption block), add the file metadata + caption:

```python
    if has_file:
        extra["file_name"] = file_parse["name"]
        extra["file_mime"] = file_parse["mime"]
        if message:
            cap_env, cap_err = core_envelope._build_shared_envelope_for_store(
                store, message.encode("utf-8")
            )
            if cap_env:
                extra.update(chat_service._chat_caption_extra_from_envelope(cap_env))
            else:
                print(f"[model_api:{store.user_id}] file caption_envelope_failed detail={cap_err}")
```

Set `user_plaintext` for the file case (line 72) — the envelope payload is the raw file bytes:

```python
    if has_image:
        user_plaintext = image_bytes
    elif has_file:
        user_plaintext = file_parse["bytes"]
    else:
        user_plaintext = message.encode("utf-8")
```

Set the stored `content_type` (line 133):

```python
        content_type="image" if has_image else ("file" if has_file else "text"),
```

And the trace detail (line 143):

```python
        detail={"mode": "agent_runtime", "has_image": bool(has_image), "has_file": bool(has_file)},
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_asgi_hosted_chat_send.py -k file -v`
Expected: PASS

- [ ] **Step 5: Run the full send test to confirm no regression**

Run: `python -m pytest tests/test_asgi_hosted_chat_send.py -q`
Expected: PASS (existing image/text cases unaffected)

- [ ] **Step 6: Commit**

```bash
git add backend/hosted/chat_send_core.py tests/test_asgi_hosted_chat_send.py
git commit -m "feat(hosted): send file turns (image-repipe / file envelope + caption)"
```

---

## Task 4: chat_core.py — VPS client-sealed ingest accepts `file`

**Files:**
- Modify: `backend/chat/chat_core.py:304-307` (the `write_message` user-ingest path only — NOT the response path at 394)
- Test: `tests/test_chat_core_file_ingest.py` (new)

**Interfaces:**
- Consumes: `store.append_chat(content_type="file", extra=...)` (Task 1).
- Produces: VPS `POST /v1/chat/send` accepts `content_type="file"` and carries `file_name`/`file_mime` (plaintext metadata) into the stored row. Content stays opaque (client-sealed envelope).

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat_core_file_ingest.py` mirroring the existing `chat_core` write_message tests (read `tests/test_asgi_chat_remaining.py` or whichever covers `write_message` and copy its store fixture):

```python
"""VPS user ingest (chat_core.write_message) accepts content_type=file."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from chat import chat_core  # noqa: E402


def _valid_envelope():
    return {
        "id": "env_f1", "v": 1, "body_ct": "ct", "nonce": "nn",
        "K_user": "ku", "K_enclave": "ke", "visibility": "shared",
        "owner_user_id": "usr_1",
    }


def test_write_message_accepts_file(fake_store):  # reuse existing store fixture
    payload = {
        "envelope": _valid_envelope(),
        "content_type": "file",
        "file_name": "plan.md",
        "file_mime": "text/markdown",
    }
    body, status = chat_core.write_message(fake_store, payload)
    assert status == 200
    row = fake_store.latest_chat_row()
    assert row["content_type"] == "file"
    assert row["file_name"] == "plan.md"


def test_write_message_rejects_unknown_content_type(fake_store):
    payload = {"envelope": _valid_envelope(), "content_type": "video"}
    body, status = chat_core.write_message(fake_store, payload)
    assert status == 400 and body["error"].startswith("content_type")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_chat_core_file_ingest.py -v`
Expected: FAIL — `content_type must be 'text' or 'image'` rejects `"file"`.

- [ ] **Step 3: Widen the whitelist + carry file metadata**

`backend/chat/chat_core.py`, replace lines 304-307:

```python
    content_type = payload.get("content_type", "text")
    if content_type not in ("text", "image", "file"):
        return {"error": "content_type must be 'text', 'image', or 'file'"}, 400
    file_extra: dict = {}
    if content_type == "file":
        fname = str(payload.get("file_name") or "").strip()
        fmime = str(payload.get("file_mime") or "").strip()
        if fname:
            file_extra["file_name"] = fname[:120]
        if fmime:
            file_extra["file_mime"] = fmime[:120]
    msg = store.append_chat(
        "user", "chat", envelope,
        content_type=content_type,
        extra=file_extra or None,
    )
```

Also update the `content_excerpt` guard on line 319 so a file turn (no plaintext) does not attempt a text excerpt — it already gates on `content_type == "text"`, so no change needed; confirm it reads `if content_type == "text"`.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_chat_core_file_ingest.py -v`
Expected: PASS

- [ ] **Step 5: Register pure-unit test if applicable + commit**

If the store fixture needs no DB, add the filename to `_PURE_UNIT`. Then:

```bash
git add backend/chat/chat_core.py tests/test_chat_core_file_ingest.py tests/conftest.py
git commit -m "feat(chat): VPS ingest accepts content_type=file + file metadata"
```

---

## Task 5: enclave/routes/chat.py — file decrypt branch

**Files:**
- Modify: `backend/enclave/routes/chat.py:71-98` (the `if ctype == "image":` / `else:` block)
- Test: `tests/test_enclave_routes_chat.py` (extend) or `tests/test_enclave_body_helper.py` — whichever covers `_decrypt_history_items`

**Interfaces:**
- Consumes: stored file row with `content_type="file"`, `file_name`, `file_mime`, optional `caption_*` (Tasks 1/3/4).
- Produces: a decrypted history entry `{"content_type": "file", "file_b64": <base64 of plaintext bytes>, "file_mime": ..., "file_name": ..., "content": <caption text or "">}`.

- [ ] **Step 1: Write the failing test**

Add to the enclave decrypt test (mirror the existing image-branch assertion that checks `image_b64`/`image_mime`):

```python
def test_decrypt_history_file_entry_emits_file_fields():
    # Build a stored file row the same way the image test builds its row, but
    # content_type="file" with file_name/file_mime, envelope plaintext = b"file bytes".
    row = _make_stored_row(plaintext=b"raw doc bytes", content_type="file",
                           extra={"file_name": "报告.docx", "file_mime": "application/...docx"})
    decrypted, errors = chat_route._decrypt_history_items([row], _uid, _sk)
    entry = decrypted[0]
    assert entry["content_type"] == "file"
    assert base64.b64decode(entry["file_b64"]) == b"raw doc bytes"
    assert entry["file_name"] == "报告.docx"
    assert entry["file_mime"].endswith("docx")
```

> `_make_stored_row`, `chat_route`, `_uid`, `_sk` are placeholders — read the existing image-branch test in this file and reuse its row-builder + decrypt call verbatim.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_enclave_routes_chat.py -k file -v`
Expected: FAIL — file rows currently hit the `else` branch and decode plaintext as utf-8 text, no `file_b64`.

- [ ] **Step 3: Add the file branch**

In `backend/enclave/routes/chat.py`, change the `if ctype == "image":` (line 71) to also handle file. After the image block's `entry["image_mime"] = ...` (line 96), before `else:` (line 97), insert an `elif`:

```python
            elif ctype == "file":
                # File plaintext is the raw file bytes — surface as base64 so the
                # resident consumer can land it on disk / inline it. Caption
                # (user text alongside the file) decrypts into content, mirroring
                # the image branch.
                entry["content"] = ""
                cap_ct = m.get("caption_body_ct")
                if cap_ct:
                    cap_env = {
                        "id": m.get("caption_id") or m.get("id"),
                        "v": int(m.get("caption_v", v) or v),
                        "body_ct": cap_ct,
                        "nonce": m.get("caption_nonce"),
                        "K_enclave": m.get("caption_K_enclave"),
                        "owner_user_id": m.get("caption_owner_user_id") or m.get("owner_user_id"),
                    }
                    try:
                        entry["content"] = envelope.decrypt_envelope(
                            cap_env, authorized_user_id, content_sk
                        ).decode("utf-8", errors="replace")
                    except Exception as e:
                        errors.append({"id": m.get("id"), "reason": f"caption_decrypt: {e}"})
                entry["file_b64"] = base64.b64encode(plaintext).decode("ascii")
                entry["file_mime"] = m.get("file_mime") or "application/octet-stream"
                entry["file_name"] = m.get("file_name") or "file"
```

> The caption block is duplicated from the image branch intentionally (the plan's readers may implement tasks out of order and the two branches may diverge later). If a reviewer prefers, a shared `_decrypt_caption(m, ...)` helper is a fine cleanup — but not required.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_enclave_routes_chat.py -k file -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/enclave/routes/chat.py tests/test_enclave_routes_chat.py
git commit -m "feat(enclave): decrypt content_type=file into file_b64/file_mime/file_name"
```

---

## Task 6: consumer — extraction helpers (docx / xlsx / text sniff)

**Files:**
- Modify: `tools/chat_resident_consumer.py` — add near the image helpers (`_image_payloads_from_msg` ~line 1291)
- Test: `tests/test_chat_resident_consumer_file.py` (new, pure unit)

**Interfaces:**
- Produces:
  ```python
  _XLSX_MAX_SHEETS = 5
  _XLSX_MAX_ROWS = 2000

  def _extract_docx_text(data: bytes) -> str | None       # None on failure
  def _extract_xlsx_text(data: bytes) -> tuple[str, bool]  # (tsv_text, truncated)
  def _friendly_file_type(name: str, mime: str) -> str     # "Word 文档" / "PDF" / ...
  # returns what the consumer should hand the agent for a file message:
  #   (local_path | None, inline_text | None, meta) where meta carries
  #   original name, friendly type, truncation info, and an "extracted" flag.
  def _prepare_file_for_agent(msg: dict) -> "FilePrep"
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat_resident_consumer_file.py` (mirror the env-bootstrap header of `tests/test_chat_resident_consumer_image.py` verbatim), then:

```python
import io
import zipfile


def _make_docx(paragraphs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def _make_xlsx(rows):
    # minimal: inline string cells, one sheet
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", '<sst xmlns="x"></sst>')
        sheet_rows = ""
        for r in rows:
            cells = "".join(f'<c t="inlineStr"><is><t>{v}</t></is></c>' for v in r)
            sheet_rows += f"<row>{cells}</row>"
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet xmlns="x"><sheetData>{sheet_rows}</sheetData></worksheet>')
    return buf.getvalue()


def test_extract_docx_text():
    from tools import chat_resident_consumer as c
    data = _make_docx(["Hello", "第二段"])
    text = c._extract_docx_text(data)
    assert "Hello" in text and "第二段" in text


def test_extract_docx_bad_zip_returns_none():
    from tools import chat_resident_consumer as c
    assert c._extract_docx_text(b"not-a-zip") is None


def test_extract_xlsx_tsv_and_truncation():
    from tools import chat_resident_consumer as c
    rows = [["a", "b"], ["c", "d"]]
    text, truncated = c._extract_xlsx_text(_make_xlsx(rows))
    assert "a\tb" in text and truncated is False

    big = [["x", str(i)] for i in range(c._XLSX_MAX_ROWS + 50)]
    text2, truncated2 = c._extract_xlsx_text(_make_xlsx(big))
    assert truncated2 is True


def test_friendly_file_type():
    from tools import chat_resident_consumer as c
    assert "Word" in c._friendly_file_type("a.docx", "")
    assert "PDF" in c._friendly_file_type("a.pdf", "application/pdf")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_chat_resident_consumer_file.py -v`
Expected: FAIL — helpers not defined.

- [ ] **Step 3: Implement the extraction helpers**

Add to `tools/chat_resident_consumer.py` (near line 1291). `zipfile`, `re`, `base64`, `os`, `Path` are already imported at module top — verify and don't re-import.

```python
import xml.etree.ElementTree as _ET

_XLSX_MAX_SHEETS = 5
_XLSX_MAX_ROWS = 2000
FILE_TEMP_DIR = Path(os.environ.get("FILE_TEMP_DIR", "/tmp/feedling_chat_files"))
FILE_PLACEHOLDER = "User sent a file."


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _extract_docx_text(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except Exception as e:
        log.warning("docx extract failed: %s", e)
        return None
    try:
        root = _ET.fromstring(xml)
    except Exception as e:
        log.warning("docx xml parse failed: %s", e)
        return None
    paras = []
    for p in root.iter():
        if _strip_ns(p.tag) != "p":
            continue
        texts = [t.text or "" for t in p.iter() if _strip_ns(t.tag) == "t"]
        line = "".join(texts).strip()
        if line:
            paras.append(line)
    return "\n".join(paras)


def _extract_xlsx_text(data: bytes) -> tuple[str, bool]:
    truncated = False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in z.namelist():
                sroot = _ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in sroot:
                    if _strip_ns(si.tag) != "si":
                        continue
                    shared.append("".join(t.text or "" for t in si.iter()
                                          if _strip_ns(t.tag) == "t"))
            sheet_names = sorted(n for n in z.namelist()
                                 if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
            if len(sheet_names) > _XLSX_MAX_SHEETS:
                sheet_names = sheet_names[:_XLSX_MAX_SHEETS]
                truncated = True
            out_lines: list[str] = []
            for sn in sheet_names:
                root = _ET.fromstring(z.read(sn))
                rows = [r for r in root.iter() if _strip_ns(r.tag) == "row"]
                if len(rows) > _XLSX_MAX_ROWS:
                    rows = rows[:_XLSX_MAX_ROWS]
                    truncated = True
                for r in rows:
                    cells = []
                    for c in r:
                        if _strip_ns(c.tag) != "c":
                            continue
                        t = c.get("t")
                        val = ""
                        if t == "s":  # shared-string index
                            v = c.find("{*}v")
                            if v is not None and v.text and v.text.isdigit():
                                idx = int(v.text)
                                val = shared[idx] if 0 <= idx < len(shared) else ""
                        elif t == "inlineStr":
                            val = "".join(x.text or "" for x in c.iter()
                                          if _strip_ns(x.tag) == "t")
                        else:
                            v = c.find("{*}v")
                            val = (v.text or "") if v is not None else ""
                        cells.append(val)
                    out_lines.append("\t".join(cells))
            return "\n".join(out_lines), truncated
    except Exception as e:
        log.warning("xlsx extract failed: %s", e)
        return "", False


def _friendly_file_type(name: str, mime: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "pdf": "PDF 文档", "docx": "Word 文档", "xlsx": "Excel 表格",
        "md": "Markdown 文件", "csv": "CSV 表格", "json": "JSON 文件",
        "txt": "文本文件",
    }.get(ext, "文件")
```

> `io` and `zipfile` may not yet be imported at the top of `chat_resident_consumer.py` — check the import block and add `import io` / `import zipfile` there if missing (top of file, not inline).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_chat_resident_consumer_file.py -v`
Expected: PASS

- [ ] **Step 5: Register pure-unit test + commit**

Add `"test_chat_resident_consumer_file.py"` to `_PURE_UNIT`. Then:

```bash
git add tools/chat_resident_consumer.py tests/test_chat_resident_consumer_file.py tests/conftest.py
git commit -m "feat(consumer): docx/xlsx text extraction + friendly type labels"
```

---

## Task 7: consumer — landing, prompt assembly, dispatch

**Files:**
- Modify: `tools/chat_resident_consumer.py` — `_prepare_file_for_agent` + prompt templates near `_message_for_agent` (~line 1341); dispatch in the message loop at ~line 6627
- Test: `tests/test_chat_resident_consumer_file.py` (extend)

**Interfaces:**
- Consumes: `_extract_docx_text`, `_extract_xlsx_text`, `_friendly_file_type` (Task 6); decrypted history entry fields `file_b64`/`file_mime`/`file_name`/`content` (Task 5).
- Produces: for a `content_type=="file"` message, the loop feeds the agent either (CLI) a landed local path + a metadata-rich instruction, or (HTTP, no tools) inlined extracted text — both always naming the original file and declaring extraction/truncation.

- [ ] **Step 1: Write the failing test**

Extend `tests/test_chat_resident_consumer_file.py`:

```python
def test_prepare_text_file_lands_and_names_original(tmp_path, monkeypatch):
    from tools import chat_resident_consumer as c
    monkeypatch.setattr(c, "FILE_TEMP_DIR", tmp_path)
    import base64
    msg = {"id": "m1", "content_type": "file", "file_name": "笔记.md",
           "file_mime": "text/markdown",
           "file_b64": base64.b64encode(b"# hi\n").decode()}
    prep = c._prepare_file_for_agent(msg)
    assert prep.original_name == "笔记.md"
    assert prep.local_path is not None and prep.local_path.endswith(".md")
    # instruction names the original file
    assert "笔记.md" in prep.cli_instruction


def test_prepare_docx_declares_extraction(tmp_path, monkeypatch):
    from tools import chat_resident_consumer as c
    monkeypatch.setattr(c, "FILE_TEMP_DIR", tmp_path)
    import base64
    docx = _make_docx(["Body para"])
    msg = {"id": "m2", "content_type": "file", "file_name": "报告.docx",
           "file_mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           "file_b64": base64.b64encode(docx).decode()}
    prep = c._prepare_file_for_agent(msg)
    assert prep.extracted is True
    assert prep.inline_text and "Body para" in prep.inline_text
    assert "抽取" in prep.cli_instruction  # declares system extracted to text


def test_prepare_pdf_http_inline_declines(tmp_path, monkeypatch):
    from tools import chat_resident_consumer as c
    monkeypatch.setattr(c, "FILE_TEMP_DIR", tmp_path)
    import base64
    msg = {"id": "m3", "content_type": "file", "file_name": "a.pdf",
           "file_mime": "application/pdf",
           "file_b64": base64.b64encode(b"%PDF-1.4 ...").decode()}
    prep = c._prepare_file_for_agent(msg)
    # PDF cannot be inlined for a tool-less HTTP agent
    assert prep.inline_text is None
    assert prep.http_fallback_note and "PDF" in prep.http_fallback_note
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_chat_resident_consumer_file.py -k prepare -v`
Expected: FAIL — `_prepare_file_for_agent` / `FilePrep` not defined.

- [ ] **Step 3: Implement `FilePrep` + `_prepare_file_for_agent` + templates**

Add to `tools/chat_resident_consumer.py`:

```python
from dataclasses import dataclass

FILE_INLINE_MAX_CHARS = int(os.environ.get("FILE_INLINE_MAX_CHARS", "30000"))


@dataclass
class FilePrep:
    original_name: str
    friendly_type: str
    local_path: str | None          # landed original bytes (CLI Read path)
    inline_text: str | None         # extracted/sniffed text for HTTP inlining
    extracted: bool                 # True if we converted (docx/xlsx)
    truncated: bool
    truncation_note: str
    http_fallback_note: str | None  # set when there is nothing to inline (PDF)
    cli_instruction: str
    http_block: str


def _decode_file_b64(value) -> bytes | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return base64.b64decode(value, validate=True)
    except Exception as e:
        log.warning("file_b64 decode failed: %s", e)
        return None


def _human_size(n: int) -> str:
    return f"{n/1024:.0f} KB" if n < 1024 * 1024 else f"{n/1024/1024:.1f} MB"


def _land_file(msg_key: str, name: str, data: bytes) -> str:
    FILE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{msg_key}_{name}")[:120] or "file"
    if not safe.lower().endswith(f".{ext}"):
        safe = f"{safe}.{ext}"
    path = FILE_TEMP_DIR / safe
    try:
        path.write_bytes(data)
    except Exception as e:
        log.warning("failed to write file temp %s: %s", path, e)
        return ""
    return str(path)


def _prepare_file_for_agent(msg: dict) -> "FilePrep":
    name = str(msg.get("file_name") or "file")
    mime = str(msg.get("file_mime") or "").lower()
    ftype = _friendly_file_type(name, mime)
    data = _decode_file_b64(msg.get("file_b64")) or b""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(msg.get("id") or "file"))[:96] or "file"
    size = _human_size(len(data))

    inline_text: str | None = None
    extracted = False
    truncated = False
    truncation_note = ""
    local_path: str | None = None
    http_fallback_note: str | None = None

    if ext == "docx":
        text = _extract_docx_text(data)
        if text is not None:
            inline_text, extracted = text, True
        local_path = _land_file(key, name, data) or None
    elif ext == "xlsx":
        text, truncated = _extract_xlsx_text(data)
        inline_text, extracted = text, True
        if truncated:
            truncation_note = "（表格内容已截断，仅含前若干表/行）"
        local_path = _land_file(key, name, data) or None
    elif ext == "pdf":
        # Binary — CLI can Read it (native PDF), HTTP cannot inline it.
        local_path = _land_file(key, name, data) or None
        http_fallback_note = "此 connector 暂不支持读取 PDF。"
    else:
        # sniffed text / source: land original AND inline
        try:
            inline_text = data.decode("utf-8")
        except UnicodeDecodeError:
            inline_text = None
        local_path = _land_file(key, name, data) or None

    if inline_text and len(inline_text) > FILE_INLINE_MAX_CHARS:
        inline_text = inline_text[:FILE_INLINE_MAX_CHARS]
        truncated = True
        truncation_note = f"（内容在 {FILE_INLINE_MAX_CHARS} 字符处截断）"

    extract_clause = "（已由系统抽取为纯文本，原始格式/图片未保留）" if extracted else ""
    cli_instruction = (
        f"用户在 IO Chat 发来一个文件：\n"
        f"- 文件名：{name}\n"
        f"- 类型：{ftype}{extract_clause}\n"
        f"- 大小：{size}\n"
        + (f"- 本地路径：{local_path}\n" if local_path else "")
        + "用 Read 工具读上面这个精确路径后再回复。读不到就直说，"
        "不要假装读过、不要编造文件内容。"
        + (f"\n{truncation_note}" if truncation_note else "")
    )
    if inline_text is not None:
        http_block = (
            f"[用户发来文件「{name}」（{ftype}，{size}），以下是"
            f"{'抽取的纯文本内容，原始格式未保留' if extracted else '文件内容'}"
            f"{('，' + truncation_note) if truncation_note else ''}：]\n"
            f"<<<\n{inline_text}\n>>>\n"
            "[文件内容结束。请基于以上内容回复用户。]"
        )
    else:
        http_block = (
            f"[用户发来文件「{name}」（{ftype}，{size}）。"
            f"{http_fallback_note or '该文件无法在当前连接内读取。'}]"
        )

    return FilePrep(
        original_name=name, friendly_type=ftype, local_path=local_path,
        inline_text=inline_text, extracted=extracted, truncated=truncated,
        truncation_note=truncation_note, http_fallback_note=http_fallback_note,
        cli_instruction=cli_instruction, http_block=http_block,
    )
```

- [ ] **Step 4: Wire dispatch into the message loop**

In `tools/chat_resident_consumer.py` at the content-type dispatch (~line 6627), after the `if content_type == "image":` block and before `elif not content:`, add a `file` branch. It mirrors how image sets `content`/paths but uses the CLI instruction (tool agents) or the HTTP block (tool-less agents). Reuse the same `content`/`image_paths` variables the downstream `_message_for_agent`/HTTP send already consume — for files, prepend the block to `content`:

```python
        elif content_type == "file":
            log.info("file message [ts=%.3f] — preparing file context for agent", ts)
            prep = _prepare_file_for_agent(msg)
            caption = content  # user's accompanying text (may be "")
            if AGENT_MODE == "http" or not _agent_entry_has_tools():
                block = prep.http_block
            else:
                block = prep.cli_instruction
                if prep.local_path:
                    file_paths.append(prep.local_path)  # if a file-path list feeds the CLI template
            content = f"{caption}\n\n{block}".strip() if caption else block
```

> Read the surrounding loop first. Two integration facts to confirm and adapt to:
> 1. How the CLI agent is told about local files today (`_message_for_agent(content, image_paths)` at ~line 3170). If files should ride the same "local file paths" channel, append `prep.local_path` to that list; if not, the path is already named inside `cli_instruction` so the Read call works from the text alone.
> 2. Whether a helper like `_agent_entry_has_tools()` exists; if not, gate on `AGENT_MODE == "http"` (the OpenAI-compat, tool-less path) vs otherwise. Use the existing signal the image branch uses to choose multimodal-block vs CLI paths.

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_chat_resident_consumer_file.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tools/chat_resident_consumer.py tests/test_chat_resident_consumer_file.py
git commit -m "feat(consumer): land file, assemble CLI/HTTP prompt, dispatch file turns"
```

---

## Task 8: Register error slugs + flow-trace `has_file`

**Files:**
- Modify: `docs/API_ERRORS.md`
- Modify: `backend/hosted/chat_send_core.py` trace detail (done in Task 3 — verify present) and add a consumer-side flow-trace `has_file` if the consumer emits `route`/`context` traces for the turn
- Test: whichever test guards `docs/API_ERRORS.md` slug coverage (search for it)

**Interfaces:**
- Produces: slugs `unsupported_file_type`, `invalid_file` registered; `payload_too_large` reused (already exists — confirm). `route.decided` detail carries `has_file`.

- [ ] **Step 1: Find the slug-guard test**

Run: `grep -rn "API_ERRORS\|unsupported_file_type\|invalid_image" tests/ backend/ | grep -i "error\|slug" | head`
Confirm whether a test asserts every returned slug is documented.

- [ ] **Step 2: Register the slugs**

Add to `docs/API_ERRORS.md` (follow the file's existing row format):

```
- `unsupported_file_type` — chat file upload: file type not accepted (heic/.doc/.xls/binary). `detail` names the type; `hint` suggests a supported format.
- `invalid_file` — chat file upload: file_b64 missing/empty/not valid base64.
```

> `payload_too_large` is already used by the image/message-size path (413) — verify it is already documented; do not duplicate.

- [ ] **Step 3: Run the guard + confirm trace**

Run: `python -m pytest tests/ -q -k "api_error or slug or error_contract" --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
Expected: PASS (slugs documented). Also grep-confirm `has_file` is in the `route.decided` detail from Task 3.

- [ ] **Step 4: Commit**

```bash
git add docs/API_ERRORS.md
git commit -m "docs(api-errors): register unsupported_file_type + invalid_file slugs"
```

---

## Task 9: Full-suite gate + real-deploy e2e (envelope path)

**Files:** none (verification task)

- [ ] **Step 1: Full suite + pyflakes**

Run:
```bash
python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
python -m pyflakes backend/core backend/hosted backend/chat backend/enclave
python -m pyflakes tools/chat_resident_consumer.py
```
Expected: zero NEW failures (2 known-red enclave cases per CONTRIBUTING backlog #12 are acceptable), pyflakes clean.

- [ ] **Step 2: Real test-deploy e2e (mandatory — envelope was touched)**

Per the crypto-changes-need-real-deploy-e2e discipline, local fake-decrypt is insufficient. On a real test deploy, from the iOS build (or an authenticated API client) send one of each and confirm the agent reply references the content:
- a `.md` (sniffed text, CLI Read),
- a `.pdf` (CLI native Read),
- a `.docx` (extracted text),
- an `.xlsx` (extracted TSV),
- an image file `.png` (re-piped → vision).

Confirm: file turn stores `content_type="file"` (image file stores `"image"`), enclave history round-trips `file_b64` + `file_name`, consumer lands + reads, agent reply cites file content. Capture the flow-trace `has_file` event as objective proof the path ran.

- [ ] **Step 3: Rollout note**

VPS resident-runtime pins a consumer commit — bump that pin so VPS residents get the file-handling consumer. (Deploy action, tracked outside this plan.)

---

## Self-Review

**Spec coverage:** Scope (当轮可读) → Tasks 3/7. Image-repipe → Tasks 2/3. docx/xlsx/text/pdf types → Tasks 2/6/7. heic/.doc/.xls reject → Task 2. 25 MiB cap ≥ client → Tasks 2 (const) + Global Constraints. Filename plaintext extra → Tasks 1/3/4. Enclave decrypt → Task 5. VPS ingest → Task 4. Low-model HTTP inline + PDF decline → Task 7. Prompt metadata (name/extraction/truncation) → Task 7. Error slugs → Task 8. Flow-trace has_file → Tasks 3/8. Real e2e → Task 9. **Gap found & folded in:** `store.append_chat` silent coercion (`store.py`) was not in the spec's file list → Task 1.

**Placeholder scan:** Test-harness helper names (`_configured_client`, `fake_store`, `_make_stored_row`, `_agent_entry_has_tools`) are explicitly flagged as "read the neighboring existing test/loop and use the real name" — the surrounding assertions and production code are concrete. No TODO/TBD left in production code steps.

**Type consistency:** `_model_api_file_payload` returns `(parse|None, err|None)` used identically in Tasks 2/3. `FilePrep` fields defined in Task 7 Step 3 match the assertions in Task 7 Step 1. `content_type="file"` constant string consistent across Tasks 1/3/4/5/7. `file_b64`/`file_mime`/`file_name` field names consistent across store → enclave → consumer.
