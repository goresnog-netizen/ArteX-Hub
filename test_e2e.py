#!/usr/bin/env python3
"""Standalone E2E test against the RUNNING bridge: WS auth + tool call + auth rejection."""

import asyncio, json, sys, urllib.request
from pathlib import Path

ROOT = Path(r"C:\MRP\proyek 2026\snapgent-connector")
sys.path.insert(0, str(ROOT))


def get_config_token() -> str:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    return cfg["bridge"]["auth"]["secret"]


async def main():
    import websockets

    token = get_config_token()
    url = "ws://127.0.0.1:17613"

    print("T1: WS WITH auth -> expect 'connected'")
    async with websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {token}"}, open_timeout=5
    ) as ws:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        print(
            "   type:",
            msg.get("type"),
            "| tools:",
            len(msg.get("tools", [])),
            "| mcp_alive:",
            msg.get("mcp_alive"),
        )

    print("T2: WS WITHOUT auth -> expect close 4001")
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            print("   got:", msg.get("type"), "(unexpected)")
    except Exception as e:
        print("   rejected:", type(e).__name__, getattr(e, "rcvd", None) or e)

    print("T3: call git tool through bridge -> expect tool_result ok")
    async with websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {token}"}, open_timeout=5
    ) as ws:
        await asyncio.wait_for(ws.recv(), timeout=10)  # connected banner
        await ws.send(
            json.dumps(
                {
                    "type": "call_tool",
                    "id": "e2e-1",
                    "name": "git_workspace_info",
                    "arguments": {},
                }
            )
        )
        while True:
            res = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if res.get("id") == "e2e-1":
                print(
                    "   ok:",
                    res.get("ok"),
                    "| text:",
                    (res.get("text") or "")[:160].replace("\n", " "),
                )
                break

    print("T4: GET /servers no token -> expect 401")
    try:
        urllib.request.urlopen("http://127.0.0.1:17614/servers", timeout=5)
        print("   got 200 (unexpected)")
    except urllib.error.HTTPError as e:
        print("   HTTP", e.code)

    print("T5: GET /servers with token -> expect 200")
    req = urllib.request.Request("http://127.0.0.1:17614/servers")
    req.add_header("Authorization", f"Bearer {token}")
    d = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
    print(
        "   servers:",
        [(s.get("id"), s.get("alive"), s.get("sandbox")) for s in d.get("servers", [])],
    )

    print("E2E DONE")


if __name__ == "__main__":
    asyncio.run(main())
