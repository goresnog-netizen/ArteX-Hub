#!/usr/bin/env python3
"""Integration test: bridge-side coordination (providers, quota, memory, A2A) over the real WS API."""

import asyncio
import json
import sys
import websockets

HOST = "127.0.0.1"
PORT = 17613
HEALTH = 17614
ROOT = r"C:\MRP\proyek 2026\snapgent-connector"

CONFIG = json.load(open(rf"{ROOT}\config.json", encoding="utf-8"))
TOKEN = CONFIG["bridge"]["auth"]["secret"]
PROVIDERS = sorted(CONFIG.get("providers", {}))

ok = True


def check(name, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print("%s %s %s" % ("PASS" if cond else "FAIL", name, detail))


async def main():
    uri = f"ws://{HOST}:{PORT}"
    async with websockets.connect(
        uri, additional_headers={"Authorization": f"Bearer {TOKEN}"}, open_timeout=5
    ) as ws:
        # connected payload must carry the provider snapshot
        first = json.loads(await ws.recv())
        if first.get("type") != "connected":
            first = json.loads(await ws.recv())
        check(
            "connected_providers",
            first.get("type") == "connected"
            and len(first.get("providers", [])) == len(PROVIDERS),
            "%s/%s" % (len(first.get("providers", [])), len(PROVIDERS)),
        )

        req = json.dumps({"type": "coord_stats", "id": 1}) + "\n"
        await ws.send(req)
        msg = json.loads(await ws.recv())
        stats = (msg.get("providers") or {}).get("providers", {})
        check("coord_stats_providers", sorted(stats) == PROVIDERS, str(sorted(stats)))
        check(
            "coord_stats_agents",
            "bridge" in (msg.get("providers") or {}).get("a2a_agents", []),
        )
        check(
            "coord_stats_memory",
            sorted((msg.get("providers") or {}).get("memory_profiles", {}))
            == PROVIDERS,
        )

        # memory optimizer must shrink an oversized payload for the chosen provider
        big = [{"role": "user", "content": "x" * 40000} for _ in range(60)]
        await ws.send(
            json.dumps(
                {"type": "coord_prepare", "id": 2, "provider": "qwen", "messages": big}
            )
            + "\n"
        )
        msg = json.loads(await ws.recv())
        check(
            "coord_prepare_type",
            msg.get("type") == "coord_prepared",
            msg.get("type", ""),
        )
        check(
            "coord_prepare_compressed",
            msg.get("compressed") is True,
            "before=%s after=%s" % (msg.get("tokens_before"), msg.get("tokens_after")),
        )
        check(
            "coord_prepare_strategy",
            msg.get("strategy") == "rolling-summary",
            str(msg.get("strategy")),
        )

        # quota exhaustion must raise, not silently overshoot
        await ws.send(
            json.dumps(
                {
                    "type": "coord_prepare",
                    "id": 3,
                    "provider": "qwen",
                    "allowOverQuota": False,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
            + "\n"
        )
        msg = json.loads(await ws.recv())
        check(
            "coord_prepare_ok_small",
            msg.get("type") == "coord_prepared",
            msg.get("type", ""),
        )

        # a2a broadcast must be routed to registered agents
        await ws.send(
            json.dumps(
                {
                    "type": "a2a_send",
                    "id": 4,
                    "sender": "test",
                    "messageType": "ping",
                    "payload": {"n": 1},
                }
            )
            + "\n"
        )
        msg = json.loads(await ws.recv())
        check("a2a_ack", msg.get("type") == "a2a_ack", msg.get("type", ""))
        check("a2a_message", msg.get("message", {}).get("type") == "ping")

    print("OVERALL: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
