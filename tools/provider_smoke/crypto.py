"""Headless mirror of the iOS/enclave content crypto (no Secure Enclave needed).

box_seal scheme (authoritative source: tools/v1_envelope_roundtrip_test.py:61-73):
  salt = ek_pub || recipient_pk_raw, info=b"feedling-box-seal-v1",
  ChaCha20-Poly1305 with a zero 12-byte nonce, wire = ek_pub(32) || ct || tag(16).
body: ChaCha20-Poly1305 IETF (12-byte nonce), AAD = f"{owner}|{v}|{id}".
"""
import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_BOX_SEAL_INFO = b"feedling-box-seal-v1"
_RAW = serialization.Encoding.Raw
_RAWPUB = serialization.PublicFormat.Raw
_ZERO_NONCE = b"\x00" * 12


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def generate_keypair() -> tuple[bytes, bytes]:
    sk = X25519PrivateKey.generate()
    sk_raw = sk.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pk_raw = sk.public_key().public_bytes(_RAW, _RAWPUB)
    return sk_raw, pk_raw


def _wrap_key(ek_pub: bytes, recipient_pk_raw: bytes, shared: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=ek_pub + recipient_pk_raw,
        info=_BOX_SEAL_INFO,
    ).derive(shared)


def box_seal(pt: bytes, recipient_pk_raw: bytes) -> bytes:
    recipient_pk = X25519PublicKey.from_public_bytes(recipient_pk_raw)
    ek = X25519PrivateKey.generate()
    ek_pub = ek.public_key().public_bytes(_RAW, _RAWPUB)
    key = _wrap_key(ek_pub, recipient_pk_raw, ek.exchange(recipient_pk))
    ct = ChaCha20Poly1305(key).encrypt(_ZERO_NONCE, pt, None)
    return ek_pub + ct


def box_open(blob: bytes, sk_raw: bytes, recipient_pk_raw: bytes) -> bytes:
    sk = X25519PrivateKey.from_private_bytes(sk_raw)
    ek_pub = blob[:32]
    ct = blob[32:]
    shared = sk.exchange(X25519PublicKey.from_public_bytes(ek_pub))
    key = _wrap_key(ek_pub, recipient_pk_raw, shared)
    return ChaCha20Poly1305(key).decrypt(_ZERO_NONCE, ct, None)


def decrypt_reply(env: dict, sk_raw: bytes, pk_raw: bytes) -> str:
    K = box_open(base64.b64decode(env["K_user"]), sk_raw, pk_raw)
    aad = f"{env['owner_user_id']}|{env.get('v', 1)}|{env['id']}".encode("utf-8")
    pt = ChaCha20Poly1305(K).decrypt(
        base64.b64decode(env["nonce"]),
        base64.b64decode(env["body_ct"]),
        aad,
    )
    return pt.decode("utf-8")
