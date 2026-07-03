#!/usr/bin/env python3
"""World book encrypted deployment E2E.

Default target is the test CVM:

    python tools/worldbook_e2e.py

The script registers a throwaway hosted user, builds iOS-compatible v1
world-book envelopes, verifies encrypted list/upsert/delete behavior, proves the
upsert-side 20k cap via enclave validation, then sends a hosted chat turn and
waits for the `worldbook_injected` flow-trace event.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import hashlib
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.provider_smoke import matrix
from tools.provider_smoke.client import SmokeClient, SmokeError


DEFAULT_BASE_URL = "https://test-api.feedling.app"
PASS = "OK "
FAIL = "FAIL"
_BOX_SEAL_INFO = b"feedling-box-seal-v1"


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def _build_env() -> dict[str, str]:
    env = _load_dotenv(ROOT / ".env")
    env.update({k: v for k, v in os.environ.items() if v})
    return env


def _first_provider(loaded: dict[str, dict], requested: str) -> tuple[str, dict]:
    if requested:
        if requested not in loaded:
            raise SystemExit(f"{FAIL} provider {requested!r} has no key in env/.env")
        return requested, loaded[requested]
    for provider in ("deepseek", "gemini", "openrouter", "anthropic", "openai", "openai_compatible"):
        if provider in loaded:
            return provider, loaded[provider]
    raise SystemExit(f"{FAIL} no provider key found in env/.env; cannot exercise hosted chat injection")


def _json_bytes(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _box_seal_hkdf(plaintext: bytes, recipient_pk_raw: bytes) -> bytes:
    recipient = X25519PublicKey.from_public_bytes(recipient_pk_raw)
    ek = X25519PrivateKey.generate()
    ek_pub = ek.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    shared = ek.exchange(recipient)
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_BOX_SEAL_INFO,
    ).derive(shared)
    wrap_nonce = hashlib.sha256(ek_pub + recipient_pk_raw).digest()[:12]
    ciphertext = ChaCha20Poly1305(wrap_key).encrypt(wrap_nonce, plaintext, None)
    return ek_pub + ciphertext


def _worldbook_envelope(
    *,
    user_id: str,
    user_pk: bytes,
    enclave_pk: bytes,
    entry_id: str,
    name: str,
    keywords: list[str],
    content: str,
    always_on: bool = False,
    enabled: bool = True,
) -> dict:
    plain = {
        "id": entry_id,
        "name": name,
        "keywords": keywords,
        "content": content,
        "alwaysOn": always_on,
        "enabled": enabled,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    aad = f"{user_id}|1|{entry_id}".encode("utf-8")
    body_ct = ChaCha20Poly1305(key).encrypt(nonce, _json_bytes(plain), aad)
    return {
        "v": 1,
        "id": entry_id,
        "body_ct": _b64(body_ct),
        "nonce": _b64(nonce),
        "K_user": _b64(_box_seal_hkdf(key, user_pk)),
        "K_enclave": _b64(_box_seal_hkdf(key, enclave_pk)),
        "enclave_pk_fpr": enclave_pk[:16].hex(),
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def _check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {label}")
        return
    suffix = f": {detail}" if detail else ""
    raise SystemExit(f"{FAIL} {label}{suffix}")


def _setup_hosted_account(client: SmokeClient, sess, provider: str, cfg: dict) -> str:
    last_detail = ""
    for model in cfg["models"]:
        try:
            client.setup(sess, provider, model, cfg["base_url"], cfg["api_key"])
            print(f"{PASS} model_api/setup provider={provider} model={model}")
            break
        except SmokeError as e:
            last_detail = e.detail
    else:
        raise SystemExit(f"{FAIL} model_api/setup failed for {provider}: {last_detail}")
    client.init_identity(sess)
    driver = client.enable_hosting(sess)
    client.open_chat_gate(sess)
    print(f"{PASS} hosted account ready driver={driver}")
    return driver


def _wait_for_worldbook_trace(client: SmokeClient, api_key: str, name: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last_events: list[dict] = []
    while time.monotonic() < deadline:
        status, body = client._req(
            "GET",
            "/v1/debug/trace?subsystem=worldbook&limit=50",
            api_key=api_key,
            read_timeout=20,
        )
        if status == 200:
            last_events = body.get("events") or []
            for event in last_events:
                names = ((event.get("detail") or {}).get("names") or [])
                if event.get("type") == "worldbook_injected" and name in names:
                    return event
        time.sleep(3)
    raise SystemExit(f"{FAIL} worldbook_injected trace not observed; last_events={last_events[:3]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("FEEDLING_SMOKE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--provider", default="")
    parser.add_argument("--api-key", default=os.environ.get("FEEDLING_WORLDBOOK_E2E_API_KEY", ""))
    parser.add_argument("--skip-chat", action="store_true", help="Only test encrypted storage/list/delete + over-cap validation.")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    env = _build_env()
    loaded = matrix.load_matrix(env)
    provider = ""
    cfg: dict = {}
    if not args.skip_chat and not args.api_key:
        provider, cfg = _first_provider(loaded, args.provider)
    client = SmokeClient(args.base_url)

    provider_label = provider or ("<existing-config>" if args.api_key else "<skipped>")
    print(f"base_url={args.base_url} provider={provider_label}")
    if args.api_key:
        sess = SimpleNamespace(user_id="", api_key=args.api_key, sk=b"", pk=b"")
        print(f"{PASS} using existing API key account")
    else:
        sess = client.register("worldbook-e2e")
        print(f"{PASS} registered {sess.user_id}")

    status, whoami = client._req("GET", "/v1/users/whoami", api_key=sess.api_key)
    _check("whoami 200", status == 200, str(whoami))
    sess.user_id = str(whoami.get("user_id") or sess.user_id)
    if not sess.pk:
        public_key_b64 = str(whoami.get("public_key") or "")
        _check("whoami has user public key", bool(public_key_b64), str(whoami))
        sess.pk = base64.b64decode(public_key_b64)
    enclave_pk_hex = str(whoami.get("enclave_content_public_key_hex") or "")
    _check("whoami has enclave content pk", len(enclave_pk_hex) == 64, str(whoami))
    enclave_pk = bytes.fromhex(enclave_pk_hex)

    token = f"wb-token-{secrets.token_hex(4)}"
    name = "WorldBook E2E"
    entry_id = f"wb_{secrets.token_hex(8)}"
    env_ok = _worldbook_envelope(
        user_id=sess.user_id,
        user_pk=sess.pk,
        enclave_pk=enclave_pk,
        entry_id=entry_id,
        name=name,
        keywords=[token],
        content=f"When the user says {token}, answer with the marker WORLD_BOOK_E2E_MARKER.",
    )

    status, body = client._req("POST", "/v1/worldbook/upsert", api_key=sess.api_key, body=env_ok)
    _check("worldbook upsert 200", status == 200 and body.get("id") == entry_id, str(body))
    status, body = client._req("GET", "/v1/worldbook/list", api_key=sess.api_key)
    envelopes = body.get("envelopes") or []
    _check("worldbook list returns encrypted envelope", status == 200 and any(e.get("id") == entry_id for e in envelopes), str(body))
    _check("worldbook list does not leak plaintext", "WORLD_BOOK_E2E_MARKER" not in json.dumps(body, ensure_ascii=False))

    too_big_id = f"wb_big_{secrets.token_hex(4)}"
    env_too_big = _worldbook_envelope(
        user_id=sess.user_id,
        user_pk=sess.pk,
        enclave_pk=enclave_pk,
        entry_id=too_big_id,
        name="Too Big",
        keywords=["too-big"],
        content="x" * 20001,
    )
    status, body = client._req("POST", "/v1/worldbook/upsert", api_key=sess.api_key, body=env_too_big)
    _check("worldbook over-cap upsert rejected", status == 400 and body.get("error") == "content_too_long", str(body))

    if args.skip_chat:
        print(f"{PASS} skipped hosted chat injection check")
    else:
        if not args.api_key:
            _setup_hosted_account(client, sess, provider, cfg)
        client._req("DELETE", "/v1/debug/trace", api_key=sess.api_key)
        status, body = client._req("POST", "/v1/debug/trace/enable", api_key=sess.api_key, body={"enabled": True})
        _check("debug trace enabled", status == 200 and body.get("enabled") is True, str(body))

        sent = client.send(sess, f"Please use context for {token}. Reply briefly.")
        _check("chat/send accepted hosted turn", bool(sent.get("user_message")), str(sent))
        event = _wait_for_worldbook_trace(client, sess.api_key, name, args.timeout)
        print(f"{PASS} worldbook_injected trace observed summary={event.get('summary')!r}")

    status, body = client._req("DELETE", f"/v1/worldbook/delete?id={entry_id}", api_key=sess.api_key)
    _check("worldbook delete 200", status == 200 and body.get("ok") is True, str(body))
    print(f"{PASS} worldbook deployment e2e passed user_id={sess.user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
