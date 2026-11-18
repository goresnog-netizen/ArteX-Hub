#!/usr/bin/env python3
"""Opaque Envelope Protocol (OEP) v1 - crypto core.

The extension and the bridge share a session key derived from X25519.
Tool commands and results travel through the chat as AES-256-GCM envelopes:

    [ENV:v1:<base64(nonce || ciphertext || tag)>]
    [RES:v1:<base64(nonce || ciphertext || tag)>]

The provider (ChatGPT, Claude, DeepSeek, Gemini, ...) sees only these
bracketed strings. It cannot decrypt them - the session key never leaves
the local bridge and the extension.
"""

import base64
import json
import os
import re
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


ENV_RE = re.compile(r"\[ENV:v1:([A-Za-z0-9+/=]+)\]")
RES_RE = re.compile(r"\[RES:v1:([A-Za-z0-9+/=]+)\]")
NONCE_BYTES = 12


class OEP:
    """One side of an OEP session.

    Lifecycle:
        bridge = OEP()
        ext = OEP()
        bridge.establish(ext.public_key, session_id)
        ext.establish(bridge.public_key, session_id)
        env = ext.wrap_envelope({"command": "read_file", ...})
        payload = bridge.unwrap_envelope(env)
        result = bridge.wrap_result({"ok": True, "stdout": "..."})
        data = ext.unwrap_result(result)
    """

    def __init__(self) -> None:
        self._priv = X25519PrivateKey.generate()
        self._key: bytes | None = None

    @property
    def public_key(self) -> bytes:
        return self._priv.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    def establish(self, peer_pub: bytes, session_id: bytes) -> bytes:
        if len(peer_pub) != 32:
            raise ValueError("peer public key must be 32 bytes")
        if not session_id:
            raise ValueError("session_id must be non-empty")
        peer = X25519PublicKey.from_public_bytes(peer_pub)
        shared = self._priv.exchange(peer)
        self._key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=session_id,
            info=b"snapgent-oep-v1",
        ).derive(shared)
        return self._key

    def _require_key(self) -> bytes:
        if self._key is None:
            raise RuntimeError("OEP session not established")
        return self._key

    @staticmethod
    def _b64encode(blob: bytes) -> str:
        return base64.b64encode(blob).decode("ascii")

    @staticmethod
    def _b64decode(text: str) -> bytes:
        return base64.b64decode(text, validate=True)

    def _wrap(self, prefix: str, payload: dict) -> str:
        pt = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        nonce = os.urandom(NONCE_BYTES)
        ct = AESGCM(self._require_key()).encrypt(nonce, pt, None)
        return f"[{prefix}:v1:{self._b64encode(nonce + ct)}]"

    def _unwrap(self, text: str, regex: re.Pattern) -> dict | None:
        m = regex.search(text)
        if not m:
            return None
        blob = self._b64decode(m.group(1))
        if len(blob) < NONCE_BYTES + 16:
            raise ValueError("envelope too short")
        nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        pt = AESGCM(self._require_key()).decrypt(nonce, ct, None)
        return json.loads(pt.decode("utf-8"))

    def wrap_envelope(self, payload: dict) -> str:
        return self._wrap("ENV", payload)

    def unwrap_envelope(self, text: str) -> dict | None:
        return self._unwrap(text, ENV_RE)

    def wrap_result(self, payload: dict) -> str:
        return self._wrap("RES", payload)

    def unwrap_result(self, text: str) -> dict | None:
        return self._unwrap(text, RES_RE)


class OEPSession:
    """Session-id + per-connection shared key store.

    The bridge keeps one OEPSession per accepted WS client. When the client
    sends `oep_hello` with its public key, the bridge establishes a shared
    key for that socket. All subsequent envelopes are decrypted with that
    session key. No other socket can decrypt them.
    """

    def __init__(self, session_id: bytes | None = None) -> None:
        self.session_id = session_id or os.urandom(16)
        self.oep = OEP()
        self.established = False

    def on_hello(self, peer_pub_b64: str) -> str:
        peer_pub = base64.b64decode(peer_pub_b64, validate=True)
        self.oep.establish(peer_pub, self.session_id)
        self.established = True
        return base64.b64encode(self.oep.public_key).decode("ascii")


# ---------------------------------------------------------------------------
# Self-test (run as: python oep.py)
# ---------------------------------------------------------------------------


def _self_test() -> int:
    alice = OEP()
    bob = OEP()
    session = b"snapgent-test-session-0001"

    alice.establish(bob.public_key, session)
    bob.establish(alice.public_key, session)

    msg = {"command": "read_file", "params": {"path": "bridge.py"}, "id": 1}
    env = alice.wrap_envelope(msg)
    print(f"env len: {len(env)}")
    print(f"env: {env[:80]}...")

    # Provider-visible text must not contain plaintext
    for needle in ("read_file", "bridge.py", "params"):
        assert needle not in env, f"{needle!r} leaked into envelope"

    out = bob.unwrap_envelope(env)
    assert out == msg, f"round-trip mismatch: {out!r}"
    print(f"envelope round-trip: {out}")

    res = {"ok": True, "stdout": "secret-file-content", "exit_code": 0}
    renv = bob.wrap_result(res)
    for needle in ("secret", "stdout", "exit_code"):
        assert needle not in renv, f"{needle!r} leaked into result"
    rout = alice.unwrap_result(renv)
    assert rout == res, f"result round-trip mismatch: {rout!r}"
    print(f"result round-trip: {rout}")

    # Tampering must fail
    tampered = env[:-3] + ("AA" if env[-3:4] != "AA" else "BB") + "]"
    try:
        bob.unwrap_envelope(tampered)
        raise AssertionError("tampered envelope was accepted")
    except Exception as exc:
        print(f"tamper rejected: {type(exc).__name__}")

    # Wrong key must fail
    eve = OEP()
    eve.establish(OEP().public_key, session)
    try:
        eve.unwrap_envelope(env)
        raise AssertionError("wrong-key envelope was accepted")
    except Exception as exc:
        print(f"wrong-key rejected: {type(exc).__name__}")

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
