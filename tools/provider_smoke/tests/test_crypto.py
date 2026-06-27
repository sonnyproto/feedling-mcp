import base64
import secrets

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from tools.provider_smoke import crypto


def test_box_seal_open_roundtrip():
    sk, pk = crypto.generate_keypair()
    secret = b"a 32-byte-ish payload \x00\x01\x02"
    sealed = crypto.box_seal(secret, pk)
    assert sealed[:32] != secret  # has ephemeral pubkey prefix
    assert crypto.box_open(sealed, sk, pk) == secret


def test_decrypt_reply_matches_enclave_wire_format():
    # Build an envelope exactly like the enclave would when sealing a reply to
    # the user's content key, then prove decrypt_reply recovers the plaintext.
    sk, pk = crypto.generate_keypair()
    owner = "usr_deadbeefdeadbeef"
    item_id = secrets.token_hex(16)
    plaintext = "PONG-1234 你好".encode("utf-8")

    K = secrets.token_bytes(32)
    body_nonce = secrets.token_bytes(12)
    aad = f"{owner}|1|{item_id}".encode("utf-8")
    body_ct = ChaCha20Poly1305(K).encrypt(body_nonce, plaintext, aad)
    k_user = crypto.box_seal(K, pk)

    env = {
        "owner_user_id": owner,
        "v": 1,
        "id": item_id,
        "body_ct": base64.b64encode(body_ct).decode(),
        "nonce": base64.b64encode(body_nonce).decode(),
        "K_user": base64.b64encode(k_user).decode(),
    }
    assert crypto.decrypt_reply(env, sk, pk) == "PONG-1234 你好"


def test_b64_roundtrip():
    assert base64.b64decode(crypto.b64(b"\x00\xff")) == b"\x00\xff"
