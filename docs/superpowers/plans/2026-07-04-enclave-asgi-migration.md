# Enclave FastAPI + asyncio 迁移实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `backend/enclave_app.py`（2333 行 Flask 单文件）迁移为 `backend/enclave/` 模块化包 + FastAPI/asyncio 混合并发模型，并从全仓删除 flask/flask-compress。

**Architecture:** 所有路由 `async def`；enclave→backend 回环走进程级 `httpx.AsyncClient`；批量 envelope 解密通过 `anyio.to_thread.run_sync` 整批下放线程池（libsodium/cryptography 释放 GIL）；whoami 缓存改 asyncio singleflight；服务方式改为内嵌 gunicorn + enclave 专用 UvicornWorker，TLS 语义由 worker 层的自定义 SSLContext 收口。设计定稿见 `docs/superpowers/specs/2026-07-04-enclave-asgi-migration-design.md`（下称"spec"）。

**Tech Stack:** FastAPI / Starlette、httpx（AsyncClient + ASGITransport 测试）、anyio、gunicorn + uvicorn-worker、PyNaCl、cryptography、pytest（无 pytest-asyncio，async 测试用 `asyncio.run`）。

## Global Constraints

- **Git 硬规则**：本仓用户要求 commit 必须由用户明确授权。各任务的 "Commit" 步骤仅在执行会话已获用户明确授权 commit 时执行；否则该步骤改为"停下，向用户汇报 diff 与测试结果"，工作树保留。
- **错误字符串逐字保留**（spec §2）：`/v1/envelope/decrypt` 用下划线拼法 `missing_api_key` / `cannot_resolve_user_id`；其余 decrypt-and-serve 读路由用空格拼法 `missing api_key` / `cannot resolve user_id`。不许统一。错误体形如 `{"error": "..."}`，状态码映射逐字照旧（401/502/503/403/400/404）。
- **422 禁令**（spec §6）：任何路由签名不得使用会触发 FastAPI 自动校验的类型化 Query/Path/Body 声明；所有输入手工解析 + 显式 `JSONResponse`。Path 参数只允许裸 `str`（frame_id 自己跑正则回 400）。
- **入口命令不变**（spec §1）：`python -u backend/enclave_app.py` 全程可用；compose 不改。
- **新包零 flask**：`backend/enclave/` 任何模块不得 import flask / flask_compress。
- **async 路由红线**（spec §9）：async 路由内禁止同步 httpx 调用、禁止内联重 CPU 循环（解密批处理必须 `anyio.to_thread.run_sync`）。
- **可 mock 约定**：跨模块调用一律写成模块属性形式（`backend_client.backend_get(...)`、`envelope.decrypt_envelope(...)`），禁止 `from x import 函数` 后直呼——测试靠 monkeypatch 模块属性。
- **测试环境**：全套测试需要可达的 throwaway Postgres（conftest 默认 `postgresql://postgres:test@127.0.0.1:55432/postgres`，可用 `FEEDLING_TEST_PG` 覆盖）；不可达时 conftest 会整套 skip——看到大量 skip 先检查 PG 容器。测试命令一律从仓库根目录跑。
- **移动 vs 重写**：标注"逐字移动"的代码从 `backend/enclave_app.py`（当前 HEAD，2333 行）按给出的行号搬运，除了去掉函数名下划线前缀和 import 调整外零改动——这是 review 的锚点（spec §9）。
- **已知有意偏差（执行时保持，不要"修复"）**：
  1. OPTIONS 请求：Flask 自动回 200+Allow，Starlette 回 405+Allow。已确认无探活/客户端依赖 OPTIONS，按 405 锁定并写测试（Task 8）。
  2. `/v1/envelope/decrypt` 收到非对象 JSON body（如数组）：旧代码 AttributeError→500，新代码归一为 400 `envelope required`（Task 9）。
  3. `/image` 的 ETag 从 werkzeug 派生值改为 `sha256(bytes)` 前 32 hex（旧实现对 BytesIO 本就不产生稳定 etag，无兼容负担，Task 13）。

## 文件结构总览

```
backend/enclave_app.py          # Task 15 重写为薄入口（bootstrap + serve）
backend/enclave/
    __init__.py                 # Task 1（空文件）
    config.py                   # Task 1
    keys.py                     # Task 2
    attestation.py              # Task 2
    state.py                    # Task 2
    envelope.py                 # Task 3
    visual.py                   # Task 4
    readside.py                 # Task 4
    backend_client.py           # Task 5
    auth.py                     # Task 6
    routes/
        __init__.py             # Task 8（build_app + lifespan）
        health.py               # Task 8
        envelope.py             # Task 9
        memory.py               # Task 10
        worldbook.py            # Task 10
        chat.py                 # Task 11
        identity.py             # Task 12
        frames.py               # Task 13
    asgi_worker.py              # Task 15
    serving.py                  # Task 15
backend/provider_client.py      # Task 7（追加 async，不动现有函数）
backend/requirements.txt/.lock  # Task 16
tests/test_enclave_*.py         # 各任务新测试 + Task 14 迁移旧 10 个文件
```

旧 `enclave_app.py` 在 Task 1–14 期间**原样保留**（旧测试继续绿），Task 15 才替换。

---

### Task 1: `enclave/config.py`

**Files:**
- Create: `backend/enclave/__init__.py`（空）
- Create: `backend/enclave/config.py`
- Test: `tests/test_enclave_config.py`

**Interfaces:**
- Produces: `ENCLAVE_PORT: int`、`ENCLAVE_TLS: bool`、`FLASK_URL: str`、`RUNTIME_TOKEN_SECRET: bytes`、`SCREEN_VLM_API_KEY/SCREEN_VLM_MODEL/SCREEN_VLM_BASE_URL: str`、`RELEASE: dict`、`APP_AUTH: dict`、`ENCLAVE_THREADS: int`、`enclave_worker_count() -> int`、`env_flag_enabled(name: str, default: str = "false") -> bool`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_config.py
"""enclave.config 单元测试：常量存在性 + 两个纯函数的解析语义。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from enclave import config  # noqa: E402


def test_constants_exist_and_typed():
    assert isinstance(config.ENCLAVE_PORT, int)
    assert isinstance(config.ENCLAVE_TLS, bool)
    assert config.FLASK_URL.startswith("http")
    assert isinstance(config.RUNTIME_TOKEN_SECRET, bytes)
    assert isinstance(config.RELEASE, dict) and "git_commit" in config.RELEASE
    assert isinstance(config.APP_AUTH, dict) and "contract" in config.APP_AUTH
    assert config.ENCLAVE_THREADS >= 1


def test_env_flag_enabled(monkeypatch):
    monkeypatch.setenv("X_FLAG", "TRUE")
    assert config.env_flag_enabled("X_FLAG") is True
    monkeypatch.setenv("X_FLAG", "off")
    assert config.env_flag_enabled("X_FLAG") is False
    monkeypatch.delenv("X_FLAG", raising=False)
    assert config.env_flag_enabled("X_FLAG") is False
    assert config.env_flag_enabled("X_FLAG", default="true") is True


def test_enclave_worker_count(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "")
    assert config.enclave_worker_count() == 1  # CI 注入空串不能崩
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "4")
    assert config.enclave_worker_count() == 4
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "0")
    assert config.enclave_worker_count() == 1  # clamp ≥1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_enclave_config.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'enclave'`）

- [ ] **Step 3: 实现**

`backend/enclave/__init__.py` 内容为一行 docstring：`"""Feedling enclave service package (FastAPI/ASGI)."""`

`backend/enclave/config.py` —— 从 `enclave_app.py` 逐字移动以下内容（含原注释），仅按下表改名：

| 旧（enclave_app.py 行号） | 新 |
|---|---|
| L84-85 `DSTACK_SIMULATOR_ENDPOINT` 空值清洗（import 时副作用） | 原样保留在模块顶部 |
| L87 `ENCLAVE_PORT` | 同名 |
| L99 `ENCLAVE_TLS` | 同名 |
| L105 `FLASK_URL` | 同名（env 名 `FEEDLING_FLASK_URL` 不动） |
| L112 `_RUNTIME_TOKEN_SECRET` | `RUNTIME_TOKEN_SECRET` |
| L159-161 `SCREEN_VLM_*` 三常量 | 同名 |
| L166-178 `RELEASE` | 同名 |
| L184-198 `APP_AUTH` | 同名 |
| L940-941 `_env_flag_enabled` | `env_flag_enabled` |
| L2194 `_ENCLAVE_THREADS` | `ENCLAVE_THREADS`（注释更新：现在是解密线程池容量，见 spec §4） |
| L2197-2206 `_enclave_worker_count` | `enclave_worker_count` |

文件只需要 `import os`。不引入任何其他依赖。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_enclave_config.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit（见 Global Constraints 的 Git 硬规则）**

```bash
git add backend/enclave/__init__.py backend/enclave/config.py tests/test_enclave_config.py
git commit -m "feat(enclave): extract config module for ASGI migration"
```

---

### Task 2: `enclave/keys.py` + `enclave/attestation.py` + `enclave/state.py`

**Files:**
- Create: `backend/enclave/keys.py`
- Create: `backend/enclave/attestation.py`
- Create: `backend/enclave/state.py`
- Test: `tests/test_enclave_keys_attestation.py`

**Interfaces:**
- Consumes: `enclave.config`（Task 1）
- Produces:
  - `keys.CONTENT_KEY_PATH/SIGNING_KEY_PATH: str`、`keys.derive_keys_from_dev_seed() -> dict`、`keys.derive_keys(dstack) -> dict`（keys dict 含 `content_sk/content_pk/content_pk_bytes/signing_sk/signing_pk/signing_pk_bytes`）
  - `keys.get_or_derive_content_sk() -> nacl.public.PrivateKey`（同步，双检锁）、`async keys.get_content_sk() -> nacl.public.PrivateKey`、`keys.set_cached_content_sk(sk) -> None`
  - `attestation.PHASE1_TLS_FINGERPRINT: bytes`、`attestation.build_report_data(content_pk_bytes, tls_cert_fingerprint, version_tag) -> bytes`、`attestation.fetch_quote_and_measurements(dstack, report_data) -> dict`、`attestation.dev_attestation(report_data) -> dict`
  - `state._state: dict`（键集与旧 `enclave_app._state` 完全一致）、`state.bootstrap() -> None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_keys_attestation.py
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from enclave import attestation, keys, state  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_sk_cache(monkeypatch):
    monkeypatch.setattr(keys, "_cached_content_sk", None)


def test_dev_seed_derivation_deterministic(monkeypatch):
    monkeypatch.setenv("FEEDLING_DEV_DSTACK_SEED", "seed-a")
    k1 = keys.derive_keys_from_dev_seed()
    k2 = keys.derive_keys_from_dev_seed()
    assert k1["content_pk_bytes"] == k2["content_pk_bytes"]
    assert len(k1["content_pk_bytes"]) == 32
    monkeypatch.setenv("FEEDLING_DEV_DSTACK_SEED", "seed-b")
    assert keys.derive_keys_from_dev_seed()["content_pk_bytes"] != k1["content_pk_bytes"]


def test_get_content_sk_async_uses_dev_seed(monkeypatch):
    monkeypatch.setenv("FEEDLING_DEV_DSTACK_SEED", "seed-a")
    sk = asyncio.run(keys.get_content_sk())
    assert bytes(sk) == bytes(keys.derive_keys_from_dev_seed()["content_sk"])
    # 第二次拿到缓存的同一对象（不重派生）
    assert asyncio.run(keys.get_content_sk()) is sk


def test_report_data_layout():
    pk = b"\x01" * 32
    rd = attestation.build_report_data(
        content_pk_bytes=pk,
        tls_cert_fingerprint=attestation.PHASE1_TLS_FINGERPRINT,
        version_tag=b"feedling-v1",
    )
    assert len(rd) == 64
    assert rd[32:33] == b"\x01"          # version byte
    assert rd[33:34] == b"\x01"          # flag: placeholder fingerprint
    assert rd[34:] == b"\x00" * 30
    with pytest.raises(ValueError):
        attestation.build_report_data(pk, b"\x00" * 31, b"feedling-v1")


def test_bootstrap_dev_seed_populates_state(monkeypatch):
    monkeypatch.setenv("FEEDLING_DEV_DSTACK_SEED", "seed-boot")
    monkeypatch.setitem(state._state, "ready", False)
    state.bootstrap()
    assert state._state["ready"] is True
    assert state._state["error"] is None
    assert len(state._state["content_pk_hex"]) == 64
    att = state._state["attestation"]
    assert att["app_id"] == "dev-memory-sandbox"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_enclave_keys_attestation.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现**

**`backend/enclave/keys.py`** —— 逐字移动 + 追加 async 包装：

| 旧行号 | 内容 | 新名 |
|---|---|---|
| L204-205 | `CONTENT_KEY_PATH` / `SIGNING_KEY_PATH` | 同名 |
| L210-214 | `_dev_seed_bytes` | 同名（模块私有） |
| L217-234 | `derive_keys_from_dev_seed` | 同名 |
| L237-265 | `derive_keys` | 同名 |
| L2157-2180 | `_get_or_derive_content_sk` + `_cached_content_sk` + `_content_sk_lock` | `get_or_derive_content_sk`（threading.Lock 双检锁原样保留——它会在线程池里被调） |

追加（新代码）：

```python
def set_cached_content_sk(sk: nacl.public.PrivateKey) -> None:
    """bootstrap() 派生完成后注入进程级缓存（旧 _cached_content_sk 赋值的显式接口）。"""
    global _cached_content_sk
    _cached_content_sk = sk


async def get_content_sk() -> nacl.public.PrivateKey:
    """async 路由取 content_sk 的唯一入口。缓存命中直接返回（常态路径，
    bootstrap 已派生）；未命中时经 to_thread 走 get_or_derive_content_sk
    （dstack socket 回环是阻塞 I/O，不能挂在事件循环上）。

    刻意不加 asyncio 锁：模块级 asyncio.Lock 会绑死在首个事件循环上，在
    多 loop 测试环境（每个 asyncio.run 一个新 loop）会炸；而并发首派生
    最多多跑几次**确定性**派生（结果相同），get_or_derive_content_sk 内部
    的 threading.Lock 已把 dstack round-trip 收敛为单次。"""
    if _cached_content_sk is not None:
        return _cached_content_sk
    return await anyio.to_thread.run_sync(get_or_derive_content_sk)
```

imports：`anyio.to_thread`、`hashlib`、`os`、`threading`、`nacl.public`、`nacl.signing`、`from dstack_sdk import DstackClient`。

**`backend/enclave/attestation.py`** —— 纯逐字移动：

| 旧行号 | 内容 |
|---|---|
| L277 | `PHASE1_TLS_FINGERPRINT` |
| L290-305 | `build_report_data` |
| L308-353 | `fetch_quote_and_measurements` |
| L356-374 | `dev_attestation` |

imports：`hashlib`、`os`、`typing.Any`、`from dstack_sdk import DstackClient`（仅类型注释用途可省）。

**`backend/enclave/state.py`** —— 逐字移动 L381-396 `_state` 与 L399-456 `bootstrap()`，改动仅限：

1. `derive_keys_from_dev_seed()` / `derive_keys(dstack)` → `keys.derive_keys_from_dev_seed()` / `keys.derive_keys(dstack)`
2. `global _cached_content_sk` + `_cached_content_sk = keys["content_sk"]`（旧 L407/L443）→ `keys.set_cached_content_sk(derived["content_sk"])`（局部变量改名 `derived` 避免与模块名 `keys` 撞）
3. `build_report_data` / `dev_attestation` / `fetch_quote_and_measurements` / `PHASE1_TLS_FINGERPRINT` → `attestation.` 前缀
4. `ENCLAVE_TLS` → `config.ENCLAVE_TLS`
5. `derive_tls_cert_and_key` 仍 `from dstack_tls import derive_tls_cert_and_key`（backend 根模块，不动）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_enclave_keys_attestation.py tests/test_enclave_config.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit（gated）**

```bash
git add backend/enclave/keys.py backend/enclave/attestation.py backend/enclave/state.py tests/test_enclave_keys_attestation.py
git commit -m "feat(enclave): extract keys/attestation/state modules"
```

---

### Task 3: `enclave/envelope.py`（加密核，纯函数）

**Files:**
- Create: `backend/enclave/envelope.py`
- Test: `tests/test_enclave_envelope_core.py`

**Interfaces:**
- Produces: `class DecryptFailure(Exception)`（`.reason: str`）、`BOX_SEAL_INFO: bytes`、`box_seal_open_hkdf(blob: bytes, recipient_sk_bytes: bytes) -> bytes`、`build_aead_aad(owner_user_id: str, v: int, item_id: str) -> bytes`、`decrypt_envelope(env: dict, authorized_user_id: str, content_sk) -> bytes`
- 本模块**禁止** import fastapi / httpx / flask（纯函数层，spec §3）。

- [ ] **Step 1: 写失败测试（真实 round-trip，seal 侧按 iOS 兼容算法在测试内重实现）**

```python
# tests/test_enclave_envelope_core.py
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import nacl.bindings  # noqa: E402
import nacl.public  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: E402
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402
from cryptography.hazmat.primitives.hashes import SHA256  # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from enclave import envelope as envmod  # noqa: E402


def _seal(recipient_pk: bytes, key32: bytes) -> bytes:
    """iOS 兼容 seal（ContentEncryption.swift / spec §2）：ek_pub||ct||tag。"""
    ek = X25519PrivateKey.generate()
    ek_pub = ek.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = ek.exchange(X25519PublicKey.from_public_bytes(recipient_pk))
    k_wrap = HKDF(algorithm=SHA256(), length=32, salt=None,
                  info=envmod.BOX_SEAL_INFO).derive(shared)
    nonce = hashlib.sha256(ek_pub + recipient_pk).digest()[:12]
    return ek_pub + ChaCha20Poly1305(k_wrap).encrypt(nonce, key32, None)


def _make_envelope(owner: str, item_id: str, body: bytes, recipient_pk: bytes,
                   v: int = 1) -> dict:
    K = os.urandom(32)
    nonce = os.urandom(12)
    aad = f"{owner}|{v}|{item_id}".encode("utf-8")
    ct = nacl.bindings.crypto_aead_chacha20poly1305_ietf_encrypt(body, aad, nonce, K)
    return {
        "id": item_id, "v": v, "owner_user_id": owner,
        "body_ct": base64.b64encode(ct).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "K_enclave": base64.b64encode(_seal(recipient_pk, K)).decode(),
    }


@pytest.fixture()
def sk():
    return nacl.public.PrivateKey.generate()


def test_round_trip(sk):
    env = _make_envelope("usr_a", "itm_1", b'{"hello": 1}', bytes(sk.public_key))
    assert envmod.decrypt_envelope(env, "usr_a", sk) == b'{"hello": 1}'


def test_owner_mismatch_rejected(sk):
    env = _make_envelope("usr_a", "itm_1", b"x", bytes(sk.public_key))
    with pytest.raises(envmod.DecryptFailure) as ei:
        envmod.decrypt_envelope(env, "usr_b", sk)
    assert "owner mismatch" in ei.value.reason


def test_tampered_ciphertext_rejected(sk):
    env = _make_envelope("usr_a", "itm_1", b"x", bytes(sk.public_key))
    raw = bytearray(base64.b64decode(env["body_ct"]))
    raw[0] ^= 0xFF
    env["body_ct"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(envmod.DecryptFailure) as ei:
        envmod.decrypt_envelope(env, "usr_a", sk)
    assert "AEAD verify" in ei.value.reason


def test_aad_binds_item_id(sk):
    # id 变了 → AAD 不匹配 → 拒绝（防跨条目替换）
    env = _make_envelope("usr_a", "itm_1", b"x", bytes(sk.public_key))
    env["id"] = "itm_2"
    with pytest.raises(envmod.DecryptFailure):
        envmod.decrypt_envelope(env, "usr_a", sk)


def test_missing_field_rejected(sk):
    env = _make_envelope("usr_a", "itm_1", b"x", bytes(sk.public_key))
    env.pop("nonce")
    with pytest.raises(envmod.DecryptFailure) as ei:
        envmod.decrypt_envelope(env, "usr_a", sk)
    assert "missing nonce" in ei.value.reason


def test_module_is_pure():
    import enclave.envelope as m
    src = Path(m.__file__).read_text()
    for banned in ("import flask", "import httpx", "import fastapi"):
        assert banned not in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_enclave_envelope_core.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现**

`backend/enclave/envelope.py` —— 逐字移动，仅改名：

| 旧行号 | 内容 | 新名 |
|---|---|---|
| L679-683 | `DecryptFailure` | 同名 |
| L685 | `_BOX_SEAL_INFO` | `BOX_SEAL_INFO` |
| L688-726 | `_box_seal_open_hkdf` | `box_seal_open_hkdf` |
| L669-676 | `_build_aead_aad` | `build_aead_aad` |
| L729-778 | `_decrypt_envelope` | `decrypt_envelope` |

imports 只保留函数体实际用到的：`base64`、`hashlib`、`nacl.bindings`、`nacl.exceptions`、`nacl.public`（类型注释）、cryptography 的 X25519/ChaCha20Poly1305/HKDF/SHA256/serialization/InvalidTag。模块 docstring 注明"纯同步无 I/O，路由必须经 to_thread 调用批量解密"。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_enclave_envelope_core.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit（gated）**

```bash
git add backend/enclave/envelope.py tests/test_enclave_envelope_core.py
git commit -m "feat(enclave): extract pure envelope crypto core"
```

---

### Task 4: `enclave/visual.py` + `enclave/readside.py`（纯函数层）

**Files:**
- Create: `backend/enclave/visual.py`
- Create: `backend/enclave/readside.py`
- Test: `tests/test_enclave_visual_readside_units.py`

**Interfaces:**
- Consumes: `enclave.envelope`（`decrypt_envelope`/`DecryptFailure`）、`enclave.config.env_flag_enabled`
- Produces:
  - `visual.raw_image_mime(data: bytes) -> str | None`、`visual.IMAGE_EXTENSION_BY_MIME: dict`、`visual.parse_visual_plaintext(plaintext: bytes) -> dict`
  - `readside.memory_readside_for_model_api_enabled() -> bool`、`readside.memory_readside_model_api_limit() -> int`、`readside.memory_readside_effective_limit(raw_limit=None) -> int`
  - `readside.memory_inner_to_v1(inner, envelope=None) -> dict`、`readside.build_memory_index_item(envelope, inner) -> dict`、`readside.build_memory_fetch_item(envelope, inner) -> dict`、`readside.memory_index_filter_items(items, payload) -> list`
  - `readside.decrypt_readside_items(moments, authorized_user_id, content_sk, *, item_builder) -> tuple[list, list]`（同步纯计算——路由在 to_thread 里调）
  - `readside.moments_to_cards(moments: list, authorized_user_id: str, content_sk) -> list[dict]`（旧 `_load_decrypted_moments` 去掉 backend 拉取后的解密+成卡部分）
  - `readside.select_context_memories_via_readside(moments, latest_user_text, *, cap=8) -> tuple[list, dict]`、`readside.context_moment_to_index_item(moment) -> dict`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_visual_readside_units.py
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from enclave import readside, visual  # noqa: E402


def test_raw_image_mime_signatures():
    assert visual.raw_image_mime(b"\xff\xd8\xff" + b"0" * 16) == "image/jpeg"
    assert visual.raw_image_mime(b"\x89PNG\r\n\x1a\n" + b"0" * 8) == "image/png"
    assert visual.raw_image_mime(b"RIFF0000WEBP") == "image/webp"
    assert visual.raw_image_mime(b"not an image") is None


def test_parse_visual_plaintext_json_wrapper():
    inner = {"image": "abc", "ocr_text": "hi"}
    out = visual.parse_visual_plaintext(json.dumps(inner).encode())
    assert out["ocr_text"] == "hi"


def test_parse_visual_plaintext_raw_photo_fallback():
    jpeg = b"\xff\xd8\xff" + b"j" * 32
    out = visual.parse_visual_plaintext(jpeg)
    assert out["image_mime"] == "image/jpeg"
    assert base64.b64decode(out["image"]) == jpeg


def test_parse_visual_plaintext_garbage_fails_closed():
    with pytest.raises(Exception):
        visual.parse_visual_plaintext(b"\x00\x01 garbage not json not image")


def test_readside_effective_limit(monkeypatch):
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_LIMIT", raising=False)
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", raising=False)
    assert readside.memory_readside_effective_limit() == 50
    assert readside.memory_readside_effective_limit(0) == 1000  # 0 = full window, hard cap
    assert readside.memory_readside_effective_limit(7) == 7
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "100")
    assert readside.memory_readside_effective_limit(0) == 100


def test_memory_inner_to_v1_passthrough_and_legacy():
    v1 = {"summary": "s", "content": "c", "bucket": "b", "threads": ["t"]}
    assert readside.memory_inner_to_v1(dict(v1))["bucket"] == "b"
    legacy = {"title": "旧标题", "description": "描述", "type": "moment"}
    adapted = readside.memory_inner_to_v1(legacy)
    assert adapted["bucket"] == "我们的关系"
    assert "描述" in adapted["content"]


def test_memory_index_filter_items():
    items = [{"bucket": "a", "threads": ["x"]}, {"bucket": "b", "threads": []}]
    assert len(readside.memory_index_filter_items(items, {"bucket": "a"})) == 1
    assert len(readside.memory_index_filter_items(items, {"thread": "x"})) == 1
    assert len(readside.memory_index_filter_items(items, {})) == 2


def test_decrypt_readside_items_skips_local_only(monkeypatch):
    # 不做真解密：patch decrypt_envelope，验证 local_only / 缺 K_enclave 分流。
    from enclave import envelope as envmod
    monkeypatch.setattr(
        envmod, "decrypt_envelope",
        lambda env, uid, sk: json.dumps({"summary": "s", "content": "c",
                                         "bucket": "b", "threads": []}).encode())
    moments = [
        {"id": "m1", "K_enclave": "x", "visibility": "shared"},
        {"id": "m2", "visibility": "local_only"},
        {"id": "m3"},  # 无 K_enclave
    ]
    items, unavailable = readside.decrypt_readside_items(
        moments, "usr_a", object(), item_builder=readside.build_memory_fetch_item)
    assert [i["id"] for i in items] == ["m1"]
    assert unavailable == ["m2", "m3"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_enclave_visual_readside_units.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现**

**`backend/enclave/visual.py`** —— 逐字移动：L1802-1816 `_raw_image_mime`→`raw_image_mime`；L1819-1825 `_IMAGE_EXTENSION_BY_MIME`→`IMAGE_EXTENSION_BY_MIME`；L1828-1849 `_parse_visual_plaintext`→`parse_visual_plaintext`。imports：`base64`、`json`、`typing.Any`。

**`backend/enclave/readside.py`** —— 逐字移动 + 内部引用改名：

| 旧行号 | 内容 | 新名 |
|---|---|---|
| L944-988 | readside 开关/limit 函数族（`_memory_readside_for_model_api_enabled` 等 4 个） | 去 `_` 前缀；`_env_flag_enabled` 改调 `config.env_flag_enabled` |
| L1094-1104 | `_memory_readside_text` / `_memory_readside_list` | 去前缀 |
| L1106-1120 | `_memory_readside_summary` / `_memory_default_bucket` | 去前缀 |
| L1123-1165 | `_memory_inner_to_v1` | `memory_inner_to_v1` |
| L1168-1197 | `bucket_refs` / `salience` / `status` / `is_sensitive` 四助手 | 去前缀 |
| L1200-1232 | `_build_memory_index_item` / `_build_memory_fetch_item` | 去前缀 |
| L1235-1245 | `_memory_index_filter_items` | `memory_index_filter_items` |
| L1272-1299 | `_memory_readside_decrypt_items` | `decrypt_readside_items`；`_decrypt_envelope`→`envelope.decrypt_envelope`、`DecryptFailure`→`envelope.DecryptFailure` |
| L991-1016 | `_context_moment_to_index_item` | `context_moment_to_index_item` |
| L1019-1091 | `_select_context_memories_via_readside` | `select_context_memories_via_readside` |
| L900-937 | `_load_decrypted_moments` **去掉开头的 `_flask_get` 拉取**（try/except httpx 那 6 行删除，函数签名改为直接收 `moments: list`） | `moments_to_cards(moments, authorized_user_id, content_sk) -> list[dict]` |

`moments_to_cards` 改后的完整代码：

```python
def moments_to_cards(moments: list, authorized_user_id: str, content_sk) -> list[dict]:
    """把 /v1/memory/list 的 envelope 列表解密成 context_memories 明文卡。
    失败（local_only、解密错）静默丢弃——context_memories 是 best-effort。
    纯同步计算：调用方负责放进 to_thread（backend 拉取已上移到路由层）。"""
    out: list[dict] = []
    for m in moments or []:
        if m.get("visibility") == "local_only":
            continue  # enclave doesn't have K_enclave for these
        try:
            plaintext = envelope.decrypt_envelope(m, authorized_user_id, content_sk)
            inner = json.loads(plaintext.decode("utf-8"))
        except (envelope.DecryptFailure, json.JSONDecodeError):
            continue
        out.append({
            "id": m.get("id"),
            "title": inner.get("title"),
            "description": inner.get("description"),
            "type": inner.get("type"),
            "source": m.get("source"),
            "occurred_at": m.get("occurred_at"),
            "created_at": m.get("created_at"),
            "her_quote": inner.get("her_quote"),
            "context": inner.get("context"),
            "linked_dimension": inner.get("linked_dimension"),
        })
    return out
```

`select_context_memories_via_readside` 里的 `select_memory_index_items` 仍 `from memory_index_selector import select_memory_index_items`（backend 根模块不动）。imports：`json`、`from enclave import config, envelope`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_enclave_visual_readside_units.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit（gated）**

```bash
git add backend/enclave/visual.py backend/enclave/readside.py tests/test_enclave_visual_readside_units.py
git commit -m "feat(enclave): extract visual + memory-readside pure layers"
```

---

### Task 5: `enclave/backend_client.py`（async 回环客户端）

**Files:**
- Create: `backend/enclave/backend_client.py`
- Test: `tests/test_enclave_backend_client.py`

**Interfaces:**
- Consumes: `enclave.config.FLASK_URL`
- Produces: `get_async_client() -> httpx.AsyncClient`、`async aclose() -> None`、`forward_auth_headers(api_key: str, runtime_token: str) -> dict`、`async backend_get(path: str, headers: dict, params: dict | None = None) -> dict`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_backend_client.py
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from enclave import backend_client  # noqa: E402


def test_forward_auth_headers_priority():
    assert backend_client.forward_auth_headers("ak", "rt") == {"X-Feedling-Runtime-Token": "rt"}
    assert backend_client.forward_auth_headers("ak", "") == {"X-API-Key": "ak"}
    assert backend_client.forward_auth_headers("", "") == {}


def test_backend_get_roundtrip(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"user_id": "usr_1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(backend_client, "_client", client)
    out = asyncio.run(backend_client.backend_get(
        "/v1/users/whoami", {"X-API-Key": "k"}, params={"a": "1"}))
    assert out == {"user_id": "usr_1"}
    assert seen["url"].endswith("/v1/users/whoami?a=1")
    assert seen["headers"]["x-api-key"] == "k"


def test_backend_get_raises_on_http_status(monkeypatch):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    monkeypatch.setattr(backend_client, "_client", client)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(backend_client.backend_get("/v1/users/whoami", {}))


def test_aclose_resets_singleton(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    monkeypatch.setattr(backend_client, "_client", client)
    asyncio.run(backend_client.aclose())
    assert backend_client._client is None
    assert client.is_closed
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_enclave_backend_client.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现（完整代码）**

```python
# backend/enclave/backend_client.py
"""enclave→backend 回环的进程级 async HTTP 客户端。

旧 enclave_app._http_client（同步 keep-alive 池）的 ASGI 版：同一份连接池
参数，换成 httpx.AsyncClient。单事件循环内 get_async_client 的检查-创建
无竞态（无 await 切换点）；lifespan 退出时 aclose()。

auth 转发语义（旧 _forward_auth_headers，逐字保留）：runtime token 优先，
其次 api_key，两者皆无 → 空 headers。"""

from __future__ import annotations

import httpx

from enclave import config

_client: httpx.AsyncClient | None = None


def get_async_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=15,
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=90.0,
            ),
        )
    return _client


async def aclose() -> None:
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        await client.aclose()


def forward_auth_headers(api_key: str, runtime_token: str) -> dict:
    if runtime_token:
        return {"X-Feedling-Runtime-Token": runtime_token}
    if api_key:
        return {"X-API-Key": api_key}
    return {}


async def backend_get(path: str, headers: dict, params: dict | None = None) -> dict:
    """GET backend 并返回 JSON。调用方负责 httpx 异常→错误码映射
    （HTTPStatusError 401→401、其余→502；HTTPError→502，逐字沿用旧路由）。"""
    r = await get_async_client().get(
        f"{config.FLASK_URL}{path}", params=params, headers=headers
    )
    r.raise_for_status()
    return r.json()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_enclave_backend_client.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit（gated）**

```bash
git add backend/enclave/backend_client.py tests/test_enclave_backend_client.py
git commit -m "feat(enclave): async backend loop client"
```

---

### Task 6: `enclave/auth.py`（AuthContext + async whoami 缓存/singleflight）

**Files:**
- Create: `backend/enclave/auth.py`
- Test: `tests/test_enclave_auth_async.py`

**Interfaces:**
- Consumes: `backend_client.backend_get/forward_auth_headers`（Task 5）、`config.RUNTIME_TOKEN_SECRET`、`core.runtime_token`
- Produces:
  - `@dataclass(frozen=True) AuthContext(api_key: str, runtime_token: str)`，属性 `forward_headers -> dict`、`missing -> bool`
  - `extract_auth(request) -> AuthContext`（X-API-Key / Bearer / ?key= / X-Feedling-Runtime-Token）
  - `local_user_id_from_token(runtime_token: str) -> str | None`
  - `async whoami_live(ctx) -> dict`（无缓存；envelope 路由用）
  - `async whoami_cached(ctx) -> dict`（本地 HMAC 快路径 → 30s TTL 缓存 → per-key singleflight → backend）
  - `async resolve_read_caller(ctx) -> tuple[str | None, tuple[dict, int] | None]`（读路由共享的 auth 前置：返回 user_id 或 (错误体, 状态码)，空格拼法错误串）
  - `WHOAMI_CACHE_TTL: float`、`reset_cache() -> None`（测试用）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_auth_async.py
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from core import runtime_token as rt_token  # noqa: E402
from enclave import auth, backend_client, config  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    auth.reset_cache()
    yield
    auth.reset_cache()


def _patch_backend(monkeypatch, calls, result=None, delay=0.0, exc=None):
    async def fake_backend_get(path, headers, params=None):
        calls.append({"path": path, "headers": dict(headers or {})})
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        return result if result is not None else {"user_id": "usr_1"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)


def test_singleflight_collapses_concurrent_misses(monkeypatch):
    calls = []
    _patch_backend(monkeypatch, calls, delay=0.05)
    ctx = auth.AuthContext(api_key="k1", runtime_token="")

    async def main():
        return await asyncio.gather(*[auth.whoami_cached(ctx) for _ in range(10)])

    results = asyncio.run(main())
    assert all(r == {"user_id": "usr_1"} for r in results)
    assert len(calls) == 1  # 10 并发冷 miss 收敛为 1 次回环


def test_cache_hit_and_ttl_expiry(monkeypatch):
    calls = []
    _patch_backend(monkeypatch, calls)
    ctx = auth.AuthContext(api_key="k1", runtime_token="")
    asyncio.run(auth.whoami_cached(ctx))
    asyncio.run(auth.whoami_cached(ctx))
    assert len(calls) == 1
    monkeypatch.setattr(auth, "WHOAMI_CACHE_TTL", 0.0)
    asyncio.run(auth.whoami_cached(ctx))
    assert len(calls) == 2


def test_local_runtime_token_fast_path(monkeypatch):
    calls = []
    _patch_backend(monkeypatch, calls)
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", b"s3cret")
    tok = rt_token.mint(b"s3cret", user_id="usr_9",
                        runtime_instance_id="ri_1", scope=["read"])
    ctx = auth.AuthContext(api_key="", runtime_token=tok)
    assert asyncio.run(auth.whoami_cached(ctx)) == {"user_id": "usr_9"}
    assert asyncio.run(auth.whoami_live(ctx)) == {"user_id": "usr_9"}
    assert calls == []  # 全程零回环


def test_bad_local_token_falls_back_to_backend(monkeypatch):
    calls = []
    _patch_backend(monkeypatch, calls)
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", b"s3cret")
    ctx = auth.AuthContext(api_key="", runtime_token="not-a-valid-token")
    assert asyncio.run(auth.whoami_cached(ctx)) == {"user_id": "usr_1"}
    assert len(calls) == 1
    assert calls[0]["headers"] == {"X-Feedling-Runtime-Token": "not-a-valid-token"}


def test_error_flight_not_cached(monkeypatch):
    calls = []
    req = httpx.Request("GET", "http://b/v1/users/whoami")
    err = httpx.HTTPStatusError("e", request=req,
                                response=httpx.Response(500, request=req))
    _patch_backend(monkeypatch, calls, exc=err)
    ctx = auth.AuthContext(api_key="k1", runtime_token="")
    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(auth.whoami_cached(ctx))
    assert len(calls) == 2  # 失败不落缓存，下次重试


def test_resolve_read_caller_error_strings(monkeypatch):
    from enclave import state
    monkeypatch.setitem(state._state, "ready", True)
    ctx = auth.AuthContext(api_key="", runtime_token="")
    user_id, error = asyncio.run(auth.resolve_read_caller(ctx))
    assert user_id is None
    assert error == ({"error": "missing api_key"}, 401)  # 空格拼法，勿改

    calls = []
    _patch_backend(monkeypatch, calls, result={"user_id": ""})
    ctx = auth.AuthContext(api_key="k", runtime_token="")
    user_id, error = asyncio.run(auth.resolve_read_caller(ctx))
    assert error == ({"error": "cannot resolve user_id"}, 401)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_enclave_auth_async.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现（完整代码）**

```python
# backend/enclave/auth.py
"""调用方身份解析（ASGI 版）。

旧 enclave_app 的 _extract_api_key/_caller_runtime_token/_local_user_id_from_token/
_whoami_cached 的 async 重写。语义保持（spec §2/§4）：
  - runtime token 本地 HMAC 校验是快路径（纯计算，事件循环内联）；
  - whoami 缓存 TTL 30s，仅供只读 decrypt-and-serve 路由；
  - singleflight：同 key 并发冷 miss 收敛为一次回环（asyncio.Future 版）；
  - /v1/envelope/decrypt 走 whoami_live（绝不走缓存）。
AuthContext 显式携带凭证（spec §4 硬约束）：token 不再藏在 request 全局，
所有 backend 转发都显式传 ctx.forward_headers。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

import httpx
from starlette.requests import Request

from core import runtime_token as rt_token
from enclave import backend_client, config


@dataclass(frozen=True)
class AuthContext:
    api_key: str
    runtime_token: str

    @property
    def forward_headers(self) -> dict:
        return backend_client.forward_auth_headers(self.api_key, self.runtime_token)

    @property
    def missing(self) -> bool:
        return not self.api_key and not self.runtime_token


def extract_auth(request: Request) -> AuthContext:
    """X-API-Key / Bearer / ?key=（legacy，泄漏进日志，仅兼容）+ runtime token。"""
    api_key = (request.headers.get("X-API-Key") or "").strip()
    if not api_key:
        authz = (request.headers.get("Authorization") or "").strip()
        if authz.lower().startswith("bearer "):
            api_key = authz[7:].strip()
    if not api_key:
        api_key = (request.query_params.get("key") or "").strip()
    runtime_token = (request.headers.get("X-Feedling-Runtime-Token") or "").strip()
    return AuthContext(api_key=api_key, runtime_token=runtime_token)


def local_user_id_from_token(runtime_token: str) -> str | None:
    """本地 HMAC 校验 runtime token（旧 _local_user_id_from_token 逐字语义）。
    secret 未配置 / token 无效 → None，调用方回退 backend 解析，绝不硬失败。"""
    if not (runtime_token and config.RUNTIME_TOKEN_SECRET):
        return None
    try:
        claims = rt_token.verify(config.RUNTIME_TOKEN_SECRET, runtime_token)
    except rt_token.TokenError:
        return None
    return claims.get("user_id") or None


WHOAMI_CACHE_TTL = 30.0
_whoami_cache: dict[str, tuple[float, dict]] = {}
_whoami_inflight: dict[str, asyncio.Future] = {}


def reset_cache() -> None:
    _whoami_cache.clear()
    _whoami_inflight.clear()


def _prune_whoami_cache(now: float) -> None:
    for h in [h for h, (ts, _) in _whoami_cache.items()
              if now - ts >= WHOAMI_CACHE_TTL]:
        _whoami_cache.pop(h, None)


async def whoami_live(ctx: AuthContext) -> dict:
    """每次实时解析（/v1/envelope/decrypt 专用——缓存会把刚吊销的 key 多放行
    最多 TTL 秒）。本地 token 校验允许：吊销延迟以 token TTL（≤15min）为界，
    与旧实现一致。"""
    local_uid = local_user_id_from_token(ctx.runtime_token)
    if local_uid:
        return {"user_id": local_uid}
    return await backend_client.backend_get("/v1/users/whoami", ctx.forward_headers)


async def whoami_cached(ctx: AuthContext) -> dict:
    local_uid = local_user_id_from_token(ctx.runtime_token)
    if local_uid:
        return {"user_id": local_uid}
    cred = ("rt:" + ctx.runtime_token) if ctx.runtime_token else ("ak:" + ctx.api_key)
    h = hashlib.sha256(cred.encode("utf-8")).hexdigest()

    hit = _whoami_cache.get(h)
    if hit is not None and time.monotonic() - hit[0] < WHOAMI_CACHE_TTL:
        return hit[1]

    inflight = _whoami_inflight.get(h)
    if inflight is not None:
        # shield：等待者被取消不连坐领跑者的 flight
        return await asyncio.shield(inflight)

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _whoami_inflight[h] = fut
    try:
        whoami = await backend_client.backend_get(
            "/v1/users/whoami", ctx.forward_headers)
        if isinstance(whoami, dict) and whoami.get("user_id"):
            now = time.monotonic()
            _whoami_cache[h] = (now, whoami)
            _prune_whoami_cache(now)
        fut.set_result(whoami)
        return whoami
    except BaseException as e:  # 含 CancelledError：领跑者倒下要放行等待者
        fut.set_exception(e)
        fut.exception()  # 标记已取，无等待者时不在 GC 期刷警告
        raise
    finally:
        if _whoami_inflight.get(h) is fut:
            _whoami_inflight.pop(h, None)


async def resolve_read_caller(ctx: AuthContext):
    """decrypt-and-serve 读路由共享的 auth 前置（旧 _memory_readside_auth_context
    的 auth 部分）。返回 (user_id, None) 或 (None, (错误体, 状态码))。
    错误字符串为空格拼法（spec §2）——与 /v1/envelope/decrypt 的下划线拼法
    是历史上并存的两套，禁止统一。"""
    from enclave import state  # 延迟 import 避免环
    if not state._state["ready"]:
        return None, ({"error": "not_ready", "detail": state._state["error"]}, 503)
    if ctx.missing:
        return None, ({"error": "missing api_key"}, 401)
    try:
        whoami = await whoami_cached(ctx)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return None, ({"error": "unauthorized"}, 401)
        return None, ({"error": f"backend_error: {e}"}, 502)
    except httpx.HTTPError as e:
        return None, ({"error": f"backend_unreachable: {e}"}, 502)
    user_id = whoami.get("user_id", "")
    if not user_id:
        return None, ({"error": "cannot resolve user_id"}, 401)
    return user_id, None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_enclave_auth_async.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit（gated）**

```bash
git add backend/enclave/auth.py tests/test_enclave_auth_async.py
git commit -m "feat(enclave): AuthContext + async whoami cache with singleflight"
```

---

### Task 7: `provider_client.chat_completion_async`

**Files:**
- Modify: `backend/provider_client.py`（只追加，不改任何现有函数）
- Test: `tests/test_provider_client_async.py`

**Interfaces:**
- Consumes: 现有 `validate_config/_runtime_model/_headers/_raise_for_provider_status/_extract_reply/_extract_openai_compatible_reasoning/_extract_openai_compatible_stop_reason/_openai_uses_responses_for_reasoning/ProviderConfig/ProviderError/chat_completion`
- Produces: `async chat_completion_async(config, messages, *, max_tokens=700, temperature=0.7, timeout=60.0, response_format=None, require_reply=True, include_reasoning=False) -> dict`（返回 dict 形状与 `chat_completion` 一致）、`async aclose_async_http_client() -> None`、`_async_http_client() -> httpx.AsyncClient`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_provider_client_async.py
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx  # noqa: E402
import pytest  # noqa: E402

import provider_client  # noqa: E402


def _mock_async_client(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(provider_client, "_shared_async_client", client)
    return client


def test_openrouter_wire_async(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "gen-1",
            "choices": [{"message": {"content": "a caption"},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 10},
        })

    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="openrouter", model="qwen/qwen3-vl-8b-instruct",
        api_key="or-key", base_url="https://openrouter.ai/api/v1")
    out = asyncio.run(provider_client.chat_completion_async(
        cfg, [{"role": "user", "content": "hi"}], max_tokens=160, timeout=45.0))
    assert out["reply"] == "a caption"
    assert out["provider"] == "openrouter"
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["max_tokens"] == 160
    assert seen["body"]["stream"] is False


def test_provider_error_on_http_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)
    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="openrouter", model="m", api_key="k",
        base_url="https://openrouter.ai/api/v1")
    with pytest.raises(provider_client.ProviderError):
        asyncio.run(provider_client.chat_completion_async(
            cfg, [{"role": "user", "content": "hi"}]))


def test_missing_key_raises():
    cfg = provider_client.ProviderConfig(provider="openrouter", model="m", api_key="")
    with pytest.raises(provider_client.ProviderError):
        asyncio.run(provider_client.chat_completion_async(
            cfg, [{"role": "user", "content": "hi"}]))


def test_non_openai_wire_bridges_to_sync(monkeypatch):
    called = {}

    def fake_sync(config, messages, **kw):
        called["provider"] = config.provider
        return {"reply": "from-sync"}

    monkeypatch.setattr(provider_client, "chat_completion", fake_sync)
    cfg = provider_client.ProviderConfig(
        provider="anthropic", model="claude-sonnet-5", api_key="k")
    out = asyncio.run(provider_client.chat_completion_async(
        cfg, [{"role": "user", "content": "hi"}]))
    assert out == {"reply": "from-sync"}
    assert called["provider"] == "anthropic"


def test_aclose_async_http_client(monkeypatch):
    client = _mock_async_client(monkeypatch, lambda r: httpx.Response(200))
    asyncio.run(provider_client.aclose_async_http_client())
    assert provider_client._shared_async_client is None
    assert client.is_closed
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_provider_client_async.py -v`
Expected: FAIL（`AttributeError: ... has no attribute 'chat_completion_async'`）

- [ ] **Step 3: 实现（追加到 provider_client.py 末尾，完整代码）**

```python
# --- async variant (enclave ASGI migration) --------------------------------
# 只有 openai-wire（openai 非 responses / openrouter / deepseek /
# openai_compatible）有原生 async 实现——enclave caption 走 openrouter，这是
# 唯一需要"45s 长等待只挂协程"的调用方。anthropic / gemini / openai-responses
# 的编解码保持单实现（同步版），经 anyio 线程桥调用，避免双份 wire codec 漂移。
# 同步 chat_completion 与异步版各用各的 httpx client，绝不混用（spec §4）。

_shared_async_client: httpx.AsyncClient | None = None


def _async_http_client() -> httpx.AsyncClient:
    global _shared_async_client
    if _shared_async_client is None or _shared_async_client.is_closed:
        _shared_async_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=90.0,
            ),
        )
    return _shared_async_client


async def aclose_async_http_client() -> None:
    global _shared_async_client
    client, _shared_async_client = _shared_async_client, None
    if client is not None and not client.is_closed:
        await client.aclose()


async def chat_completion_async(
    config: ProviderConfig,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 700,
    temperature: float = 0.7,
    timeout: float = 60.0,
    response_format: dict[str, Any] | None = None,
    require_reply: bool = True,
    include_reasoning: bool = False,
) -> dict[str, Any]:
    provider, model, base_url = validate_config(
        config.provider, config.model, config.base_url
    )
    request_model, extra_body = _runtime_model(provider, model)
    key = (config.api_key or "").strip()
    if not key:
        raise ProviderError("api_key required")

    if provider in ("anthropic", "gemini") or (
        provider == "openai" and _openai_uses_responses_for_reasoning(request_model)
    ):
        import anyio.to_thread
        from functools import partial

        return await anyio.to_thread.run_sync(partial(
            chat_completion, config, messages,
            max_tokens=max_tokens, temperature=temperature, timeout=timeout,
            response_format=response_format, require_reply=require_reply,
            include_reasoning=include_reasoning,
        ))

    # 以下为 _chat_completion_openai_compatible 的 async 镜像（含 openrouter
    # reasoning 400/422 降级重试）。改同步版时必须同步改这里 —— 两处有同一个
    # payload/降级契约。
    payload: dict[str, Any] = {
        "model": request_model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max(1, min(int(max_tokens), 8192)),
    }
    if response_format:
        payload["response_format"] = response_format
    if extra_body:
        payload.update(extra_body)
    if include_reasoning and provider == "openrouter":
        payload.setdefault("reasoning", {"enabled": True, "exclude": False})

    async def post_with_payload(request_payload: dict[str, Any]) -> httpx.Response:
        try:
            return await _async_http_client().post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=_headers(ProviderConfig(provider, request_model, key, base_url)),
                json=request_payload,
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"provider network error: {type(e).__name__}") from e

    resp = await post_with_payload(payload)
    try:
        _raise_for_provider_status(resp)
    except ProviderError:
        if (include_reasoning and provider == "openrouter"
                and resp.status_code in {400, 422} and "reasoning" in payload):
            fallback_payload = dict(payload)
            fallback_payload.pop("reasoning", None)
            resp = await post_with_payload(fallback_payload)
            _raise_for_provider_status(resp)
        else:
            raise

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")

    return {
        "reply": _extract_reply(body, required=require_reply),
        "reasoning": _extract_openai_compatible_reasoning(body),
        "usage": body.get("usage") if isinstance(body.get("usage"), dict) else {},
        "raw_id": body.get("id", ""),
        "stop_reason": _extract_openai_compatible_stop_reason(body),
        "provider": provider,
        "model": model,
    }
```

（注意与同步版的一处已知差异要对齐检查：同步 `_chat_completion_openai_compatible` 的 `_headers` 用 `model` 而非 `request_model` 构造 ProviderConfig——headers 不含 model，无行为差异，但执行时以同步版实参为准逐项核对。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_provider_client_async.py -v`
Expected: 5 PASS

- [ ] **Step 5: 跑 provider_client 既有测试确认无回归**

Run: `python -m pytest tests/ -q -k "provider"`
Expected: 全 PASS

- [ ] **Step 6: Commit（gated）**

```bash
git add backend/provider_client.py tests/test_provider_client_async.py
git commit -m "feat(provider): async chat_completion for enclave caption path"
```

---

### Task 8: `routes/__init__.py`（build_app + lifespan）+ `routes/health.py`

**Files:**
- Create: `backend/enclave/routes/__init__.py`
- Create: `backend/enclave/routes/health.py`
- Test: `tests/test_enclave_routes_health.py`

**Interfaces:**
- Consumes: `state._state`、`config.RELEASE/APP_AUTH/ENCLAVE_THREADS`、`backend_client.aclose`、`provider_client.aclose_async_http_client`
- Produces: `build_app() -> FastAPI`（后续所有路由任务把 router 挂进 `_ROUTE_MODULES`；Task 8 时元组暂为 `("health",)`，每个路由任务往里加自己的模块名）；`health.router: APIRouter`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_routes_health.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    return _AsgiTestClient(build_app())


def test_healthz_ready(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "ready": True}


def test_healthz_not_ready(monkeypatch, client):
    monkeypatch.setitem(enclave_state._state, "ready", False)
    monkeypatch.setitem(enclave_state._state, "error", "boom")
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.get_json() == {"ok": False, "ready": False, "error": "boom"}


def test_attestation_shape_and_cache_header(monkeypatch, client):
    monkeypatch.setitem(enclave_state._state, "content_pk_hex", "aa" * 32)
    monkeypatch.setitem(enclave_state._state, "signing_pk_hex", "bb" * 32)
    monkeypatch.setitem(enclave_state._state, "booted_at", 123.0)
    monkeypatch.setitem(enclave_state._state, "attestation", {
        "tdx_quote_hex": "cc" * 64, "event_log_json": "[]",
        "measurements": {"mrtd": "00"}, "compose_hash": "h",
        "app_id": "app", "instance_id": "inst",
    })
    r = client.get("/attestation")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=60"
    body = r.get_json()
    for field in ("tdx_quote_hex", "enclave_content_pk_hex", "enclave_release",
                  "app_auth", "report_data_version", "phase", "tls_in_enclave",
                  "mcp_tls_cert_pubkey_fingerprint_hex", "notes", "booted_at"):
        assert field in body


def test_attestation_not_ready(monkeypatch, client):
    monkeypatch.setitem(enclave_state._state, "ready", False)
    monkeypatch.setitem(enclave_state._state, "error", "kms down")
    r = client.get("/attestation")
    assert r.status_code == 503
    assert r.get_json() == {"error": "not_ready", "detail": "kms down"}


def test_gzip_on_large_response(monkeypatch, client):
    monkeypatch.setitem(enclave_state._state, "content_pk_hex", "aa" * 32)
    monkeypatch.setitem(enclave_state._state, "signing_pk_hex", "bb" * 32)
    monkeypatch.setitem(enclave_state._state, "booted_at", 1.0)
    monkeypatch.setitem(enclave_state._state, "attestation", {
        "tdx_quote_hex": "ab" * 8000,  # 16KB，远超 500B 阈值
        "event_log_json": "[]", "measurements": {}, "compose_hash": "h",
        "app_id": "a", "instance_id": "i",
    })
    r = client.get("/attestation", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert len(r.get_json()["tdx_quote_hex"]) == 32000  # httpx 已自动解压


def test_head_and_options_behavior(client):
    # HEAD：Starlette 对 GET 路由自动支持（探活工具依赖，锁定）。
    assert client.open("/healthz", method="HEAD").status_code == 200
    # OPTIONS：有意偏差（Global Constraints #1）—— Flask 自动 200，新栈 405+Allow。
    r = client.open("/healthz", method="OPTIONS")
    assert r.status_code == 405
    assert "GET" in r.headers.get("allow", "")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_enclave_routes_health.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 实现（完整代码）**

```python
# backend/enclave/routes/__init__.py
"""enclave FastAPI 组装（assembly-only，逻辑在各路由模块）。"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

from enclave import backend_client, config

# 每个路由任务落地时把模块名加进来（Task 9-13）。
_ROUTE_MODULES = ("health",)


@asynccontextmanager
async def lifespan(app):
    # 解密线程池容量（spec §4）：anyio 默认全局 40 tokens；这里的池保护的是
    # 解密批处理 + 少量 dstack 阻塞调用，与主 backend 的 FEEDLING_ASGI_DB_THREADS
    # 无关，用 FEEDLING_ENCLAVE_THREADS（默认 32，env 名沿用免动 compose）。
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = config.ENCLAVE_THREADS
    yield
    import provider_client
    await backend_client.aclose()
    await provider_client.aclose_async_http_client()


def build_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # decrypt-with-image ~470KB JSON 是主要受益者；500B 阈值对齐 flask-compress 默认。
    app.add_middleware(GZipMiddleware, minimum_size=500)
    for name in _ROUTE_MODULES:
        module = importlib.import_module(f"enclave.routes.{name}")
        app.include_router(module.router)
    return app
```

```python
# backend/enclave/routes/health.py
"""GET /healthz + GET /attestation（旧 enclave_app L469-518 语义逐字）。"""

from __future__ import annotations

import json

from fastapi import APIRouter
from starlette.responses import JSONResponse, Response

from enclave import config, state

router = APIRouter()


@router.get("/healthz")
async def healthz():
    if state._state["ready"]:
        return JSONResponse({"ok": True, "ready": True})
    return JSONResponse(
        {"ok": False, "ready": False, "error": state._state["error"]},
        status_code=503,
    )


@router.get("/attestation")
async def attestation():
    if not state._state["ready"]:
        return JSONResponse(
            {"error": "not_ready", "detail": state._state["error"]}, status_code=503
        )
    # bundle 字典逐字取自旧 L482-515（含全部注释），仅两处替换：
    #   RELEASE → config.RELEASE、APP_AUTH → config.APP_AUTH，
    #   _state → state._state。
    att = state._state["attestation"]
    bundle = {
        # …… 按旧 L482-515 逐字搬运 ……
    }
    return Response(
        json.dumps(bundle, indent=2),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=60"},
    )
```

（`bundle` 字典执行时从旧文件复制全文，此处不重复 34 行原文；`indent=2` 与 Cache-Control 必须保留——iOS 审计卡直接读这个响应。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_enclave_routes_health.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit（gated）**

```bash
git add backend/enclave/routes/__init__.py backend/enclave/routes/health.py tests/test_enclave_routes_health.py
git commit -m "feat(enclave): ASGI app assembly + health/attestation routes"
```

---

### Task 9: `routes/envelope.py`（/v1/envelope/decrypt）

**Files:**
- Create: `backend/enclave/routes/envelope.py`
- Modify: `backend/enclave/routes/__init__.py`（`_ROUTE_MODULES` 加 `"envelope"`）
- Test: `tests/test_enclave_routes_envelope.py`

**Interfaces:**
- Consumes: `auth.extract_auth/whoami_live`、`keys.get_content_sk`、`envelope.decrypt_envelope/DecryptFailure`
- Produces: `router: APIRouter`，POST `/v1/envelope/decrypt`
- 错误串：**下划线拼法**（`missing_api_key` / `cannot_resolve_user_id`），403 `decrypt_failed: ...`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_routes_envelope.py
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def _authed(monkeypatch):
    async def fake_backend_get(path, headers, params=None):
        return {"user_id": "usr_a"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)


def test_missing_credentials_underscore_spelling(client):
    r = client.post("/v1/envelope/decrypt", json={"envelope": {}})
    assert r.status_code == 401
    assert r.get_json() == {"error": "missing_api_key"}  # 下划线拼法，勿改


def test_envelope_required(client, _authed):
    r = client.post("/v1/envelope/decrypt", json={},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "envelope required"}


def test_decrypt_failure_maps_403(client, _authed, monkeypatch):
    def boom(env, uid, sk):
        raise envmod.DecryptFailure("owner mismatch: x")
    monkeypatch.setattr(envmod, "decrypt_envelope", boom)
    r = client.post("/v1/envelope/decrypt",
                    json={"envelope": {"id": "i", "v": 1}},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 403
    assert r.get_json()["error"].startswith("decrypt_failed: owner mismatch")


def test_success_returns_plaintext_b64(client, _authed, monkeypatch):
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: b"secret!")
    r = client.post("/v1/envelope/decrypt",
                    json={"envelope": {"id": "itm", "v": 2}},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert base64.b64decode(body["plaintext_b64"]) == b"secret!"
    assert body == {"owner_user_id": "usr_a", "id": "itm", "v": 2,
                    "plaintext_b64": body["plaintext_b64"]}


def test_backend_401_maps_unauthorized(client, monkeypatch):
    req = httpx.Request("GET", "http://b/v1/users/whoami")
    err = httpx.HTTPStatusError("e", request=req,
                                response=httpx.Response(401, request=req))
    async def fake_backend_get(path, headers, params=None):
        raise err
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    r = client.post("/v1/envelope/decrypt", json={"envelope": {}},
                    headers={"X-API-Key": "bad"})
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized"}


def test_no_cache_every_call_resolves_live(client, monkeypatch):
    calls = []
    async def fake_backend_get(path, headers, params=None):
        calls.append(path)
        return {"user_id": "usr_a"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    from enclave import envelope as e2
    monkeypatch.setattr(e2, "decrypt_envelope", lambda e, u, s: b"x")
    for _ in range(3):
        client.post("/v1/envelope/decrypt", json={"envelope": {"id": "i"}},
                    headers={"X-API-Key": "k"})
    assert len(calls) == 3  # 敏感 unwrap 路由绝不走缓存（spec §2/§4）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_enclave_routes_envelope.py -v`
Expected: FAIL

- [ ] **Step 3: 实现（完整代码）**

```python
# backend/enclave/routes/envelope.py
"""POST /v1/envelope/decrypt —— 解开一个调用者自有的 v1 envelope。

旧 enclave_app L794-871 的 async 重写。安全语义不变：身份每次实时解析
（whoami_live，绝不走缓存）；本地 runtime-token HMAC 校验允许（吊销延迟
以 token TTL 为界）。错误串是下划线拼法（missing_api_key /
cannot_resolve_user_id）——与读路由的空格拼法是并存两套，禁止统一（spec §2）。"""

from __future__ import annotations

import base64

import anyio.to_thread
import httpx
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from enclave import auth, envelope, keys, state

router = APIRouter()


@router.post("/v1/envelope/decrypt")
async def v1_envelope_decrypt(request: Request):
    if not state._state["ready"]:
        return JSONResponse(
            {"error": "not_ready", "detail": state._state["error"]}, status_code=503)

    ctx = auth.extract_auth(request)
    if ctx.missing:
        return JSONResponse({"error": "missing_api_key"}, status_code=401)

    try:
        whoami = await auth.whoami_live(ctx)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"error": f"backend_error: {e}"}, status_code=502)
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"backend_error: {e}"}, status_code=502)

    authorized_user_id = whoami.get("user_id", "")
    if not authorized_user_id:
        return JSONResponse({"error": "cannot_resolve_user_id"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}  # 有意偏差 #2：非对象 body 旧代码 500，这里归一为 400
    env = payload.get("envelope")
    if not isinstance(env, dict):
        return JSONResponse({"error": "envelope required"}, status_code=400)

    try:
        content_sk = await keys.get_content_sk()
    except Exception as e:
        return JSONResponse(
            {"error": f"key_derivation_unavailable: {e}"}, status_code=503)

    try:
        plaintext = await anyio.to_thread.run_sync(
            envelope.decrypt_envelope, env, authorized_user_id, content_sk)
    except envelope.DecryptFailure as e:
        return JSONResponse({"error": f"decrypt_failed: {e.reason}"}, status_code=403)

    return JSONResponse({
        "owner_user_id": authorized_user_id,
        "id": env.get("id", ""),
        "v": int(env.get("v", 1)),
        "plaintext_b64": base64.b64encode(plaintext).decode("ascii"),
    })
```

`routes/__init__.py`：`_ROUTE_MODULES = ("health", "envelope")`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_enclave_routes_envelope.py tests/test_enclave_routes_health.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit（gated）**

```bash
git add backend/enclave/routes/envelope.py backend/enclave/routes/__init__.py tests/test_enclave_routes_envelope.py
git commit -m "feat(enclave): /v1/envelope/decrypt ASGI route"
```

---

### Task 10: `routes/memory.py` + `routes/worldbook.py`

**Files:**
- Create: `backend/enclave/routes/memory.py`（/v1/memory/index、/v1/memory/fetch、/v1/memory/list）
- Create: `backend/enclave/routes/worldbook.py`（/v1/worldbook/match）
- Modify: `backend/enclave/routes/__init__.py`（加 `"memory", "worldbook"`）
- Test: `tests/test_enclave_routes_memory.py`

**Interfaces:**
- Consumes: `auth.extract_auth/resolve_read_caller`、`keys.get_content_sk`、`readside.*`（Task 4 全家）、`backend_client.backend_get`、`envelope.decrypt_envelope`、`worldbook_readside_core.build_block`（backend 根模块，不动）
- Produces: `memory.router`、`worldbook.router`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_routes_memory.py
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def _authed(monkeypatch):
    async def fake_backend_get(path, headers, params=None):
        return {"user_id": "usr_a"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)


def _v1_inner():
    return json.dumps({"summary": "s", "content": "c", "bucket": "b",
                       "threads": []}).encode()


def test_missing_key_space_spelling(client):
    r = client.post("/v1/memory/index", json={"moments": []})
    assert r.status_code == 401
    assert r.get_json() == {"error": "missing api_key"}  # 空格拼法


def test_index_moments_must_be_list(client, _authed):
    r = client.post("/v1/memory/index", json={"moments": "nope"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "moments must be a list"}


def test_index_decrypts_and_flags_unavailable(client, _authed, monkeypatch):
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: _v1_inner())
    moments = [
        {"id": "m1", "K_enclave": "x"},
        {"id": "m2", "visibility": "local_only"},
    ]
    r = client.post("/v1/memory/index", json={"moments": moments},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["user_id"] == "usr_a"
    assert [i["id"] for i in body["items"]] == ["m1"]
    assert body["unavailable_ids"] == ["m2"]


def test_fetch_blocks_sensitive_by_default(client, _authed, monkeypatch):
    sensitive = json.dumps({"summary": "s", "content": "c", "bucket": "b",
                            "threads": [], "is_sensitive": True}).encode()
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: sensitive)
    r = client.post("/v1/memory/fetch",
                    json={"moments": [{"id": "m1", "K_enclave": "x"}]},
                    headers={"X-API-Key": "k"})
    body = r.get_json()
    assert body["items"] == []
    assert body["blocked_sensitive_ids"] == ["m1"]


def test_memory_list_decrypt_and_serve(client, _authed, monkeypatch):
    inner = json.dumps({"title": "t", "description": "d", "type": "fact"}).encode()
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        assert path == "/v1/memory/list"
        return {"moments": [{"id": "m1", "v": 1, "K_enclave": "x"}], "total": 1}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: inner)
    r = client.get("/v1/memory/list", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["moments"][0]["title"] == "t"
    assert body["moments"][0]["decrypt_status"] == "ok"


def test_worldbook_match_shape(client, _authed, monkeypatch):
    inner = json.dumps({"entries": []}).encode()
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: inner)
    r = client.post("/v1/worldbook/match",
                    json={"world_books": [], "messages": []},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["user_id"] == "usr_a"
    assert body["unavailable_ids"] == []


def test_worldbook_messages_must_be_list(client, _authed):
    r = client.post("/v1/worldbook/match",
                    json={"world_books": [], "messages": "x"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "messages must be a list"}


def test_runtime_token_only_forwards_token(client, monkeypatch):
    seen = []
    async def fake_backend_get(path, headers, params=None):
        seen.append(dict(headers or {}))
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        return {"moments": [], "total": 0}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    r = client.get("/v1/memory/list",
                   headers={"X-Feedling-Runtime-Token": "tok-1"})
    assert r.status_code == 200
    # spec §7 回归：api_key 为空时所有 backend 调用转发 runtime token，非空 auth
    assert all(h == {"X-Feedling-Runtime-Token": "tok-1"} for h in seen)
    assert len(seen) == 2  # whoami + memory/list
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_enclave_routes_memory.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

**`backend/enclave/routes/memory.py`** —— 三条路由。/index 与 /fetch 是旧 L1302-1360 的直译；/list 是旧 L1601-1697 的直译。骨架（/index 全文给出，/fetch、/list 按同构模式 + 行号直译）：

```python
# backend/enclave/routes/memory.py
"""memory 读侧三路由（旧 enclave_app L1302-1360 + L1601-1697）。
错误串空格拼法；解密批处理经 to_thread（spec §4）。"""

from __future__ import annotations

import anyio.to_thread
import httpx
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from enclave import auth, backend_client, envelope, keys, readside, state

router = APIRouter()


async def _read_payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


@router.post("/v1/memory/index")
async def v1_memory_index(request: Request):
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)
    try:
        content_sk = await keys.get_content_sk()
    except Exception as e:
        return JSONResponse(
            {"error": f"key_derivation_unavailable: {e}"}, status_code=503)
    payload = await _read_payload(request)
    moments = payload.get("moments")
    if not isinstance(moments, list):
        return JSONResponse({"error": "moments must be a list"}, status_code=400)
    effective_limit = readside.memory_readside_effective_limit(payload.get("limit"))

    def _work():
        items, unavailable_ids = readside.decrypt_readside_items(
            moments[:effective_limit], user_id or "", content_sk,
            item_builder=readside.build_memory_index_item)
        items = readside.memory_index_filter_items(items, payload)
        if not bool(payload.get("include_sensitive", False)):
            items = [i for i in items if not i.get("is_sensitive")]
        return items, unavailable_ids

    items, unavailable_ids = await anyio.to_thread.run_sync(_work)
    return JSONResponse({
        "user_id": user_id,
        "items": items,
        "unavailable_ids": unavailable_ids,
    })
```

`/v1/memory/fetch`：同构，`item_builder=readside.build_memory_fetch_item`，敏感项分流进 `blocked_sensitive_ids`（旧 L1346-1360 逐字直译进 `_work`）。

`/v1/memory/list`：auth 后先 `backend_client.backend_get("/v1/memory/list", ctx.forward_headers, params=...)`（params 组装照旧 L1632-1636；httpx 异常映射逐字照旧 L1639-1646，注意"whoami 可能来自缓存，此处 401 仍映射 401"的注释保留），再 `content_sk`，再把旧 L1655-1690 的解密循环整段放进 `_work()` 经 to_thread（`_decrypt_envelope`→`envelope.decrypt_envelope`，`DecryptFailure`→`envelope.DecryptFailure`）。

**`backend/enclave/routes/worldbook.py`** —— 旧 L1363-1401 直译：auth（`resolve_read_caller`）→ content_sk → 手工校验 `world_books`/`messages` 为 list（400 逐字）→ 解密循环 + `worldbook_readside_core.build_block(entries, messages)` 全部放进 `_work()` 经 to_thread。`import worldbook_readside_core` 保持根模块导入。

`routes/__init__.py`：`_ROUTE_MODULES = ("health", "envelope", "memory", "worldbook")`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_enclave_routes_memory.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit（gated）**

```bash
git add backend/enclave/routes/memory.py backend/enclave/routes/worldbook.py backend/enclave/routes/__init__.py tests/test_enclave_routes_memory.py
git commit -m "feat(enclave): memory readside + worldbook ASGI routes"
```

---

### Task 11: `routes/chat.py`（/v1/chat/history）

**Files:**
- Create: `backend/enclave/routes/chat.py`
- Modify: `backend/enclave/routes/__init__.py`（加 `"chat"`）
- Test: `tests/test_enclave_routes_chat.py`

**Interfaces:**
- Consumes: `auth.extract_auth/resolve_read_caller`、`backend_client.backend_get`、`keys.get_content_sk`、`envelope.decrypt_envelope/DecryptFailure`、`readside.moments_to_cards/select_context_memories_via_readside/memory_readside_for_model_api_enabled/memory_readside_model_api_limit`、`context_memory_selection.select_context_memories(_with_trace)`（根模块）
- Produces: `router`；模块内纯函数 `_decrypt_history_items(messages: list, authorized_user_id: str, content_sk) -> tuple[list, list]`（(decrypted, errors)，供 to_thread 与 perf 测试 monkeypatch）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_routes_chat.py
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


def _wire(monkeypatch, messages, moments=None):
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": messages, "total": len(messages)}
        if path == "/v1/memory/list":
            return {"moments": moments or [], "total": 0}
        raise AssertionError(path)
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)


def test_history_decrypts_text_messages(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "m1", "role": "user", "ts": 1.0, "v": 1, "source": "ios",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a"},
    ])
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: b"hello")
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    m = body["messages"][0]
    assert m["content"] == "hello"
    assert m["decrypt_status"] == "ok"
    assert body["decrypt_errors"] == []
    assert body["user_id"] == "usr_a"
    assert "context_memories" in body


def test_local_only_placeholder(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "m1", "role": "user", "ts": 1.0, "v": 1,
         "visibility": "local_only", "content_type": "text"},
    ])
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    m = r.get_json()["messages"][0]
    assert m["content"] is None
    assert m["decrypt_status"] == "local_only_agent_cannot_read"


def test_per_item_decrypt_error_not_500(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "bad", "role": "user", "ts": 1.0, "v": 1,
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a"},
    ])
    def boom(env, uid, sk):
        raise envmod.DecryptFailure("AEAD verify: nope")
    monkeypatch.setattr(envmod, "decrypt_envelope", boom)
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["messages"][0]["decrypt_status"].startswith("error: AEAD")
    assert body["decrypt_errors"][0]["id"] == "bad"


def test_image_message_with_caption(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "img1", "role": "user", "ts": 1.0, "v": 1, "content_type": "image",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a",
         "image_mime": "image/png",
         "caption_body_ct": "y", "caption_nonce": "y", "caption_K_enclave": "y"},
    ])
    jpeg = b"\x89PNG fake"
    def fake_decrypt(env, uid, sk):
        return b"what is this?" if env.get("body_ct") == "y" else jpeg
    monkeypatch.setattr(envmod, "decrypt_envelope", fake_decrypt)
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    m = r.get_json()["messages"][0]
    assert base64.b64decode(m["image_b64"]) == jpeg
    assert m["image_mime"] == "image/png"
    assert m["content"] == "what is this?"


def test_context_memories_best_effort_on_failure(client, monkeypatch):
    _wire(monkeypatch, [])
    from enclave import readside
    def boom(*a, **kw):
        raise RuntimeError("selector exploded")
    monkeypatch.setattr(readside, "moments_to_cards", boom)
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200  # context_memories 失败绝不 500
    assert r.get_json()["context_memories"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_enclave_routes_chat.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`backend/enclave/routes/chat.py` 结构：

```python
# backend/enclave/routes/chat.py
"""GET /v1/chat/history —— decrypt-and-serve 聊天史 + context_memories。
旧 enclave_app L1404-1598 的 async 重写：auth/拉取在事件循环，
解密批处理 + context_memories 组装整体在 to_thread（spec §4）。"""

from __future__ import annotations

import anyio.to_thread
import httpx
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from context_memory_selection import (
    select_context_memories,
    select_context_memories_with_trace,
)
from enclave import auth, backend_client, envelope, keys, readside, state

router = APIRouter()


def _decrypt_history_items(messages, authorized_user_id, content_sk):
    """纯同步批解密（在 to_thread 里跑）。函数体 = 旧 L1471-1546 的
    for 循环逐字，唯一改动：_decrypt_envelope → envelope.decrypt_envelope、
    DecryptFailure → envelope.DecryptFailure。返回 (decrypted, errors)。"""
    decrypted, errors = [], []
    # …… 旧 L1471-1546 逐字 ……
    return decrypted, errors


def _build_context_memories(moments, decrypted, query_args):
    """纯同步 context_memories 选择（在 to_thread 里跑）。
    函数体 = 旧 L1551-1585 逐字（latest_user_text 提取 + context_mode/
    want_trace 解析已在路由层做完传入 query_args dict），
    _select_context_memories_via_readside → readside.select_context_memories_via_readside、
    _load_decrypted_moments 的解密部分 → readside.moments_to_cards(moments, ...)。
    返回 (context_memories, context_memory_trace | None)。"""
    ...


@router.get("/v1/chat/history")
async def v1_chat_history(request: Request):
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    since = request.query_params.get("since", "0")
    limit = request.query_params.get("limit", "200")
    try:
        hist = await backend_client.backend_get(
            "/v1/chat/history", ctx.forward_headers,
            params={"since": since, "limit": limit})
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"error": f"backend_error: {e}"}, status_code=502)
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"backend_error: {e}"}, status_code=502)

    try:
        content_sk = await keys.get_content_sk()
    except Exception as e:
        return JSONResponse(
            {"error": f"key_derivation_unavailable: {e}"}, status_code=503)

    decrypted, errors = await anyio.to_thread.run_sync(
        _decrypt_history_items, hist.get("messages", []), user_id, content_sk)

    # context_memories：best-effort，任何失败都不 500（旧 L1548-1587）。
    context_memories, context_memory_trace = [], None
    try:
        # query 参数解析（context_mode/context_strict/context_trace）逐字旧 L1559-1566
        use_readside = ...  # 旧 L1567
        memory_limit = (readside.memory_readside_model_api_limit()
                        if use_readside else 200)
        listing = await backend_client.backend_get(
            "/v1/memory/list", ctx.forward_headers,
            params={"limit": str(memory_limit)})
        moments = listing.get("moments", []) or []
        context_memories, context_memory_trace = await anyio.to_thread.run_sync(
            _build_context_memories, moments, decrypted, query_args)
    except Exception as e:
        print(f"[chat/history:{user_id}] context_memories failed: {e}")

    payload = {
        "user_id": user_id,
        "messages": decrypted,
        "context_memories": context_memories,
        "total": hist.get("total", len(decrypted)),
        "decrypt_errors": errors,
    }
    if context_memory_trace is not None:
        payload["context_memory_trace"] = context_memory_trace
    return JSONResponse(payload)
```

直译要点：
1. 旧代码 `/v1/memory/list` 拉取失败静默返回 `[]`（`_load_decrypted_moments` 里的 `except httpx.HTTPError: return []`）——新代码由外层 `except Exception` 覆盖，语义相同（context_memories 为空，不报错）。
2. `query_args` 是路由层预解析好的 dict（`{"context_mode": ..., "want_trace": ..., "latest 从 decrypted 提取放 _build 内"}`），因为 `request.query_params` 不能跨线程安全假设——显式传参（与 AuthContext 同一原则）。
3. `select_context_memories` / `_with_trace` / `moments_to_cards` 全在 `_build_context_memories`（线程内）调用。

`routes/__init__.py`：加 `"chat"`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_enclave_routes_chat.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit（gated）**

```bash
git add backend/enclave/routes/chat.py backend/enclave/routes/__init__.py tests/test_enclave_routes_chat.py
git commit -m "feat(enclave): /v1/chat/history ASGI route with threaded batch decrypt"
```

---

### Task 12: `routes/identity.py`（/v1/identity/get）

**Files:**
- Create: `backend/enclave/routes/identity.py`
- Modify: `backend/enclave/routes/__init__.py`（加 `"identity"`）
- Test: `tests/test_enclave_routes_identity.py`

**Interfaces:**
- Consumes: 同 Task 10 模式；另移入 `_parse_iso_calendar_date`（旧 L781-791 → 本模块私有 `_parse_iso_calendar_date`，逐字）
- Produces: `router`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_routes_identity.py
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


def _wire(monkeypatch, identity):
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        assert path == "/v1/identity/get"
        return {"identity": identity}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)


def test_identity_none_passthrough(client, monkeypatch):
    _wire(monkeypatch, None)
    r = client.get("/v1/identity/get", headers={"X-API-Key": "k"})
    assert r.get_json() == {"identity": None, "user_id": "usr_a"}


def test_identity_decrypt_with_live_days_anchor(client, monkeypatch):
    anchor = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    _wire(monkeypatch, {"v": 1, "K_enclave": "x", "body_ct": "x", "nonce": "x",
                        "owner_user_id": "usr_a",
                        "relationship_started_at": anchor,
                        "created_at": "c", "updated_at": "u"})
    inner = json.dumps({"agent_name": "枫", "self_introduction": "hi",
                        "dimensions": [], "days_with_user": 999}).encode()
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: inner)
    r = client.get("/v1/identity/get", headers={"X-API-Key": "k"})
    body = r.get_json()["identity"]
    assert body["agent_name"] == "枫"
    assert body["days_with_user"] == 10  # 服务端锚点覆盖信封内旧值
    assert body["decrypt_status"] == "ok"


def test_identity_local_only(client, monkeypatch):
    _wire(monkeypatch, {"v": 1, "visibility": "local_only",
                        "created_at": "c", "updated_at": "u"})
    r = client.get("/v1/identity/get", headers={"X-API-Key": "k"})
    body = r.get_json()["identity"]
    assert body["decrypt_status"] == "local_only_agent_cannot_read"


def test_identity_decrypt_error_shape(client, monkeypatch):
    _wire(monkeypatch, {"v": 1, "K_enclave": "x", "body_ct": "x", "nonce": "x",
                        "owner_user_id": "usr_a", "created_at": "c",
                        "updated_at": "u"})
    def boom(env, uid, sk):
        raise envmod.DecryptFailure("bad tag")
    monkeypatch.setattr(envmod, "decrypt_envelope", boom)
    r = client.get("/v1/identity/get", headers={"X-API-Key": "k"})
    body = r.get_json()
    assert body["identity"]["decrypt_status"] == "error: bad tag"
    assert body["decrypt_errors"] == [{"reason": "bad tag"}]
```

- [ ] **Step 2: 跑失败** — Run: `python -m pytest tests/test_enclave_routes_identity.py -v`，Expected: FAIL

- [ ] **Step 3: 实现**

`backend/enclave/routes/identity.py`：旧 L1700-1799 直译，模式同 Task 10（auth → `backend_client.backend_get("/v1/identity/get", ...)` → content_sk → 单条解密 + days_with_user 锚点计算放进 `_work()` 经 to_thread）。`_parse_iso_calendar_date`（旧 L781-791）逐字移入本模块。httpx 异常映射与错误串逐字（`missing api_key` 空格拼法、`backend_unreachable`、`key_derivation_unavailable`）。`routes/__init__.py` 加 `"identity"`。

- [ ] **Step 4: 跑通过** — Run: `python -m pytest tests/test_enclave_routes_identity.py -v`，Expected: 4 PASS

- [ ] **Step 5: Commit（gated）**

```bash
git add backend/enclave/routes/identity.py backend/enclave/routes/__init__.py tests/test_enclave_routes_identity.py
git commit -m "feat(enclave): /v1/identity/get ASGI route"
```

---

### Task 13: `routes/frames.py`（decrypt / caption / image + Range）

**Files:**
- Create: `backend/enclave/routes/frames.py`
- Modify: `backend/enclave/routes/__init__.py`（加 `"frames"`）
- Test: `tests/test_enclave_routes_frames.py`

**Interfaces:**
- Consumes: `visual.parse_visual_plaintext/raw_image_mime/IMAGE_EXTENSION_BY_MIME`、`provider_client.chat_completion_async/ProviderConfig/ProviderError`、`config.SCREEN_VLM_MODEL/SCREEN_VLM_BASE_URL`，其余同前
- Produces: `router`；`_conditional_image_response(request, image_bytes, image_mime, frame_id) -> Response`（Range/ETag 实现，独立可测）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_routes_frames.py
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402

FRAME_ID = "ab" * 8
JPEG = b"\xff\xd8\xff" + bytes(range(256)) * 4  # 1027 bytes，可切块


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def _wired(monkeypatch):
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        assert path == f"/v1/screen/frames/{FRAME_ID}/envelope"
        return {"v": 1, "K_enclave": "x", "body_ct": "x", "nonce": "x",
                "owner_user_id": "usr_a", "ts": 1.0}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    inner = {"image": base64.b64encode(JPEG).decode(), "image_mime": "image/jpeg",
             "ocr_text": "text on screen", "app": "Safari", "w": 100, "h": 200}
    monkeypatch.setattr(envmod, "decrypt_envelope",
                        lambda e, u, s: json.dumps(inner).encode())


def test_bad_frame_id_400(client):
    r = client.get("/v1/screen/frames/NOT-HEX/decrypt",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "bad frame id"}


def test_decrypt_include_image_toggle(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/decrypt?include_image=false",
                   headers={"X-API-Key": "k"})
    body = r.get_json()
    assert body["image_b64"] is None
    assert body["image_bytes_omitted"] is True
    assert body["ocr_text"] == "text on screen"
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/decrypt",
                   headers={"X-API-Key": "k"})
    assert base64.b64decode(r.get_json()["image_b64"]) == JPEG


def test_caption_unconfigured_503(client, _wired, monkeypatch):
    monkeypatch.delenv("FEEDLING_SCREEN_VLM_API_KEY", raising=False)
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/caption",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 503
    assert r.get_json() == {"error": "screen_caption_unconfigured"}


def test_caption_calls_async_vlm(client, _wired, monkeypatch):
    monkeypatch.setenv("FEEDLING_SCREEN_VLM_API_KEY", "vk")
    import provider_client
    seen = {}
    async def fake_async(cfg, messages, **kw):
        seen["provider"] = cfg.provider
        seen["kw"] = kw
        return {"reply": " a caption "}
    monkeypatch.setattr(provider_client, "chat_completion_async", fake_async)
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/caption",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.get_json()["caption"] == "a caption"
    assert seen["provider"] == "openrouter"
    assert seen["kw"]["max_tokens"] == 160  # 非 full 模式


# ---- /image Range/ETag（spec §6/§7）----

def test_image_full_200(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.data == JPEG
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers.get("etag")


def test_image_single_range_206(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k", "Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.data == JPEG[:100]
    assert r.headers["content-range"] == f"bytes 0-99/{len(JPEG)}"


def test_image_parallel_chunks_reassemble(client, _wired):
    n, total = 4, len(JPEG)
    step = (total + n - 1) // n
    chunks = []
    for i in range(n):
        lo, hi = i * step, min((i + 1) * step - 1, total - 1)
        r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                       headers={"X-API-Key": "k", "Range": f"bytes={lo}-{hi}"})
        assert r.status_code == 206
        chunks.append(r.data)
    assert b"".join(chunks) == JPEG


def test_image_suffix_range(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k", "Range": "bytes=-100"})
    assert r.status_code == 206
    assert r.data == JPEG[-100:]


def test_image_multipart_range_falls_back_200(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k", "Range": "bytes=0-9,20-29"})
    assert r.status_code == 200
    assert r.data == JPEG


def test_image_unsatisfiable_416(client, _wired):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k",
                            "Range": f"bytes={len(JPEG) + 10}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(JPEG)}"


def test_image_etag_304(client, _wired):
    etag = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                      headers={"X-API-Key": "k"}).headers["etag"]
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/image",
                   headers={"X-API-Key": "k", "If-None-Match": etag})
    assert r.status_code == 304


def test_image_head_supported(client, _wired):
    # spec §6 HEAD 验收（/healthz、/attestation 已在 health 测试覆盖）
    r = client.open(f"/v1/screen/frames/{FRAME_ID}/image", method="HEAD",
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.data == b""  # Starlette 自动 HEAD：头同 GET、无 body
```

- [ ] **Step 2: 跑失败** — Run: `python -m pytest tests/test_enclave_routes_frames.py -v`，Expected: FAIL

- [ ] **Step 3: 实现**

`/decrypt`（旧 L1852-1943）与 `/caption`（旧 L1946-2055）按 Task 10 模式直译；要点：

1. frame_id 校验 `re.match(r"^[a-f0-9]{16,64}$", frame_id or "")` → 400 `bad frame id`，在 auth 之前（照旧顺序）。Path 声明用裸 `str`：`@router.get("/v1/screen/frames/{frame_id}/decrypt")`，签名 `async def v1_frame_decrypt(frame_id: str, request: Request)`（str 类型不会触发 422）。
2. envelope 拉取的 404 映射 `frame not found`（照旧）。
3. `/caption`：`vlm_key = os.environ.get("FEEDLING_SCREEN_VLM_API_KEY", "")` **每请求读取，无 fallback，fail-closed**（spec §4 caption 运行时读取约束）；`model`/`base_url` 运行时读取带 `config.SCREEN_VLM_MODEL`/`config.SCREEN_VLM_BASE_URL` 默认；instruction/user_content 组装逐字旧 L2014-2028；调用换 `await provider_client.chat_completion_async(...)`（`ProviderError` 与 `httpx.HTTPError` 都映射 502 `screen_caption_failed: ...` 照旧）。
4. 单帧解密也走 `anyio.to_thread.run_sync(envelope.decrypt_envelope, ...)`（帧 100KB+，别赌）。

`/image`（旧 L2058-2154）+ Range 实现（完整代码）：

```python
def _conditional_image_response(request: Request, image_bytes: bytes,
                                image_mime: str, frame_id: str):
    """send_file(conditional=True) 的手工替代（spec §6）：单区间 Range、
    ETag/If-None-Match→304、非法区间 416、multipart Range 回退整文件 200。
    dstack-gateway 每 TCP 连接 ~1Mbps 限速下，客户端靠并行单区间请求分块拉图。"""
    total = len(image_bytes)
    ext = visual.IMAGE_EXTENSION_BY_MIME.get(image_mime, "image")
    etag = f'"{hashlib.sha256(image_bytes).hexdigest()[:32]}"'
    base_headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": "no-cache",
        "Content-Disposition": f'inline; filename="{frame_id}.{ext}"',
    }

    inm = request.headers.get("If-None-Match", "")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers=base_headers)

    range_header = (request.headers.get("Range") or "").strip()
    if range_header.startswith("bytes=") and "," not in range_header:
        spec_part = range_header[len("bytes="):].strip()
        start_s, _, end_s = spec_part.partition("-")
        start = end = None
        try:
            if start_s == "" and end_s:          # 后缀式 bytes=-N
                n = int(end_s)
                if n >= 1:
                    start, end = max(0, total - n), total - 1
            elif start_s:
                start = int(start_s)
                end = int(end_s) if end_s else total - 1
        except ValueError:
            start = None                          # 畸形 Range → 忽略（RFC 7233）
        if start is not None:
            if start >= total or end < start:
                return Response(status_code=416, headers={
                    **base_headers, "Content-Range": f"bytes */{total}"})
            end = min(end, total - 1)
            return Response(
                image_bytes[start:end + 1], status_code=206,
                media_type=image_mime,
                headers={**base_headers,
                         "Content-Range": f"bytes {start}-{end}/{total}"})
    return Response(image_bytes, media_type=image_mime, headers=base_headers)
```

路由主体：auth/拉取/解密同 `/decrypt`，末尾 `image_b64` 为空 → 404 `no image in plaintext`、b64 decode 失败 → 502 `image_b64_decode: ...`（照旧），成功 → `return _conditional_image_response(request, image_bytes, image_mime, frame_id)`。

`routes/__init__.py`：`_ROUTE_MODULES = ("health", "envelope", "memory", "worldbook", "chat", "identity", "frames")`（终态）。

- [ ] **Step 4: 跑通过** — Run: `python -m pytest tests/test_enclave_routes_frames.py -v`，Expected: 12 PASS

- [ ] **Step 5: 跑全部新增 enclave 测试**

Run: `python -m pytest tests/test_enclave_config.py tests/test_enclave_keys_attestation.py tests/test_enclave_envelope_core.py tests/test_enclave_visual_readside_units.py tests/test_enclave_backend_client.py tests/test_enclave_auth_async.py tests/test_provider_client_async.py tests/test_enclave_routes_health.py tests/test_enclave_routes_envelope.py tests/test_enclave_routes_memory.py tests/test_enclave_routes_chat.py tests/test_enclave_routes_identity.py tests/test_enclave_routes_frames.py -q`
Expected: 全 PASS

- [ ] **Step 6: Commit（gated）**

```bash
git add backend/enclave/routes/frames.py backend/enclave/routes/__init__.py tests/test_enclave_routes_frames.py
git commit -m "feat(enclave): frames routes with hand-rolled Range/ETag + async VLM caption"
```

---

### Task 14: 迁移 10 个旧测试文件到新包

**Files:**
- Modify: `tests/test_enclave_dev_seed.py`、`tests/test_enclave_route_errors.py`、`tests/test_enclave_routeb_readside.py`、`tests/test_enclave_runtime_token.py`、`tests/test_enclave_server_perf.py`、`tests/test_enclave_visual_plaintext.py`、`tests/test_enclave_frame_caption.py`、`tests/test_memory_readside.py`、`tests/test_memory_v1_readside.py`、`tests/test_memory_v1_schema.py`

**Interfaces:**
- Consumes: Task 1-13 的全部新接口。

统一替换映射表（每个文件按此机械替换后再按文件特记处理）：

| 旧引用 | 新引用 |
|---|---|
| `import enclave_app` / `importlib.import_module("enclave_app")` | 按需 `from enclave import state, auth, keys, envelope, visual, readside, backend_client, config` + `from enclave.routes import build_app` |
| `enclave_app.app.test_client()` | `_AsgiTestClient(build_app())`（`from asgi_test_client import _AsgiTestClient`） |
| `enclave_app.app.config.update(...)` | 删除该行（Starlette 异常处理不走 Flask TESTING 语义；错误映射由路由显式 JSONResponse 保证） |
| `enclave_app._state` | `enclave.state._state` |
| `enclave_app._flask_get` / `_flask_get_headers` monkeypatch | `enclave.backend_client.backend_get` 的 async fake（模式见 Task 10 测试） |
| `enclave_app._whoami_cached` | `enclave.auth.whoami_cached`（async；直接 patch `backend_client.backend_get` 更贴近行为，优先） |
| `enclave_app._decrypt_envelope` | `enclave.envelope.decrypt_envelope` |
| `enclave_app._get_or_derive_content_sk` | `enclave.keys.get_content_sk`（async fake：`async def fake(): return sk`） |
| `enclave_app._RUNTIME_TOKEN_SECRET` | `enclave.config.RUNTIME_TOKEN_SECRET`（monkeypatch.setattr） |
| `enclave_app._parse_visual_plaintext` | `enclave.visual.parse_visual_plaintext` |
| `enclave_app._local_user_id_from_token` | `enclave.auth.local_user_id_from_token` |
| whoami 缓存清理（直接改 `_whoami_cache` dict） | `enclave.auth.reset_cache()` |

- [ ] **Step 1: 逐文件迁移（每迁一个跑一个）**

顺序与文件特记：
1. `test_enclave_visual_plaintext.py` / `test_enclave_frame_caption.py`：`importlib.import_module("enclave_app")` → 直接 import 新模块；caption 测试里 sync `provider_client.chat_completion` 的 monkeypatch 改为 `chat_completion_async` 的 async fake。
2. `test_memory_v1_schema.py` / `test_memory_readside.py` / `test_memory_v1_readside.py`：readside 纯函数改 `enclave.readside.*`；走路由的用 `_AsgiTestClient(build_app())`。
3. `test_enclave_routeb_readside.py`：route-B 管道函数 → `enclave.readside.select_context_memories_via_readside` 等。
4. `test_enclave_runtime_token.py`：token 快路径/回退 → `enclave.auth` + `enclave.config.RUNTIME_TOKEN_SECRET`。
5. `test_enclave_route_errors.py`：错误映射矩阵（本计划最重要的回归网）——backend fake 抛 `httpx.ConnectError` / `HTTPStatusError`，断言 502/503/401 逐条保留。
6. `test_enclave_dev_seed.py`：dev-seed bootstrap → `enclave.state.bootstrap` + `enclave.keys`。
7. `test_enclave_server_perf.py`：重写为事件循环不阻塞冒烟（完整代码）：

```python
# tests/test_enclave_server_perf.py（重写核心）
def test_healthz_responsive_during_slow_decrypt(monkeypatch):
    """spec §7：大批量解密进行中 /healthz 仍及时响应（解密在 to_thread，
    事件循环不被阻塞）。"""
    import asyncio, time
    import httpx
    from enclave import auth, backend_client, keys, state
    from enclave.routes import build_app, chat

    monkeypatch.setitem(state._state, "ready", True)
    monkeypatch.setitem(state._state, "error", None)
    auth.reset_cache()

    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": [], "total": 0}
        return {"moments": [], "total": 0}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    def slow_decrypt(messages, uid, sk):
        time.sleep(1.0)  # 模拟重解密批（在 to_thread 里跑才不会卡 loop）
        return [], []
    monkeypatch.setattr(chat, "_decrypt_history_items", slow_decrypt)

    app = build_app()

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            slow = asyncio.create_task(
                c.get("/v1/chat/history", headers={"X-API-Key": "k"}))
            await asyncio.sleep(0.1)  # 确保慢请求已进入解密阶段
            t0 = time.monotonic()
            h = await c.get("/healthz")
            dt = time.monotonic() - t0
            r = await slow
            return h.status_code, dt, r.status_code

    h_status, dt, slow_status = asyncio.run(main())
    assert h_status == 200
    assert slow_status == 200
    assert dt < 0.5, f"/healthz took {dt:.2f}s while decrypt batch was running"
```

- [ ] **Step 2: 全套 enclave/memory 相关测试通过**

Run: `python -m pytest tests/ -q -k "enclave or memory or worldbook or context"`
Expected: 全 PASS，0 fail

- [ ] **Step 3: Commit（gated）**

```bash
git add tests/
git commit -m "test(enclave): migrate legacy enclave suites to ASGI package"
```

---

### Task 15: `serving.py` + `asgi_worker.py` + 薄入口重写

**Files:**
- Create: `backend/enclave/asgi_worker.py`
- Create: `backend/enclave/serving.py`
- Modify: `backend/enclave_app.py`（整文件替换为薄入口）
- Test: `tests/test_enclave_serving_asgi.py`

**Interfaces:**
- Consumes: `state.bootstrap`、`config.ENCLAVE_PORT/enclave_worker_count`、`routes.build_app`
- Produces: `serving.materialize_tls_files() -> tuple[str, str] | None`、`serving.gunicorn_options(tls) -> dict`、`serving.run_enclave_server(tls) -> None`、`asgi_worker.EnclaveUvicornWorker`、`asgi_worker._enclave_create_ssl_context(*, certfile, keyfile, **_) -> ssl.SSLContext`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_enclave_serving_asgi.py
from __future__ import annotations

import datetime as dt
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402


def _self_signed(tmp_path):
    """测试用自签 ECDSA P-256 证书（与 dstack_tls 产物同形）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime(2020, 1, 1))
            .not_valid_after(dt.datetime(2040, 1, 1))
            .sign(key, hashes.SHA256()))
    cert_p = tmp_path / "cert.pem"
    key_p = tmp_path / "key.pem"
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(cert_p), str(key_p)


def test_custom_ssl_context_semantics(tmp_path):
    from enclave import asgi_worker
    cert, key = _self_signed(tmp_path)
    ctx = asgi_worker._enclave_create_ssl_context(certfile=cert, keyfile=key)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_uvicorn_create_ssl_context_is_patched():
    import uvicorn.config
    from enclave import asgi_worker
    assert uvicorn.config.create_ssl_context is asgi_worker._enclave_create_ssl_context


def test_gunicorn_options(monkeypatch, tmp_path):
    from enclave import serving
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "2")
    opts = serving.gunicorn_options(None)
    assert opts["workers"] == 2
    assert opts["worker_class"] == "enclave.asgi_worker.EnclaveUvicornWorker"
    assert opts["timeout"] == 120
    assert "certfile" not in opts
    cert, key = _self_signed(tmp_path)
    opts = serving.gunicorn_options((cert, key))
    assert opts["certfile"] == cert and opts["keyfile"] == key


def test_materialize_tls_files_roundtrip(monkeypatch):
    from enclave import serving, state
    monkeypatch.setitem(state._state, "tls_enabled", True)
    monkeypatch.setitem(state._state, "tls_cert_pem", b"CERT")
    monkeypatch.setitem(state._state, "tls_key_pem", b"KEY")
    tls = serving.materialize_tls_files()
    assert tls is not None
    cert_path, key_path = tls
    assert Path(cert_path).read_bytes() == b"CERT"
    assert Path(key_path).read_bytes() == b"KEY"
    assert (Path(cert_path).stat().st_mode & 0o777) == 0o600
    monkeypatch.setitem(state._state, "tls_enabled", False)
    assert serving.materialize_tls_files() is None


def test_thin_entrypoint_importable_without_flask():
    import importlib
    for m in ("flask", "flask_compress"):
        sys.modules.pop(m, None)
    import enclave_app  # noqa: F401
    importlib.reload(enclave_app)
    assert "flask" not in sys.modules  # 薄入口不再触碰 flask
```

- [ ] **Step 2: 跑失败** — Run: `python -m pytest tests/test_enclave_serving_asgi.py -v`，Expected: FAIL

- [ ] **Step 3: 实现**

**`backend/enclave/asgi_worker.py`**（完整代码）：

```python
"""enclave 专用 gunicorn worker：TLS 行为收口点（spec §5）。

iOS 把 sha256(cert.DER) 钉在 REPORT_DATA 里，握手必须精确出示 bootstrap
派生的那张证书，且语义与旧 _enclave_ssl_context 一致：裸 PROTOCOL_TLS_SERVER、
TLS1.2+、无客户端证书校验、无 ALPN 定制。uvicorn 没有公开的"注入现成
SSLContext"入口（Config.load 内部调 create_ssl_context），所以在这里对
uvicorn.config.create_ssl_context 做进程级替换——enclave 进程只跑 enclave，
不会波及主 backend（主 backend 用 asgi.worker.FeedlingUvicornWorker，
不 import 本模块）。本模块被 import 即生效（gunicorn 解析 worker_class 时）。"""

from __future__ import annotations

import ssl

import uvicorn.config
from uvicorn_worker import UvicornWorker


def _enclave_create_ssl_context(*, certfile, keyfile, **_ignored) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


uvicorn.config.create_ssl_context = _enclave_create_ssl_context


class EnclaveUvicornWorker(UvicornWorker):
    # limit_concurrency 是 uvicorn 兜底闸（远高于正常并发），防失控堆积。
    CONFIG_KWARGS = {"limit_concurrency": 2048}
```

**`backend/enclave/serving.py`**：逐字移动 L2209-2254 `_materialize_tls_files`→`materialize_tls_files`（`_state`→`state._state`）；`gunicorn_options`（旧 L2274-2292 改写）：

```python
def gunicorn_options(tls: tuple[str, str] | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "bind": f"0.0.0.0:{config.ENCLAVE_PORT}",
        "workers": config.enclave_worker_count(),
        "worker_class": "enclave.asgi_worker.EnclaveUvicornWorker",
        "timeout": 120,
        "graceful_timeout": 30,
    }
    if tls is not None:
        cert_path, key_path = tls
        options["certfile"] = cert_path
        options["keyfile"] = key_path
    return options
```

（旧 `threads` 选项删除——uvicorn worker 无线程池概念，`FEEDLING_ENCLAVE_THREADS` 已改由 lifespan limiter 消费；旧 `ssl_context` hook 删除——由 asgi_worker 的 create_ssl_context 替换承担。）

`run_enclave_server`（旧 L2295-2325 改写）：BaseApplication 的 `load()` 返回 `build_app()`（延迟 import `from enclave.routes import build_app`），docstring 保留 compose_hash 约束说明。

**`backend/enclave_app.py`** 整文件替换（完整代码）：

```python
#!/usr/bin/env python3
"""Feedling enclave service — thin entrypoint.

实现在 backend/enclave/ 包（FastAPI/ASGI，见
docs/superpowers/specs/2026-07-04-enclave-asgi-migration-design.md）。
本文件保持 `python -u backend/enclave_app.py` 启动方式不变
（compose 命令与 compose_hash 故事不变，CONTRIBUTING §7；
tools/e2e_encryption_test.py 与 tests/e2e_model_api_test.py 也直接拉起它）。"""

from __future__ import annotations

from enclave import config, serving, state

if __name__ == "__main__":
    state.bootstrap()
    tls = serving.materialize_tls_files()
    scheme = "https" if tls else "http"
    print(
        f"Feedling enclave service listening on {scheme}://0.0.0.0:{config.ENCLAVE_PORT}",
        flush=True,
    )
    serving.run_enclave_server(tls)
```

- [ ] **Step 4: 跑通过** — Run: `python -m pytest tests/test_enclave_serving_asgi.py -v`，Expected: 5 PASS

- [ ] **Step 5: dev-seed 冒烟（真启动，走新 serving 栈）**

```bash
FEEDLING_DEV_DSTACK_SEED=smoke FEEDLING_ENCLAVE_PORT=5093 \
  python -u backend/enclave_app.py &
sleep 3
curl -s http://127.0.0.1:5093/healthz
curl -s http://127.0.0.1:5093/attestation | head -c 200
kill %1
```
Expected: healthz `{"ok": true, "ready": true}`；attestation 带 `dev-memory-sandbox` compose_hash。

- [ ] **Step 6: Commit（gated）**

```bash
git add backend/enclave/asgi_worker.py backend/enclave/serving.py backend/enclave_app.py tests/test_enclave_serving_asgi.py
git commit -m "feat(enclave): ASGI serving stack + thin entrypoint, retire Flask enclave"
```

---

### Task 16: 删 flask 依赖 + 全量基线 + 收尾

**Files:**
- Modify: `backend/requirements.txt`（删 flask、flask-compress 两行及其头部注释段）
- Modify: `backend/requirements.lock`（重新生成）
- Modify: `docs/CHANGELOG.md`（追加 landmark 条目）
- Test: `tests/test_no_flask_anywhere.py`

- [ ] **Step 1: 写零 flask 守卫测试**

```python
# tests/test_no_flask_anywhere.py
"""ASGI 迁移收尾守卫：全 backend 不再 import flask（spec §8.4）。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).parent.parent / "backend"


def test_no_flask_imports_in_backend():
    out = subprocess.run(
        ["grep", "-rn", "-E", r"^\s*(import flask|from flask)", str(BACKEND),
         "--include=*.py"],
        capture_output=True, text=True)
    assert out.stdout.strip() == "", f"flask imports remain:\n{out.stdout}"


def test_enclave_package_imports_clean():
    sys.path.insert(0, str(BACKEND))
    for mod in ("enclave.config", "enclave.keys", "enclave.attestation",
                "enclave.state", "enclave.envelope", "enclave.visual",
                "enclave.readside", "enclave.backend_client", "enclave.auth",
                "enclave.routes", "enclave.serving", "enclave.asgi_worker"):
        __import__(mod)
    assert "flask" not in sys.modules
```

- [ ] **Step 2: 跑守卫** — Run: `python -m pytest tests/test_no_flask_anywhere.py -v`，Expected: 2 PASS（Task 15 已删旧代码；若 FAIL 则还有残留，先清）

- [ ] **Step 3: 删依赖并重锁**

`backend/requirements.txt`：删除 `flask>=3.0.0`、`flask-compress>=1.14` 两行 + 文件头"flask/flask-compress are needed ONLY by enclave_app.py …"注释段 + L35-36 的 flask 说明句（改为一句"backend 全栈 FastAPI/ASGI，无 flask"）。

```bash
uv pip compile backend/requirements.txt \
    --generate-hashes \
    --python-version 3.12 \
    -o backend/requirements.lock
git diff --stat backend/requirements.lock   # 确认只有 flask 系（flask/flask-compress/itsdangerous/blinker 等）消失
```

- [ ] **Step 4: 全量测试基线**

```bash
python -m pytest tests/ -q 2>&1 | tail -5
```
Expected: 通过数 ≥ 迁移前基线（主 backend 迁移后为 2037 green + 本计划新增），0 failed。若有 skip 暴增先查 Postgres 容器。

- [ ] **Step 5: memory-sandbox compose 冒烟（spec §8.1）**

```bash
docker compose -f deploy/docker-compose.memory-sandbox.yaml up -d --build
sleep 10
docker compose -f deploy/docker-compose.memory-sandbox.yaml ps   # enclave 服务 healthy
curl -sk http://127.0.0.1:5003/healthz
docker compose -f deploy/docker-compose.memory-sandbox.yaml down
```
Expected: enclave 容器 healthy，healthz ok。

- [ ] **Step 6: 更新 CHANGELOG**

`docs/CHANGELOG.md` 按现有条目格式追加：enclave Flask→FastAPI/asyncio 迁移（模块化 `backend/enclave/`、混合并发模型、手工 Range/ETag、flask/flask-compress 依赖删除、`FEEDLING_ENCLAVE_THREADS` 语义变更为解密线程池容量、两处有意行为偏差 OPTIONS→405 与 envelope 非对象 body→400），并注明 test CVM 待验证项（TLS 钉扎三条硬验收，spec §5/§8）。

- [ ] **Step 7: Commit（gated）**

```bash
git add backend/requirements.txt backend/requirements.lock docs/CHANGELOG.md tests/test_no_flask_anywhere.py
git commit -m "chore: drop flask/flask-compress — backend is fully ASGI"
```

---

## 计划外（执行完成后的部署验收，不属于本计划任务）

spec §8 的 test CVM / prod 阶段需要真实 TDX 环境与上链操作，由用户驱动：

1. test CVM 部署后：`/attestation` 无回归；`openssl s_client -connect <host>:5003 </dev/null 2>/dev/null | openssl x509 -outform DER | shasum -a 256` 与 attestation 指纹比对；iOS 审计卡实测；`printf 'GET /healthz …' | openssl s_client -tls1_1` 应握手失败（TLS1.2 下限）；chat/memory/identity/frames e2e；Range 并行分块实测；runtime-token 与 api-key 双路径。
2. prod：随下一次常规上链部署；`FEEDLING_ENCLAVE_WORKERS` 保持 2。
3. `tools/e2e_encryption_test.py` 与 `tests/e2e_model_api_test.py` 在部署前本地跑一遍（它们拉起真服务，覆盖薄入口）。
