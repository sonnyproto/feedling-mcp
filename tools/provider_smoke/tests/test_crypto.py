import base64
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from tools.provider_smoke import crypto

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
import content_encryption as ce  # noqa: E402 — the authoritative implementation


# ---- drift guards: mirror vs the REAL implementation ----
#
# Everything below this comment used to be tested only against itself (mirror seals,
# mirror opens), which is why a real drift went unnoticed: crypto.py still carried the
# OLD BoxSeal scheme (HKDF salt=ek_pub||recipient + zero nonce) long after the server
# moved to salt=None + nonce=SHA256(ek_pub||recipient_pk)[:12]. Self-consistent tests
# stayed green while `run_smoke <any provider>` died on a bare `InvalidTag` for every
# reply — the tool was 100% broken and nothing caught it.
#
# The only assertion that means anything: WHAT THE SERVER SEALS, THIS MIRROR MUST OPEN.


def test_mirror_opens_what_the_server_seals():
    sk, pk = crypto.generate_keypair()
    blob = ce.box_seal(b"sealed by the server", pk)
    assert crypto.box_open(blob, sk, pk) == b"sealed by the server"


def test_mirror_decrypts_a_real_server_envelope():
    """The exact path the smoke client walks: the server builds the v1 envelope
    (K_user wrap + body AEAD bound by the owner|v|id AAD); we must recover it."""
    sk, pk = crypto.generate_keypair()
    _, enclave_pk = crypto.generate_keypair()
    env = ce.build_envelope(
        plaintext="回复内容".encode(),
        owner_user_id="usr_test",
        user_pk_bytes=pk,
        enclave_pk_bytes=enclave_pk,
    )
    assert crypto.decrypt_reply(env, sk, pk) == "回复内容"


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
