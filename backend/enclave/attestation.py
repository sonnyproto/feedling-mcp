"""Enclave attestation assembly: REPORT_DATA layout + TDX quote fetch.

Verbatim extraction from enclave_app.py's "TLS cert material" and
"Attestation assembly" sections. See docs/DESIGN_E2E.md §5.1 for the
REPORT_DATA layout this encodes.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from dstack_sdk import DstackClient

# Sentinel "no TLS binding" fingerprint. Before Phase 3 the bundle always
# carried this (Caddy/gateway terminated TLS). Post-Phase 3 this appears
# only when ENCLAVE_TLS=false (local dev). iOS treats all-zeros as
# "operator terminates TLS" and surfaces the amber disclosure.
PHASE1_TLS_FINGERPRINT = b"\x00" * 32


# `derive_tls_cert_and_key` is imported from `dstack_tls` so enclave_app
# (which also terminates TLS inside the enclave in Phase C) derives from
# the same path and produces the same cert.


def build_report_data(content_pk_bytes: bytes, tls_cert_fingerprint: bytes, version_tag: bytes) -> bytes:
    """Construct the 64-byte REPORT_DATA per docs/DESIGN_E2E.md §5.1.

    Layout:
        [0:32]  sha256(content_pk || sha256(tls_cert_der) || "feedling-v1")
        [32]    version_byte
        [33]    flag_byte (bit 0: phase-1 placeholder TLS fingerprint)
        [34:64] reserved (zeros)
    """
    if len(tls_cert_fingerprint) != 32:
        raise ValueError("tls_cert_fingerprint must be 32 bytes (sha256)")
    binding = hashlib.sha256(content_pk_bytes + tls_cert_fingerprint + version_tag).digest()
    version_byte = b"\x01"
    flag_byte = b"\x01" if tls_cert_fingerprint == PHASE1_TLS_FINGERPRINT else b"\x00"
    reserved = b"\x00" * 30
    return binding + version_byte + flag_byte + reserved


def fetch_quote_and_measurements(dstack: DstackClient, report_data: bytes) -> dict[str, Any]:
    """Ask dstack for a TDX quote over our report_data, and pull the live
    measurement registers out of /info for clients to cross-check."""
    quote_resp = dstack.get_quote(report_data)
    info = dstack.info()
    tcb = info.tcb_info

    # event_log on the quote response is a JSON-encoded string; forward
    # as-is so the iOS verifier can decode if it wants to cross-check
    # RTMR values against the event chain.
    event_log_raw = getattr(quote_resp, "event_log", "") or ""

    # Parse mr_config_id directly from the raw quote bytes — the dstack SDK's
    # TcbInfo doesn't expose it, but dstack encodes compose_hash there on
    # real deployments per the convention from dstack-tutorial:
    #   mr_config_id[0]    = 0x01 (version marker)
    #   mr_config_id[1:33] = sha256(canonical(app_compose))
    #   mr_config_id[33:]  = zero padding
    # The simulator leaves mr_config_id all zeros, so the iOS auditor
    # treats a non-zero mr_config_id[0]=0x01 as an additional independent
    # confirmation of compose_hash, not a mandatory check.
    quote_hex = quote_resp.quote if isinstance(quote_resp.quote, str) else quote_resp.quote.hex()
    mr_config_id_hex = ""
    try:
        qbytes = bytes.fromhex(quote_hex)
        # TD Report body starts at offset 48; mr_config_id at body+184, 48 bytes
        mr_config_id_hex = qbytes[48 + 184:48 + 184 + 48].hex()
    except Exception:
        pass

    return {
        "tdx_quote_hex": quote_hex,
        "event_log_json": event_log_raw,
        "measurements": {
            "mrtd": tcb.mrtd,
            "rtmr0": tcb.rtmr0,
            "rtmr1": tcb.rtmr1,
            "rtmr2": tcb.rtmr2,
            "rtmr3": tcb.rtmr3,
            "mr_aggregated": tcb.mr_aggregated,
            "mr_config_id": mr_config_id_hex,
        },
        "compose_hash": info.compose_hash,
        "app_id": info.app_id,
        "instance_id": info.instance_id,
    }


def dev_attestation(report_data: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(report_data + os.environ.get("FEEDLING_DEV_DSTACK_SEED", "").encode("utf-8")).hexdigest()
    zero_measurement = "00" * 48
    return {
        "tdx_quote_hex": digest,
        "event_log_json": "[]",
        "measurements": {
            "mrtd": zero_measurement,
            "rtmr0": zero_measurement,
            "rtmr1": zero_measurement,
            "rtmr2": zero_measurement,
            "rtmr3": zero_measurement,
            "mr_aggregated": zero_measurement,
            "mr_config_id": "",
        },
        "compose_hash": f"dev-memory-sandbox-{digest[:16]}",
        "app_id": "dev-memory-sandbox",
        "instance_id": "dev-memory-sandbox",
    }
