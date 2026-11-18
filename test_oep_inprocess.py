#!/usr/bin/env python3
"""In-process OEP end-to-end test against a fresh bridge instance.

Boots a Bridge() on a random loopback port, spawns a WS client, exercises
`oep_hello` + `oep_call_tool` against a stub MCP server, and asserts:
  - plaintext round-trips correctly
  - neither the envelope nor the result leaks the tool name, args, or body

Does not touch the live bridge on 17613, so it can run while Tiger's
production bridge keeps serving.
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import Bridge  # noqa: E402
from oep import OEP  # noqa: E402


class _StubServer:
    """Minimal MCP-like server the bridge can route through."""

    def __init__(self, server_id="stub"):
        self.server_id = server_id
        self.alive = True
        self.tools = [
            {
                "name": "read_file",
                "description": "stub read",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ]
        self.error = None
        self.sandbox_type = "none"

    async def call_tool(self, name, arguments, timeout):
        if name != "read_file":
            raise RuntimeError(f"unknown tool: {name}")
        path = arguments.get("path")
        # File content deliberately has 'import asyncio' to test the leak check.
        body = "import asyncio\n# secret-file-content from stub\n"
        return {
            "content": [{"type": "text", "text": body}],
            "isError": False,
            "path": path,
        }


async def main():
    bridge = Bridge()
    bridge.auth.enabled = False
    bridge.allowed_origins = set()
    bridge.servers = {"stub": _StubServer()}
    bridge._rebuild_catalog()

    server = await websockets.serve(
        bridge.handle_client, "127.0.0.1", 0, ping_interval=None, ping_timeout=None
    )
    port = list(server.sockets)[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}"
    print(f"test bridge on {url}")

    wire = []
    try:
        async with websockets.connect(url) as ws:
            first = json.loads(await ws.recv())
            wire.append(first)
            print(f"got {first.get('type')}")

            ext = OEP()
            pub_b64 = base64.b64encode(ext.public_key).decode("ascii")
            await ws.send(
                json.dumps({"type": "oep_hello", "id": 1, "pub": pub_b64})
            )
            frame = json.loads(await ws.recv())
            wire.append(frame)
            assert frame.get("type") == "oep_ready", frame
            bridge_pub = base64.b64decode(frame["pub"])
            sid = base64.b64decode(frame["session_id"])
            ext.establish(bridge_pub, sid)
            print("oep established")

            plain = {
                "name": "read_file",
                "arguments": {"path": "bridge.py"},
                "timeout_ms": 5000,
            }
            env = ext.wrap_envelope(plain)
            for needle in ("read_file", "bridge.py", "arguments"):
                assert needle not in env, f"plaintext leaked: {needle!r}"
            print(f"envelope len={len(env)}")

            await ws.send(
                json.dumps({"type": "oep_call_tool", "id": 2, "env": env})
            )
            res = json.loads(await ws.recv())
            wire.append(res)
            assert res.get("type") == "oep_result", res
            renv = res["env"]
            for needle in ("import asyncio", "secret-file-content", "read_file"):
                assert needle not in renv, f"result leaked: {needle!r}"
            print(f"result env len={len(renv)}")

            out = ext.unwrap_result(renv)
            assert out and out.get("ok") is True, out
            assert "import asyncio" in out.get("text", ""), out
            print(f"decrypted {len(out['text'])} bytes, ok={out['ok']}")

        # Full wire audit
        wire_blob = json.dumps(wire)
        for needle in ("bridge.py", "import asyncio", "secret-file-content"):
            assert needle not in wire_blob, f"wire leaked {needle!r}"
        print("wire audit OK: no plaintext in any frame")
        print("PASS")
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        sys.exit(1)
