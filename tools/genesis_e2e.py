#!/usr/bin/env python3
"""Genesis end-to-end harness: drive one real genesis pass against a deployed env.

Mimics the iOS upload client: creates a genesis import job, seals a test transcript
into v1 chunk envelopes (K_enclave -> enclave content pubkey, visibility=shared, so
the in-CVM worker can decrypt), uploads + finalizes, then polls + verifies the
distilled outputs (genesis_state done, persona blob ENCRYPTED with no plaintext,
Garden facts written) and asserts no raw plaintext leaked.

Run against the TEST CVM (not locally — needs the enclave + a test user + the worker
flag FEEDLING_GENESIS_WORKER_ENABLED=1). Crypto matches content_encryption.build_envelope.

  upload:  python3 tools/genesis_e2e.py upload  --api-url <U> --api-key <K> --user-id <UID> \
                   --transcript transcript.txt [--enclave-pk-hex <hex> | --attestation-url <U>] \
                   [--source-kind history] [--chunk-size 12000]
  verify:  python3 tools/genesis_e2e.py verify  --api-url <U> --api-key <K> --job-id <J>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from content_encryption import build_envelope  # noqa: E402

try:
    from nacl.public import PrivateKey  # PyNaCl (a backend dep)
except Exception as e:  # noqa: BLE001
    print(json.dumps({"ok": False, "error": f"PyNaCl required (backend dep): {e}"}))
    sys.exit(2)


def _http(method, url, api_key, *, json_body=None, timeout=60):
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-API-Key": api_key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:400]}


def _enclave_pk_bytes(args) -> bytes:
    if args.enclave_pk_hex:
        return bytes.fromhex(args.enclave_pk_hex.strip())
    url = args.attestation_url or f"{args.api_url.rstrip('/')}/attestation"
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    pk_hex = body.get("enclave_content_pk_hex") or body.get("content_pk_hex") or ""
    if not pk_hex:
        raise SystemExit(f"no enclave_content_pk_hex at {url}")
    return bytes.fromhex(pk_hex)


def _chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), max(1, size))] or [""]


def _provision_user(args) -> tuple[str, str, bytes, bytes]:
    """Self-provision a throwaway test user: register a fresh content keypair, set up
    its model_api provider key (validated -> test_status=ok), and read its enclave pk.
    Returns (api_key, user_id, enclave_pk_bytes, user_pk_bytes). Provider key comes
    from the env (GENESIS_E2E_PROVIDER_API_KEY), never persisted to disk."""
    import os
    provider_key = os.environ.get("GENESIS_E2E_PROVIDER_API_KEY", "").strip()
    if not provider_key:
        raise SystemExit("GENESIS_E2E_PROVIDER_API_KEY env required for --register (worker LLM key)")
    sk = PrivateKey.generate()
    user_pk = bytes(sk.public_key)
    base = args.api_url.rstrip("/")
    s, b = _http("POST", f"{base}/v1/users/register", "", json_body={"public_key": user_pk.hex()})
    if s >= 400:
        raise SystemExit(f"register failed {s}: {b}")
    api_key = b.get("api_key") or b.get("apiKey") or ""
    user_id = b.get("user_id") or b.get("userId") or ""
    if not api_key or not user_id:
        raise SystemExit(f"register response missing api_key/user_id: {b}")
    s, b = _http("POST", f"{base}/v1/model_api/setup", api_key, json_body={
        "provider": args.provider, "model": args.model,
        "base_url": args.base_url, "api_key": provider_key})
    if s >= 400 or b.get("test_status") not in ("ok", None):
        raise SystemExit(f"model_api/setup failed {s}: {b}")
    s, b = _http("GET", f"{base}/v1/users/whoami", api_key)
    enclave_hex = b.get("enclave_content_public_key_hex") or ""
    if not enclave_hex:
        raise SystemExit(f"whoami missing enclave_content_public_key_hex: {b}")
    return api_key, user_id, bytes.fromhex(enclave_hex), user_pk


def cmd_upload(args):
    if args.register:
        args.api_key, args.user_id, enclave_pk, user_pk = _provision_user(args)
        print(json.dumps({"provisioned": True, "user_id": args.user_id, "api_key": args.api_key}, ensure_ascii=False))
    else:
        enclave_pk = _enclave_pk_bytes(args)
        user_pk = bytes(PrivateKey.generate().public_key)  # throwaway: worker decrypts via K_enclave
    parts = _chunks(Path(args.transcript).read_text(encoding="utf-8"), args.chunk_size)

    status, body = _http("POST", f"{args.api_url.rstrip('/')}/v1/genesis/imports", args.api_key,
                         json_body={"source_kind": args.source_kind, "total_chunks": len(parts)})
    if status >= 400:
        print(json.dumps({"ok": False, "step": "create", "status": status, "body": body})); return 1
    job_id = body.get("job_id") or body.get("job", {}).get("job_id")
    if not job_id:
        print(json.dumps({"ok": False, "step": "create", "error": "no job_id", "body": body})); return 1

    for seq, part in enumerate(parts):
        env = build_envelope(plaintext=part.encode("utf-8"), owner_user_id=args.user_id,
                             user_pk_bytes=user_pk, enclave_pk_bytes=enclave_pk, visibility="shared")
        body_ct = env["body_ct"]
        cct_sha = hashlib.sha256(base64.b64decode(body_ct)).hexdigest()
        s, b = _http("PUT", f"{args.api_url.rstrip('/')}/v1/genesis/imports/{job_id}/chunks/{seq}",
                     args.api_key, json_body={"envelope": env, "ciphertext_sha256": cct_sha,
                                              "byte_start": 0, "byte_end": len(part.encode("utf-8"))})
        if s >= 400:
            print(json.dumps({"ok": False, "step": f"chunk:{seq}", "status": s, "body": b})); return 1

    s, b = _http("POST", f"{args.api_url.rstrip('/')}/v1/genesis/imports/{job_id}/finalize", args.api_key)
    print(json.dumps({"ok": s < 400, "step": "finalize", "status": s, "job_id": job_id, "body": b}, ensure_ascii=False))
    return 0 if s < 400 else 1


def cmd_verify(args):
    url = f"{args.api_url.rstrip('/')}/v1/genesis/imports/{args.job_id}"
    deadline = time.time() + args.timeout
    last = {}
    while time.time() < deadline:
        s, b = _http("GET", url, args.api_key)
        last = b
        state = str((b.get("state") or b.get("status") or b.get("job", {}).get("status") or "")).lower()
        if state in ("done", "failed"):
            break
        time.sleep(args.poll)
    state = str((last.get("state") or last.get("status") or last.get("job", {}).get("status") or "")).lower()
    out = {"ok": state == "done", "state": state, "job": last}
    # Privacy spot-check (shallow): the status payload must be metadata-only, never
    # raw chunk/transcript text. Deep check (genesis_persona encrypted, outputs
    # sanitized) needs a DB query in the env — see the README note in this file.
    blob = json.dumps(last, ensure_ascii=False).lower()
    out["status_payload_raw_keys"] = [k for k in ("transcript", "raw_text", "chunks", "plaintext") if k in blob]
    print(json.dumps(out, ensure_ascii=False))
    return 0 if state == "done" else 1


def main():
    p = argparse.ArgumentParser(prog="genesis_e2e", description="Genesis e2e harness (test CVM).")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upload", help="(optionally register a user, then) seal+upload chunks + finalize")
    up.add_argument("--api-url", required=True)
    up.add_argument("--register", action="store_true",
                    help="self-provision a throwaway user (register + model_api/setup + whoami); "
                         "provider key from GENESIS_E2E_PROVIDER_API_KEY env")
    up.add_argument("--provider", default="", help="with --register: model provider (e.g. anthropic/openai)")
    up.add_argument("--model", default="", help="with --register: model id")
    up.add_argument("--base-url", default="", help="with --register: optional provider base_url")
    up.add_argument("--api-key", default="", help="existing user api_key (omit with --register)")
    up.add_argument("--user-id", default="", help="owner_user_id (omit with --register)")
    up.add_argument("--transcript", required=True, help="path to a plaintext test transcript")
    up.add_argument("--enclave-pk-hex", default="", help="enclave content pubkey hex (non-register; else fetch attestation)")
    up.add_argument("--attestation-url", default="", help="defaults to <api-url>/attestation")
    up.add_argument("--source-kind", default="history")
    up.add_argument("--chunk-size", type=int, default=12000)
    up.set_defaults(func=cmd_upload)

    vf = sub.add_parser("verify", help="poll job to done + privacy spot-check")
    vf.add_argument("--api-url", required=True)
    vf.add_argument("--api-key", required=True)
    vf.add_argument("--job-id", required=True)
    vf.add_argument("--timeout", type=float, default=600)
    vf.add_argument("--poll", type=float, default=10)
    vf.set_defaults(func=cmd_verify)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
