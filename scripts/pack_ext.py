#!/usr/bin/env python3
"""Package the MV3 extension into dist/.

The archive name comes from manifest.json, so it can never drift from the
version the extension actually declares. Dev-only files are excluded by name
and the archive is verified with testzip() before it is reported as good.

    python scripts\\pack_ext.py
"""

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "snapgent-extension"
DIST = ROOT / "dist"

EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules"}
EXCLUDE_FILES = {"test-chatgpt.js", "test-meta.js", "test-parser.js", "README.md"}
EXCLUDE_SUFFIX = {".pyc", ".log", ".tmp", ".map"}


def main() -> int:
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    version = str(manifest.get("version", "0.0.0"))
    name = str(manifest.get("name", "extension")).lower().replace(" ", "-")
    out = DIST / f"{name}-{version}.zip"
    DIST.mkdir(parents=True, exist_ok=True)

    files = [
        path
        for path in sorted(EXT.rglob("*"))
        if path.is_file()
        and path.name not in EXCLUDE_FILES
        and path.suffix not in EXCLUDE_SUFFIX
        and not (set(path.relative_to(EXT).parts) & EXCLUDE_DIRS)
    ]
    if not files:
        print("ERROR: no extension files found")
        return 1

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(EXT).as_posix())

    with zipfile.ZipFile(out) as archive:
        broken = archive.testzip()
        names = set(archive.namelist())
    if broken:
        print(f"ERROR: corrupt entry {broken}")
        return 1

    required = {"manifest.json", "background.js"}
    missing = required - names
    if missing:
        print(f"ERROR: archive is missing {sorted(missing)}")
        return 1

    print(
        f"OK  {out.relative_to(ROOT)}  {len(files)} files  {out.stat().st_size // 1024} KB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
