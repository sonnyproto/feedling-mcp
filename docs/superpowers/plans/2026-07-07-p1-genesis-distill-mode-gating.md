# P1 — genesis distill-mode gating + sealed schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Add the deploy-level `FEEDLING_GENESIS_DISTILL_MODE` gate + `sealed_v1` body schema + **bidirectional hard validation** on `/v1/genesis/imports/plaintext`, so worker mode can never ingest a sealed body (and vice-versa). Foundation + safety edge for the VPS resident-distill feature.

**Architecture:** Pure request-layer gating in `backend/genesis/genesis_core.py` (framework-neutral seam `plaintext_import`). No storage / worker changes yet; resident+sealed returns `501` until P2 wires the resident path. Worker mode with a legacy plaintext body is byte-for-byte unchanged.

**Tech Stack:** Python, pytest.

## Global Constraints (from spec)

- `FEEDLING_GENESIS_DISTILL_MODE = worker | resident`, **default `worker`**; garbage → `worker` (safe default).
- Worker mode **rejects sealed body** (400); resident mode **rejects plaintext body** (400). This is THE safety edge — a misconfig must never feed ciphertext into the worker as plaintext.
- `cloud/worker` path (`plaintext_import → start_job`) unchanged for legacy plaintext bodies.
- Sealed body marker: `format == "sealed_v1"`.

---

### Task 1: distill-mode + sealed-body helpers

**Files:**
- Modify: `backend/genesis/genesis_core.py` (add `import os`; add two module-level helpers after `_bad`)
- Test: `tests/test_genesis_distill_mode.py` (new)

**Interfaces:**
- Produces: `genesis_distill_mode() -> str` ("worker"|"resident"); `_is_sealed_body(payload: dict) -> bool`

- [ ] **Step 1: failing tests**

```python
# tests/test_genesis_distill_mode.py
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from genesis import genesis_core  # noqa: E402


def test_distill_mode_defaults_worker(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_DISTILL_MODE", raising=False)
    assert genesis_core.genesis_distill_mode() == "worker"


def test_distill_mode_resident(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_DISTILL_MODE", "resident")
    assert genesis_core.genesis_distill_mode() == "resident"


def test_distill_mode_garbage_is_worker(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_DISTILL_MODE", "nonsense")
    assert genesis_core.genesis_distill_mode() == "worker"


def test_is_sealed_body_true():
    assert genesis_core._is_sealed_body({"format": "sealed_v1", "sealed_envelope": {}}) is True


def test_is_sealed_body_false_legacy():
    assert genesis_core._is_sealed_body({"format": "auto", "content": "hi"}) is False
    assert genesis_core._is_sealed_body({}) is False
```

- [ ] **Step 2: run, expect fail** — `python -m pytest tests/test_genesis_distill_mode.py -q` → FAIL (no `genesis_distill_mode`).

- [ ] **Step 3: implement** — in `genesis_core.py` add `import os` to the import block, and after `_bad` (line ~46):

```python
def genesis_distill_mode() -> str:
    """Deploy-level distill mode. `resident` = a self-hosted VPS whose own local
    agent does the distillation (material sealed client-side, agent claims + distills);
    anything else (default) = `worker` = the current server-side genesis worker.
    Garbage → worker (safe default): a cloud box must never fall into resident."""
    return "resident" if str(os.environ.get("FEEDLING_GENESIS_DISTILL_MODE", "")).strip().lower() == "resident" else "worker"


def _is_sealed_body(payload: dict) -> bool:
    """A resident-mode upload is a client-sealed envelope, tagged `format: sealed_v1`
    (NOT the legacy plaintext body). Explicit tag so worker/resident bodies never blur."""
    return isinstance(payload, dict) and str(payload.get("format") or "").strip().lower() == "sealed_v1"
```

- [ ] **Step 4: run, expect pass** — `python -m pytest tests/test_genesis_distill_mode.py -q` → 5 passed.

- [ ] **Step 5: commit** — `git add backend/genesis/genesis_core.py tests/test_genesis_distill_mode.py && git commit -m "feat(genesis): distill-mode + sealed-body helpers (P1)"`

---

### Task 2: bidirectional hard validation in `plaintext_import`

**Files:**
- Modify: `backend/genesis/genesis_core.py` (`plaintext_import`, right after the `isinstance(payload, dict)` check, before the input_hash computation)
- Test: `tests/test_genesis_distill_mode.py` (append)

**Interfaces:**
- Consumes: `genesis_distill_mode`, `_is_sealed_body`, `_bad`

- [ ] **Step 1: failing tests** (append)

```python
def _raise(*a, **k):
    raise RuntimeError("reached_helpers")


def _call(payload, **env):
    # gating returns before touching store/helpers on reject; on pass it reaches the
    # injected helpers (we prove that via _raise).
    return genesis_core.plaintext_import(
        object(), payload, api_key=None,
        prepare=_raise, find_reusable=_raise, plaintext_mode=lambda p, **k: "add_memory",
        job_metadata=_raise, start_job=_raise,
    )


def test_worker_rejects_sealed(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_DISTILL_MODE", raising=False)  # worker
    body, status = _call({"format": "sealed_v1", "sealed_envelope": {}})
    assert status == 400 and body["error"] == "sealed_body_rejected_in_worker_mode"


def test_resident_rejects_plaintext(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_DISTILL_MODE", "resident")
    body, status = _call({"format": "auto", "content": "hi"})
    assert status == 400 and body["error"] == "plaintext_body_rejected_in_resident_mode"


def test_resident_sealed_501_until_p2(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_DISTILL_MODE", "resident")
    body, status = _call({"format": "sealed_v1", "sealed_envelope": {}})
    assert status == 501 and body["error"] == "resident_distill_not_available"


def test_worker_plaintext_proceeds_past_gating(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_DISTILL_MODE", raising=False)  # worker
    import pytest
    with pytest.raises(RuntimeError, match="reached_helpers"):
        _call({"format": "auto", "content": "hi"})  # passes gating → hits injected helper
```

- [ ] **Step 2: run, expect fail** — `python -m pytest tests/test_genesis_distill_mode.py -q` → the 4 new fail (gating not present; e.g. worker+sealed currently reaches helpers → RuntimeError not the 400).

- [ ] **Step 3: implement** — in `plaintext_import`, immediately after `if not isinstance(payload, dict): return _bad("json_object_required", 400)`:

```python
    mode = genesis_distill_mode()
    sealed = _is_sealed_body(payload)
    if mode == "worker" and sealed:
        return _bad("sealed_body_rejected_in_worker_mode", 400)
    if mode == "resident" and not sealed:
        return _bad("plaintext_body_rejected_in_resident_mode", 400)
    if mode == "resident" and sealed:
        # P2 wires the resident path (persist ciphertext + claimable job). Until then,
        # fail loudly rather than silently drop.
        return _bad("resident_distill_not_available", 501)
```

- [ ] **Step 4: run, expect pass** — `python -m pytest tests/test_genesis_distill_mode.py -q` → 9 passed.

- [ ] **Step 5: regression** — `python -m pytest tests/test_genesis_plaintext_routes.py -q` → still passing (worker plaintext unchanged).

- [ ] **Step 6: commit** — `git add backend/genesis/genesis_core.py tests/test_genesis_distill_mode.py && git commit -m "feat(genesis): bidirectional distill-mode body validation (P1 safety edge)"`
