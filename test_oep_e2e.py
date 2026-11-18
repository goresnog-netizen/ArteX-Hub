#!/usr/bin/env python3
"""End-to-end OEP test against the live bridge.

Starts a fake OEP client, connects over WS with the bridge token, exchanges
X25519 keys, sends an encrypted tool call, and verifies the response:
  - the plaintext result comes back correct
  - the wire log of every frame the bridge sent contains NO plaintext
    arguments, path, or result bytes - only ciphertext.

The second assertion is the whole point of OEP: a packet capture on
127.0.0.1 (or a provider logging proxy) sees nothing useful.
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oep import OEP  # noqa: E402


BRIDGE_WS = "ws://127.0.0.1:17613"
BRIDGE_HEALTH = "http://127.0.0.1:17614"


def _load_token():
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    return cfg["bridge"]["auth"]["secret"]


def _load_ext_id():
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    return cfg["bridge"]["security"]["extensionId"]


async def main():
    token = _load_token()
    ext_id = _load_ext_id()
    url = f"{BRIDGE_WS}?token={token}"
    origin = f"chrome-extension://{ext_id}"

    # Every frame that crosses the socket is captured here for inspection.
    wire = []

    async with websockets.connect(url, origin=origin) as ws:
        # Receive the initial `connected` frame
        first = json.loads(await ws.recv())
        wire.append(first)
        assert first.get("type") == "connected", first
        print(f"connected: tools={len(first.get('tools', []))}")

        # --- OEP handshake ---------------------------------------------------
        ext = OEP()
        ext_pub_b64 = base64.b64encode(ext.public_key).decode("ascii")
        await ws.send(json.dumps({"type": "oep_hello", "id": 1, "pub": ext_pub_b64}))
        frame = json.loads(await ws.recv())
        wire.append(frame)
        assert frame.get("type") == "oep_ready", frame
        bridge_pub = base64.b64decode(frame["pub"])
        session_id = base64.b64decode(frame["session_id"])
        ext.establish(bridge_pub, session_id)
        print(f"oep established, session_id={frame['session_id']}")

        # --- Encrypted tool call ---------------------------------------------
        # git_workspace_info is unique to the git server, so the bare name is
        # resolvable. It takes no arguments, which keeps the test independent
        # of whichever IDE workspace the MCP servers are rooted at.
        plain = {
            "name": "git_workspace_info",
            "server": "git",
            "arguments": {},
            "timeout_ms": 10000,
        }
        env = ext.wrap_envelope(plain)
        print(f"envelope length: {len(env)} bytes")

        # The whole point: the provider-visible text must not leak the call.
        leaked = []
        for needle in ("git_workspace_info", "arguments", "timeout_ms"):
            if needle in env:
                leaked.append(needle)
        assert not leaked, f"plaintext leaked into envelope: {leaked}"

        await ws.send(json.dumps({"type": "oep_call_tool", "id": 2, "env": env}))
        result_frame = json.loads(await ws.recv())
        wire.append(result_frame)
        assert result_frame.get("type") == "oep_result", result_frame

        # Result envelope must also carry no plaintext
        renv = result_frame["env"]
        for needle in ("is_git_repository", "workspace_info", "IndoHunt"):
            assert needle not in renv, f"plaintext leaked in result: {needle!r}"
        print(f"result envelope length: {len(renv)} bytes")

        decoded = ext.unwrap_result(renv)
        assert decoded and decoded.get("ok") is True, decoded
        body = decoded.get("text") or ""
        assert "is_git_repository" in body, "decrypted body is not the workspace info"
        print(
            f"decrypted result: {len(body)} bytes of workspace info, ok={decoded['ok']}"
        )

    # Inspect the wire log: it must contain zero plaintext bytes that would
    # betray the tool args or the result body.
    for frame in wire:
        if frame.get("type") in ("oep_call_tool", "oep_result"):
            env = frame.get("env", "")
            for needle in ("git_workspace_info", "arguments", "is_git_repository"):
                assert needle not in env, f"envelope leaked {needle!r}"

    print("PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
