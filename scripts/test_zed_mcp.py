#!/usr/bin/env python3
"""Spawn each Zed-registered server exactly as Zed would, drive a minimal MCP handshake, report."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\MRP\proyek 2026\snapgent-connector")
SERVERS = {
    "snapgent-git": ["python", str(ROOT / "git_mcp.py")],
    "snapgent-shell": ["python", str(ROOT / "shell_mcp.py")],
    "snapgent-shell-sandboxed": [
        "python",
        str(ROOT / "sandbox_job.py"),
        str(ROOT / "scripts" / "shell-mcp-job.json"),
    ],
}

REQ = (
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "zed-probe", "version": "1"},
            },
        }
    )
    + "\n"
    + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    + "\n"
    + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    + "\n"
)


def main() -> int:
    ok = True
    for name, cmd in SERVERS.items():
        p = subprocess.run(
            cmd, input=REQ, capture_output=True, text=True, timeout=120, cwd=str(ROOT)
        )
        lines = [l for l in p.stdout.splitlines() if l.strip()]
        ids, tools, note = [], None, ""
        for l in lines:
            try:
                m = json.loads(l)
            except ValueError:
                ok = False
                note = "NON-JSON RESPONSE: %r" % l[:120]
                continue
            if m.get("id") == 1:
                ids.append(
                    "initialize=%s"
                    % m.get("result", {}).get("serverInfo", {}).get("name", "?")
                )
            elif m.get("id") == 2 and "result" in m:
                tools = len(m["result"].get("tools", []))
            elif m.get("id") is None or "id" not in m:
                ok = False
                note = "RESPONDED TO A NOTIFICATION: %r" % l[:120]
        verdict = "PASS" if (len(ids) == 1 and tools and not note) else "FAIL"
        if verdict == "FAIL":
            ok = False
        print(
            "%s %-26s responses=%d tools=%s %s"
            % (verdict, name, len(lines), tools, note)
        )
    print("OVERALL: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
