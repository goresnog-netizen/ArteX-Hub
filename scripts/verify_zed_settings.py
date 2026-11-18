#!/usr/bin/env python3
"""Verify the Zed settings.json edit: JSONC-tolerating parse + structural checks."""

import json
import re
from pathlib import Path

p = Path.home() / "AppData" / "Roaming" / "Zed" / "settings.json"
t = p.read_text(encoding="utf-8")

t2 = re.sub(r"^\s*//.*$", "", t, flags=re.M)  # line comments
t2 = re.sub(r",(\s*[}\]])", r"\1", t2)  # trailing commas
d = json.loads(t2)

agent = d["agent"]
print("PARSED OK")
print("top-level servers:", list(agent["context_servers"].keys()))
print("top args:", agent["context_servers"]["snapgent-shell-sandboxed"]["args"])
print("git env:", agent["context_servers"]["snapgent-git"].get("env"))
for n, v in agent.get("profiles", {}).items():
    print("profile %-8s -> %s" % (n, list(v.get("context_servers", {}).keys())))
print("agent keys survive:", [k for k in agent if k != "context_servers"])
print(
    "enable_all_context_servers per profile:",
    {
        n: v.get("enable_all_context_servers")
        for n, v in agent.get("profiles", {}).items()
    },
)
