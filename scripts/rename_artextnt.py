from pathlib import Path

root = Path(r"C:\MRP\proyek 2026\ArteX-HUB")
skip_dirs = {
    ".git",
    "logs",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "_cleanup_archive_20261111",
}
skip_suffix = {".png", ".jpg", ".jpeg", ".ico", ".zip", ".exe", ".pyc", ".log", ".map"}

variants = ["ArteX-TNT", "ArteX-TNT", "ArteX-TNT", "ArteX-TNT"]
new = "ArteX-TNT"

updated = 0
for path in root.rglob("*"):
    if not path.is_file():
        continue
    if any(part in skip_dirs for part in path.parts):
        continue
    if path.suffix.lower() in skip_suffix:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        continue
    original = text
    for variant in variants:
        text = text.replace(variant, new)
    if text != original:
        path.write_text(text, encoding="utf-8")
        updated += 1

print(f"updated {updated} files")
PY
    print("cleaned")
PY
python scripts/rename_artextnt.py
