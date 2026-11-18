#!/usr/bin/env python3
"""Register/refresh the snapgent MCP servers in Zed's settings.json.

Idempotent: every existing `snapgent-*` entry is removed from every
`context_servers` map and the current server set is written back. Safe to
re-run after the server list changes.

    python scripts/register_zed_mcp.py                  # apply to Zed settings
    python scripts/register_zed_mcp.py --dry-run        # print the result only
    python scripts/register_zed_mcp.py --root D:\\path\\to\\project
    python scripts/register_zed_mcp.py --settings C:\\path\\settings.json

`--root` sets the workspace every snapgent server works in (SNAPGENT_WORKSPACE).
Without it the folder Zed is actually working in is detected from the Zed
thread database, falling back to this repository. The MCP server scripts
themselves always run from the repository.
"""

import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ZED_SETTINGS = Path.home() / "AppData" / "Roaming" / "Zed" / "settings.json"
ROOT = Path(__file__).resolve().parent.parent


def build_servers(workspace: Path) -> dict:
    """Server definitions; `workspace` is the project every server works in."""
    return {
        "snapgent-git": {
            "command": "python",
            "args": [str(ROOT / "git_mcp.py")],
            "env": {"SNAPGENT_WORKSPACE": str(workspace)},
        },
        "snapgent-shell": {
            "command": "python",
            "args": [str(ROOT / "shell_mcp.py")],
            "env": {"SNAPGENT_WORKSPACE": str(workspace)},
        },
        "snapgent-shell-sandboxed": {
            "command": "python",
            "args": [
                str(ROOT / "sandbox_job.py"),
                str(ROOT / "scripts" / "shell-mcp-job.json"),
            ],
            "env": {"SNAPGENT_WORKSPACE": str(workspace)},
        },
        "snapgent-workspace": {
            "command": "python",
            "args": [str(ROOT / "vscode_mcp.py")],
            "env": {"SNAPGENT_WORKSPACE": str(workspace)},
        },
        # Agent-to-agent bridge: the Zed agent talks to the local bridge over
        # its WebSocket protocol (A2A inbox, mesh projects, coordination).
        "snapgent-a2a": {
            "command": "python",
            "args": [str(ROOT / "zed_a2a_mcp.py")],
            "env": {
                "SNAPGENT_WORKSPACE": str(workspace),
                "SNAPGENT_BRIDGE_URL": "ws://127.0.0.1:17613",
                "SNAPGENT_AGENT_NAME": "zed",
            },
        },
    }


def _match_block(text: str, open_index: int) -> int:
    """Index just after the `}` matching the `{` at open_index (string-aware)."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(open_index, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced braces")


def _strip_entries(inner: str, names) -> str:
    out = inner
    for name in names:
        pattern = r'"%s"\s*:\s*\{' % re.escape(name)
        while True:
            match = re.search(pattern, out)
            if not match:
                break
            brace = out.find("{", match.start())
            end = _match_block(out, brace)
            tail = end
            while tail < len(out) and out[tail] in " \t\r\n":
                tail += 1
            if tail < len(out) and out[tail] == ",":
                tail += 1
            out = out[: match.start()] + out[tail:]
    return out


def _entry_text(name: str, cfg: dict, indent: str) -> str:
    lines = ['%s"%s": {' % (indent, name)]
    lines.append('%s  "command": %s,' % (indent, json.dumps(cfg["command"])))
    lines.append(
        '%s  "args": [%s],' % (indent, ", ".join(json.dumps(a) for a in cfg["args"]))
    )
    env = cfg.get("env")
    if env:
        lines.append('%s  "env": {' % indent)
        items = list(env.items())
        for j, (key, value) in enumerate(items):
            comma = "," if j < len(items) - 1 else ""
            lines.append('%s    "%s": %s%s' % (indent, key, json.dumps(value), comma))
        lines.append("%s  }," % indent)
    else:
        lines[-1] = lines[-1][:-1]
    lines.append("%s}" % indent)
    return "\n".join(lines)


def _indent_of(text: str, index: int) -> str:
    line_start = text.rfind("\n", 0, index) + 1
    prefix = text[line_start:index]
    return prefix[: len(prefix) - len(prefix.lstrip())]


def _upsert_block(raw: str, start: int, end: int, indent: str, servers: dict) -> str:
    inner = raw[start + 1 : end - 1]
    cleaned = _strip_entries(inner, servers.keys())
    cleaned = re.sub(r",\s*$", "", cleaned.rstrip())
    cleaned = re.sub(r",(\s*)$", r"\1", cleaned)
    entries = ",\n".join(_entry_text(n, c, indent) for n, c in servers.items())
    if cleaned.strip():
        body = cleaned.rstrip() + ",\n" + entries
    else:
        body = "\n" + entries + "\n" + indent[:-2]
    return raw[: start + 1] + body + raw[end - 1 :]


def _refresh_all_blocks(raw: str, servers: dict) -> str:
    index = 0
    while True:
        hit = raw.find('"context_servers"', index)
        if hit < 0:
            return raw
        brace = raw.find("{", hit)
        if brace < 0:
            return raw
        end = _match_block(raw, brace)
        block_indent = _indent_of(raw, hit) + "  "
        raw = _upsert_block(raw, brace, end, block_indent, servers)
        index = brace + 1


def _ensure_agent_block(raw: str, servers: dict) -> str:
    if '"agent"' not in raw:
        return raw
    agent = raw.find('"agent"')
    brace = raw.find("{", agent)
    end = _match_block(raw, brace)
    inner = raw[brace + 1 : end - 1]
    if '"context_servers"' in inner:
        return raw
    indent = _indent_of(raw, agent) + "  "
    entries = ",\n".join(_entry_text(n, c, indent) for n, c in servers.items())
    block = '\n%s"context_servers": {\n%s\n%s},' % (indent, entries, indent[:-2])
    return raw[: brace + 1] + block + raw[brace + 1 :]


def _validate(text: str) -> None:
    stripped = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    json.loads(stripped)


def detect_workspace() -> Path:
    """Find the folder Zed is actually working in.

    Zed keeps no folder state file in %APPDATA%, but every agent thread
    records the folders it ran in. The most recently updated thread holds the
    project the operator has open. Falls back to this repository.
    """
    local = os.environ.get("LOCALAPPDATA")
    if local:
        database = Path(local) / "Zed" / "threads" / "threads.db"
        if database.is_file():
            try:
                connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
                try:
                    rows = connection.execute(
                        "SELECT folder_paths FROM threads "
                        "WHERE folder_paths IS NOT NULL AND folder_paths <> '' "
                        "ORDER BY updated_at DESC LIMIT 20"
                    ).fetchall()
                finally:
                    connection.close()
            except Exception:
                rows = []
            for (raw,) in rows:
                for entry in str(raw or "").splitlines():
                    entry = entry.strip()
                    if not entry:
                        continue
                    candidate = Path(entry)
                    if candidate.is_dir():
                        return candidate.resolve()
    return ROOT


def _parse_args(argv):
    settings = ZED_SETTINGS
    workspace = None
    dry_run = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--dry-run":
            dry_run = True
        elif arg in ("--root", "--settings"):
            if i + 1 >= len(argv):
                raise SystemExit("%s requires a path" % arg)
            value = Path(argv[i + 1]).expanduser().resolve()
            i += 1
            if arg == "--root":
                workspace = value
            else:
                settings = value
        else:
            raise SystemExit("unknown argument: %s" % arg)
        i += 1
    return settings, workspace, dry_run


def main() -> int:
    settings, workspace, dry_run = _parse_args(sys.argv[1:])
    if not settings.exists():
        print("error: Zed settings not found: %s" % settings)
        return 1
    if workspace is None:
        workspace = detect_workspace()
    if not workspace.is_dir():
        print("error: workspace root is not a directory: %s" % workspace)
        return 1

    servers = build_servers(workspace)
    raw = settings.read_text(encoding="utf-8")
    updated = _refresh_all_blocks(raw, servers)
    updated = _ensure_agent_block(updated, servers)
    _validate(updated)

    if updated == raw:
        print("OK: snapgent servers already up to date")
        return 0
    if dry_run:
        print(updated)
        return 0

    backup = settings.with_suffix(".json.bak-%d" % int(time.time()))
    shutil.copy2(settings, backup)
    settings.write_text(updated, encoding="utf-8")
    print("backup: %s" % backup)
    print("workspace: %s" % workspace)
    print("registered %d snapgent servers: %s" % (len(servers), ", ".join(servers)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
