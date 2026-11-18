import json, shutil, time
from pathlib import Path

p = Path.home() / "AppData" / "Roaming" / "Code" / "User" / "mcp.json"
root = Path(r"C:\MRP\proyek 2026\snapgent-connector")

servers = {
    "snapgent-vscode": {
        "type": "stdio",
        "command": "python",
        "args": [str(root / "vscode_mcp.py")],
        "env": {"SNAPGENT_WORKSPACE": str(root)},
    },
    "snapgent-git": {
        "type": "stdio",
        "command": "python",
        "args": [str(root / "git_mcp.py")],
        "env": {"SNAPGENT_WORKSPACE": str(root)},
    },
    "snapgent-shell": {
        "type": "stdio",
        "command": "python",
        "args": [str(root / "shell_mcp.py")],
        "env": {"SNAPGENT_WORKSPACE": str(root)},
    },
}

data = json.loads(p.read_text(encoding="utf-8"))
for k, v in servers.items():
    if k not in data["servers"]:
        data["servers"][k] = v
        print("added", k)
    else:
        print("skip", k)

backup = p.with_suffix(".json.bak-%d" % int(time.time()))
shutil.copy2(p, backup)
print("backup", backup)
p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
print("written OK")
