"""Diagnose the bridge: health, extension origin pin, and IDE workspaces.

    python scripts/check_bridge_health.py              # full report
    python scripts/check_bridge_health.py --extension-id  # just the pinned id

The extension id is the value the bridge pins for chrome-extension:// origins.
If the browser still runs an older build of the extension, its id differs and
/auth-token answers 403 - the panel then shows the bridge as offline.
"""

import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
HEALTH_URL = "http://127.0.0.1:17614/health"


def pinned_extension_id():
    if not CONFIG.exists():
        return ""
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    security = config.get("bridge", {}).get("security", {})
    return str(security.get("extensionId") or "").strip()


def manifest_extension_id():
    manifest_path = ROOT / "snapgent-extension" / "manifest.json"
    if not manifest_path.exists():
        return ""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    key = manifest.get("key")
    if not key:
        return ""
    import base64
    import hashlib

    der = base64.b64decode(key)
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def vscode_workspace():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return ""
    storage = Path(appdata) / "Code" / "User" / "globalStorage" / "storage.json"
    if not storage.is_file():
        return ""
    try:
        data = json.loads(storage.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""
    windows_state = data.get("windowsState") or {}
    uris = []
    last_folder = (windows_state.get("lastActiveWindow") or {}).get("folder")
    if last_folder:
        uris.append(last_folder)
    for window in windows_state.get("openedWindows") or []:
        folder = (window or {}).get("folder")
        if folder:
            uris.append(folder)
    for uri in uris:
        path = re.sub(r"^file://", "", str(uri))
        path = path.replace("%3A", ":").replace("%20", " ")
        path = re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), path)
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        candidate = Path(path)
        if candidate.is_dir():
            return str(candidate.resolve())
    return ""


def zed_workspace():
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return ""
    database = Path(local) / "Zed" / "threads" / "threads.db"
    if not database.is_file():
        return ""
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
        return ""
    for (raw,) in rows:
        for entry in str(raw or "").splitlines():
            entry = entry.strip()
            if entry and Path(entry).is_dir():
                return str(Path(entry).resolve())
    return ""


def last_seen_origin():
    log = ROOT / "logs" / "bridge_debug.log"
    if not log.is_file():
        return ""
    try:
        with log.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-400:]
    except OSError:
        return ""
    for line in reversed(lines):
        if "auth_token_refused" in line:
            match = re.search(r"origin=([^,\s]+)", line)
            if match:
                return match.group(1)
    return ""


def main() -> int:
    if "--extension-id" in sys.argv:
        print(pinned_extension_id())
        return 0

    print("== Snapgent bridge check ==")
    try:
        response = urllib.request.urlopen(HEALTH_URL, timeout=3)
        health = json.loads(response.read())
        print("bridge      : healthy (version %s)" % health.get("version"))
        print(
            "servers     : %s active / %s configured"
            % (
                health.get("active_servers"),
                health.get("servers"),
            )
        )
    except urllib.error.URLError as error:
        print("bridge      : OFFLINE (%s)" % error)
    except Exception as error:
        print("bridge      : OFFLINE (%s)" % error)

    pinned = pinned_extension_id()
    from_manifest = manifest_extension_id()
    print("ext id pin  : %s" % (pinned or "<not pinned>"))
    print("manifest id : %s" % (from_manifest or "<no key in manifest>"))
    if pinned and from_manifest and pinned != from_manifest:
        print("             MISMATCH - run scripts/gen_ext_key.py again")
    seen = last_seen_origin()
    if seen:
        print("last origin : %s" % seen)
        if pinned and pinned not in seen:
            print("             the browser still runs a different extension id")
            print("             reload it: chrome://extensions -> Reload")
    print("vscode root : %s" % (vscode_workspace() or "<none>"))
    print("zed root    : %s" % (zed_workspace() or "<none>"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
